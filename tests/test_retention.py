"""Retention must bound the store without breaking a chain or a neighbour's cache."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cachelib.manifest import DELTA, FULL, Key, Manifest
from cachelib.retention import (
    apply_sweep,
    chains_of,
    orphan_blobs,
    plan_sweep,
    sweep_all,
)
from cachelib.store import FileStore, digest_of

KEY = Key("obs-rust/obs-studio", "app", "linux-x64")
OTHER = Key("FastLED/FastLED", "app", "linux-x64")


def dig(n: int) -> str:
    return "sha256:" + f"{n:064x}"


def chain_entries(
    seqs: list[int],
    identity: str = "id-a",
    key: Key = KEY,
    bases: set[int] | None = None,
) -> list[Manifest]:
    """A chain as a producer builds it: a base, then deltas."""
    bases = bases if bases is not None else {seqs[0]}
    built: list[Manifest] = []
    for seq in seqs:
        is_base = seq in bases or not built
        built.append(
            Manifest(
                key=key,
                seq=seq,
                identity=identity,
                kind=FULL if is_base else DELTA,
                parent=None if is_base else dig(seq - 1),
                digest=dig(seq),
                bytes=100 * seq,
                created_at="2026-11-24T00:00:00Z",
            )
        )
    return built


class ChainSplitTest(unittest.TestCase):
    def test_a_chain_runs_from_a_base_to_just_before_the_next(self):
        entries = chain_entries([1, 2, 3, 4, 5, 6], bases={1, 4})
        chains = chains_of(entries)
        self.assertEqual(
            [[layer.seq for layer in chain.layers] for chain in chains],
            [[1, 2, 3], [4, 5, 6]],
        )
        self.assertEqual(chains[0].base.seq, 1)
        self.assertEqual(chains[1].tip.seq, 6)
        self.assertEqual(chains[1].bytes, 1500)

    def test_a_single_base_is_one_chain(self):
        self.assertEqual(len(chains_of(chain_entries([1]))), 1)
        self.assertEqual(chains_of([]), [])


class PlanSweepTest(unittest.TestCase):
    def test_an_empty_key_plans_nothing(self):
        plan = plan_sweep([], KEY)
        self.assertTrue(plan.empty)
        self.assertEqual(plan.keep, [])

    def test_the_oldest_chain_goes_and_the_newest_stays_whole(self):
        entries = chain_entries([1, 2, 3, 4, 5], bases={1, 4})
        plan = plan_sweep(entries, KEY, keep=1)
        self.assertEqual([m.seq for m in plan.keep], [4, 5])
        self.assertEqual([m.seq for m in plan.evict], [1, 2, 3])

    def test_two_chains_are_kept_when_asked_for(self):
        entries = chain_entries([1, 2, 3, 4, 5], bases={1, 4})
        plan = plan_sweep(entries, KEY, keep=2)
        self.assertTrue(plan.empty)
        self.assertEqual(len(plan.keep), 5)

    def test_a_protected_digest_keeps_its_whole_chain(self):
        entries = chain_entries([1, 2, 3, 4, 5], bases={1, 4})
        plan = plan_sweep(entries, KEY, keep=1, protect={dig(2)})
        kept = {m.seq for m in plan.keep}
        self.assertTrue({1, 2, 3, 4, 5} <= kept, "the protected chain needs its base")
        self.assertTrue(plan.empty)
        self.assertIn("protected", plan.reasons[1])

    def test_identities_are_trimmed_independently(self):
        busy = chain_entries([1, 2, 3, 4, 5, 6], identity="id-a", bases={1, 3, 5})
        quiet = chain_entries([7], identity="id-b")
        plan = plan_sweep(busy + quiet, KEY, keep=1)
        kept = {m.seq for m in plan.keep}
        self.assertEqual(kept, {5, 6, 7}, "one chain per identity, newest first")
        self.assertTrue({1, 2, 3, 4} <= {m.seq for m in plan.evict})

    def test_a_size_cap_drops_oldest_chains_but_never_the_newest(self):
        # chains of 300, 700 and 1100 bytes; 2100 total against a 1300 cap.
        entries = chain_entries([1, 2, 3, 4, 5, 6], bases={1, 3, 5})
        plan = plan_sweep(entries, KEY, keep=3, max_bytes=1300)
        self.assertLessEqual(plan.kept_bytes(), 1300)
        self.assertEqual(
            {m.seq for m in plan.keep},
            {5, 6},
            "the oldest whole chains go first; the newest stays",
        )
        self.assertEqual({m.seq for m in plan.evict}, {1, 2, 3, 4})

    def test_a_chain_larger_than_the_cap_is_kept_whole(self):
        # One chain, 600 bytes, cap 300: dropping the base would orphan the delta,
        # so nothing is evicted. A cache over its budget that still restores beats
        # a store that silently returns misses.
        entries = chain_entries([1, 2, 3], bases={1})
        plan = plan_sweep(entries, KEY, keep=1, max_bytes=300)
        self.assertTrue(plan.empty)
        self.assertEqual({m.seq for m in plan.keep}, {1, 2, 3})

    def test_keep_must_be_positive(self):
        with self.assertRaises(ValueError):
            plan_sweep(chain_entries([1]), KEY, keep=0)


class ApplySweepTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = FileStore(self.root / "store")

    def tearDown(self):
        self._tmp.cleanup()

    def publish(self, key: Key, entries: list[Manifest]) -> list[Manifest]:
        published = []
        for index, manifest in enumerate(entries):
            payload = self.root / f"payload-{key.family}-{key.platform}-{manifest.seq}-{index}.bin"
            payload.write_bytes(f"{manifest.identity}:{manifest.seq}".encode() * 8)
            stored = Manifest(
                key=key,
                seq=manifest.seq,
                identity=manifest.identity,
                kind=manifest.kind,
                parent=manifest.parent,
                digest=digest_of(payload),
                bytes=payload.stat().st_size,
                created_at="2026-11-24T00:00:00Z",
            )
            self.store.put_blob(stored, payload)
            self.store.put_manifest(stored)
            published.append(stored)
        return published

    def blob_path(self, digest: str) -> Path:
        return self.root / "store" / "blobs" / digest.replace(":", "/", 1)

    def test_a_sweep_deletes_the_planned_entries_and_their_blobs(self):
        self.publish(KEY, chain_entries([1, 2, 3, 4, 5], bases={1, 4}))
        plan = plan_sweep(self.store.entries(KEY), KEY, keep=1)
        self.assertFalse(plan.empty)

        report = apply_sweep(self.store, plan)
        self.assertEqual([item["seq"] for item in report["evicted"]], [1, 2, 3])
        self.assertEqual([m.seq for m in self.store.entries(KEY)], [4, 5])
        for evicted in plan.evict:
            self.assertFalse(
                self.blob_path(evicted.digest).exists(),
                f"blob {evicted.digest} should be gone",
            )
        self.assertEqual(len(report["blobs_deleted"]), 3)

    def test_a_dry_run_deletes_nothing(self):
        self.publish(KEY, chain_entries([1, 2, 3], bases={1}))
        plan = plan_sweep(self.store.entries(KEY), KEY, keep=1, protect={dig(1)})
        before = [m.seq for m in self.store.entries(KEY)]
        report = apply_sweep(self.store, plan, dry_run=True)
        self.assertEqual([m.seq for m in self.store.entries(KEY)], before)
        self.assertEqual(report["blobs_deleted"], [])

    def test_a_blob_shared_with_another_key_is_never_deleted(self):
        published = self.publish(KEY, chain_entries([1, 2], bases={1}))
        shared = published[0]
        other = Manifest(
            key=OTHER,
            seq=1,
            identity="id-a",
            kind=FULL,
            digest=shared.digest,
            bytes=shared.bytes,
            created_at="2026-11-24T00:00:00Z",
        )
        self.store.put_manifest(other)  # same bytes, different key
        blob = self.blob_path(shared.digest)
        self.assertTrue(blob.exists())

        plan = plan_sweep(self.store.entries(KEY), KEY, keep=1)
        # Chain of one base+delta: keep=1 keeps it all, so force a cap that evicts it.
        plan = plan_sweep(self.store.entries(KEY), KEY, keep=1, max_bytes=0)
        apply_sweep(self.store, plan)
        self.assertTrue(blob.exists(), "another key still names this blob")

    def test_evicting_everything_orphans_every_blob(self):
        self.publish(KEY, chain_entries([1, 2], bases={1}))
        entries = self.store.entries(KEY)
        self.assertEqual(orphan_blobs(self.store, []), [])
        # The evicted manifests are still on disk when the sweep asks, so they
        # must not count as their own references (a leak, not a safety net).
        self.assertEqual(len(orphan_blobs(self.store, entries)), 2)


class SweepAllTest(unittest.TestCase):
    """The scheduled entry point: every key, dry by default."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = FileStore(self.root / "store")

    def tearDown(self):
        self._tmp.cleanup()

    def publish(self, key: Key, seqs: list[int], bases: set[int], identity: str = "id-a"):
        for manifest in chain_entries(seqs, identity=identity, key=key, bases=bases):
            payload = self.root / f"{key.repo.replace('/', '-')}-{key.platform}-{manifest.seq}.bin"
            payload.write_bytes(f"{identity}:{manifest.seq}".encode() * 8)
            stored = Manifest(
                key=key,
                seq=manifest.seq,
                identity=manifest.identity,
                kind=manifest.kind,
                parent=manifest.parent,
                digest=digest_of(payload),
                bytes=payload.stat().st_size,
                created_at="2026-11-24T00:00:00Z",
            )
            self.store.put_blob(stored, payload)
            self.store.put_manifest(stored)

    def test_sweep_all_covers_every_key(self):
        self.publish(KEY, [1, 2], {1})
        self.publish(OTHER, [3], {3})
        reports = sweep_all(self.store, keep=1, dry_run=True)
        self.assertTrue(reports, "a dry run still reports the keys it would touch")
        self.assertEqual({report["key"] for report in reports}, {KEY.prefix, OTHER.prefix})

    def test_sweep_all_defaults_to_dry_and_deletes_nothing(self):
        self.publish(KEY, [1, 2], {1})
        self.publish(KEY, [3], {3})
        before = [m.seq for m in self.store.entries(KEY)]
        sweep_all(self.store, keep=1)
        self.assertEqual([m.seq for m in self.store.entries(KEY)], before)

        reports = sweep_all(self.store, keep=1, dry_run=False)
        self.assertEqual(len(reports), 1)
        self.assertEqual([m.seq for m in self.store.entries(KEY)], [3])

    def test_an_untouched_key_is_still_reported(self):
        # A sweep reports the map of the store, including keys it leaves alone.
        self.publish(KEY, [1], {1})
        reports = sweep_all(self.store, keep=5, dry_run=True)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["evicted"], [])

    def test_an_empty_store_reports_nothing(self):
        self.assertEqual(sweep_all(self.store, keep=2), [])


if __name__ == "__main__":
    unittest.main()
