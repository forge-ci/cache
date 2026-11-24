"""The session log is a history: append-only, derived status, replayable."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cachelib.chain import plan_push, plan_restore
from cachelib.manifest import Key, Manifest
from cachelib.sessions import (
    CLOSED,
    FAILED,
    OPEN,
    PUBLISH,
    RESTORE,
    SessionLog,
    publish_signature,
    record_publish,
    record_restore,
    replay,
    restore_signature,
)
from cachelib.store import FileStore, digest_of

KEY = Key("obs-rust/obs-studio", "app", "linux-x64")
IDENTITY = "rustc1.98.1-linux-x64-debug"


class SessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.log = SessionLog(self.root / "sqlite.db")

    def tearDown(self):
        self.log.close_connection()
        self._tmp.cleanup()

    def test_a_new_session_is_open_with_no_events(self):
        session = self.log.open(KEY, IDENTITY, ref="refs/heads/master", run_id="42")
        self.assertEqual(session.status, OPEN)
        self.assertEqual(self.log.events(session.id), [])
        self.assertEqual(len(session.id), 32)

    def test_events_are_ordinal_and_ordered(self):
        session = self.log.open(KEY, IDENTITY)
        for _ in range(3):
            self.log.append(session.id, RESTORE, detail="miss:none")
        self.assertEqual([e["ordinal"] for e in self.log.events(session.id)], [1, 2, 3])
        self.assertEqual([e["op"] for e in self.log.events(session.id)], [RESTORE] * 3)

    def test_status_is_derived_from_events_not_stored(self):
        session = self.log.open(KEY, IDENTITY)
        self.assertEqual(self.log.get(session.id).status, OPEN)
        self.log.seal(session.id)
        self.assertEqual(self.log.get(session.id).status, CLOSED)

    def test_sealing_twice_appends_and_stays_closed(self):
        session = self.log.open(KEY, IDENTITY)
        self.log.seal(session.id)
        self.log.seal(session.id)
        self.assertEqual(
            [e["op"] for e in self.log.events(session.id)], ["close", "close"]
        )
        self.assertEqual(self.log.get(session.id).status, CLOSED)

    def test_a_failed_session_reports_failed(self):
        session = self.log.open(KEY, IDENTITY)
        self.log.seal(session.id, outcome=FAILED, detail="build failed")
        self.assertEqual(self.log.get(session.id).status, FAILED)
        self.assertTrue(self.log.get(session.id).closed)

    def test_the_session_row_never_changes(self):
        session = self.log.open(KEY, IDENTITY, ref="refs/heads/master")
        before = self.log.get(session.id)
        self.log.append(session.id, RESTORE, detail="miss:x")
        self.log.seal(session.id)
        after = self.log.get(session.id)
        for field in ("id", "identity", "ref", "started_at", "run_id", "job"):
            self.assertEqual(getattr(before, field), getattr(after, field), field)
        self.assertEqual(before.key, after.key)

    def test_the_log_exposes_no_update_path(self):
        # Append-only is a property of the API, not a convention: the only
        # writers are INSERTs from open()/append().
        self.assertFalse(hasattr(self.log, "update"))
        self.assertFalse(hasattr(self.log, "set_status"))

    def test_sessions_are_listed_newest_first_and_filterable_by_repo(self):
        first = self.log.open(KEY, IDENTITY)
        other = self.log.open(Key("FastLED/FastLED", "app", "linux-x64"), IDENTITY)
        self.assertEqual({s.id for s in self.log.sessions()}, {first.id, other.id})
        filtered = self.log.sessions(repo="FastLED/FastLED")
        self.assertEqual([s.id for s in filtered], [other.id])

    def test_the_database_survives_reopening(self):
        session = self.log.open(KEY, IDENTITY)
        self.log.append(session.id, RESTORE, detail="miss:none")
        self.log.close_connection()
        reopened = SessionLog(self.root / "sqlite.db")
        try:
            self.assertEqual(
                [e["detail"] for e in reopened.events(session.id)], ["miss:none"]
            )
        finally:
            reopened.close_connection()
        self.log = SessionLog(self.root / "sqlite.db")

    def test_an_unknown_session_is_not_invented(self):
        self.assertIsNone(self.log.get("nope"))
        with self.assertRaises(KeyError):
            self.log.seal("nope")


class ReplayTest(unittest.TestCase):
    """Replay must reproduce a session's decisions from the store's own history."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = FileStore(self.root / "store")
        self.log = SessionLog(self.root / "sqlite.db")
        self.payload = self.root / "layer.bin"
        self.payload.write_bytes(b"layer-bytes" * 100)

    def tearDown(self):
        self.log.close_connection()
        self._tmp.cleanup()

    def publish(self, session_id: str, identity: str = IDENTITY) -> Manifest:
        entries = self.store.entries(KEY)
        plan = plan_push(entries, KEY, identity)
        manifest = Manifest(
            key=KEY,
            seq=plan.seq,
            identity=identity,
            kind=plan.kind,
            parent=plan.parent.digest if plan.parent else None,
            digest=digest_of(self.payload),
            bytes=self.payload.stat().st_size,
            created_at="2026-11-24T00:00:00Z",
        )
        self.store.put_blob(manifest, self.payload)
        self.store.put_manifest(manifest)
        record_publish(self.log, session_id, plan, manifest)
        return manifest

    def test_a_session_that_starts_cold_publishes_and_warms_up_replays_identically(
        self,
    ):
        session = self.log.open(KEY, IDENTITY)
        record_restore(
            self.log, session.id, plan_restore(self.store.entries(KEY), IDENTITY)
        )
        self.publish(session.id)
        record_restore(
            self.log, session.id, plan_restore(self.store.entries(KEY), IDENTITY)
        )
        self.log.seal(session.id)

        events = self.log.events(session.id)
        self.assertEqual(events[0]["outcome"], "miss")
        self.assertTrue(events[2]["detail"].startswith("s000001:"))

        report = replay(self.log, self.store, session.id)
        self.assertTrue(report.identical, [s for s in report.divergent])
        self.assertEqual(
            [s.op for s in report.steps], [RESTORE, PUBLISH, RESTORE, "close"]
        )

    def test_a_live_replay_also_reads_the_layers_it_would_apply(self):
        session = self.log.open(KEY, IDENTITY)
        self.publish(session.id)
        record_restore(
            self.log, session.id, plan_restore(self.store.entries(KEY), IDENTITY)
        )
        report = replay(self.log, self.store, session.id, dry_run=False)
        self.assertTrue(report.identical)
        restore_step = next(s for s in report.steps if s.op == RESTORE)
        self.assertIn("read 1 layer(s)", restore_step.note)

    def test_a_decision_that_no_longer_holds_is_reported_not_hidden(self):
        session = self.log.open(KEY, IDENTITY)
        self.publish(session.id)
        # A recorded restore that the store disagrees with.
        self.log.append(session.id, RESTORE, detail="s000009:" + "sha256:" + "0" * 64)
        report = replay(self.log, self.store, session.id)
        self.assertFalse(report.identical)
        self.assertEqual(len(report.divergent), 1)
        self.assertIn("s000001:", report.divergent[0].replayed)

    def test_a_publish_removed_from_the_store_is_called_out(self):
        session = self.log.open(KEY, IDENTITY)
        manifest = self.publish(session.id)
        self.store._blob(manifest.digest).unlink()
        (self.root / "store" / KEY.prefix / "s000001.json").unlink()
        report = replay(self.log, self.store, session.id)
        publish_step = next(s for s in report.steps if s.op == PUBLISH)
        self.assertIn("no longer in the store", publish_step.note)

    def test_signatures_are_stable_strings(self):
        plan = plan_restore(self.store.entries(KEY), IDENTITY)
        self.assertTrue(restore_signature(plan).startswith("miss:"))
        push = plan_push(self.store.entries(KEY), KEY, IDENTITY)
        self.assertEqual(publish_signature(push), "full s000001 parent=-")


if __name__ == "__main__":
    unittest.main()
