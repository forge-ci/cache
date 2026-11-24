"""The client: a session in five calls, and misses that are not exceptions."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import cache_server
from cachelib.client import CacheClient, CacheError

KEY = ("obs-rust/obs-studio", "app", "linux-x64")
IDENTITY = "rustc1.98.1-linux-x64-debug"
PAYLOAD = b"client-layer" * 50


class ClientTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.state = cache_server.State(root / "store", root / "sqlite.db")
        self.server = cache_server.make_server(self.state)
        self.client = CacheClient("http://127.0.0.1:%d" % self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.workdir = root / "work"
        self.workdir.mkdir()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def open(self, identity: str = IDENTITY) -> str:
        return self.client.open(*KEY, identity, ref="refs/heads/master", run_id="7")

    def test_health_is_reachable(self):
        self.assertTrue(self.client.health()["ok"])

    def test_a_cold_cache_is_a_result_not_an_exception(self):
        session = self.open()
        decision = self.client.restore(session)
        self.assertFalse(decision.hit)
        self.assertEqual(decision.to_download, [])
        self.assertTrue(decision.signature.startswith("miss:"))

    def test_publish_then_restore_downloads_exactly_what_went_up(self):
        session = self.open()
        published = self.client.publish(session, PAYLOAD)
        self.assertEqual(published["kind"], "full")

        decision = self.client.restore(session)
        self.assertTrue(decision.hit)
        self.assertEqual(decision.bytes, len(PAYLOAD))

        restored = Path(self.client.download(decision.layers[0].digest, self.workdir / "layer.bin"))
        self.assertEqual(restored.read_bytes(), PAYLOAD)

    def test_two_publishes_build_a_chain_and_the_restore_names_both(self):
        session = self.open()
        self.client.publish(session, PAYLOAD)
        self.client.publish(session, PAYLOAD + b"v2")
        decision = self.client.restore(session)
        self.assertEqual([layer.seq for layer in decision.layers], [1, 2])
        self.assertEqual([layer.kind for layer in decision.layers], ["full", "delta"])

    def test_a_closed_session_refuses_work_with_409(self):
        session = self.open()
        self.client.close(session)
        with self.assertRaises(CacheError) as raised:
            self.client.publish(session, PAYLOAD)
        self.assertEqual(raised.exception.status, 409)

    def test_an_unknown_session_is_a_404_with_the_server_message(self):
        with self.assertRaises(CacheError) as raised:
            self.client.restore("0" * 32)
        self.assertEqual(raised.exception.status, 404)
        self.assertIn("no session", raised.exception.message)

    def test_a_missing_blob_is_a_404(self):
        with self.assertRaises(CacheError) as raised:
            self.client.download("sha256:" + "2" * 64, self.workdir / "nope")
        self.assertEqual(raised.exception.status, 404)

    def test_replay_and_session_reads_round_trip(self):
        session = self.open()
        self.client.restore(session)
        self.client.publish(session, PAYLOAD)
        report = self.client.replay(session)
        self.assertTrue(report["identical"])

        detail = self.client.session(session)
        self.assertEqual([e["op"] for e in detail["events"]], ["restore", "publish"])
        self.assertEqual([s["id"] for s in self.client.sessions(repo=KEY[0])], [session])


if __name__ == "__main__":
    unittest.main()
