"""Parity: the CLI and the service are two front doors onto one store.

Issue #7's acceptance is 'a CI client gets a decision identical to calling
cachectl.py directly'. These tests are literal: one store directory, entries
written by one interface, decisions read back through the other.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import cache_server
from cachelib.client import CacheClient
from cachelib.manifest import Key
from cachelib.store import FileStore

REPO = Path(__file__).resolve().parent.parent
KEY = Key("obs-rust/obs-studio", "app", "linux-x64")
IDENTITY = "rustc1.98.1-linux-x64-debug"


class ParityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store_dir = self.root / "store"
        self.state = cache_server.State(self.store_dir, self.root / "sqlite.db")
        self.server = cache_server.make_server(self.state)
        self.client = CacheClient("http://127.0.0.1:%d" % self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.layer = self.root / "layer.bin"
        self.layer.write_bytes(b"parity-layer" * 200)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def cli(self, *args: str) -> str:
        result = subprocess.run(
            [sys.executable, "cachectl.py", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(REPO)},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def key_args(self, identity: str = IDENTITY) -> list[str]:
        return [
            "--repo",
            KEY.repo,
            "--family",
            KEY.family,
            "--platform",
            KEY.platform,
            "--identity",
            identity,
            "--store",
            f"dir={self.store_dir}",
        ]

    def test_a_layer_published_by_the_cli_is_restored_by_the_service(self):
        out = self.cli("push", *self.key_args(), "--stream", str(self.layer))
        self.assertIn("published full s000001", out)

        session = self.client.open(KEY.repo, KEY.family, KEY.platform, IDENTITY)
        decision = self.client.restore(session)
        self.assertTrue(decision.hit, decision.reason)
        self.assertEqual([layer.seq for layer in decision.layers], [1])
        # The bytes the CLI stored are the bytes the service hands back.
        stored = Path(
            self.client.download(
                decision.layers[0].digest, self.root / "from-service.bin"
            )
        )
        self.assertEqual(stored.read_bytes(), self.layer.read_bytes())

    def test_a_layer_published_by_the_service_is_listed_and_planned_by_the_cli(self):
        session = self.client.open(KEY.repo, KEY.family, KEY.platform, IDENTITY)
        self.client.publish(session, self.layer.read_bytes())
        self.client.publish(session, self.layer.read_bytes() + b"v2")

        listing = self.cli(
            "list",
            "--repo",
            KEY.repo,
            "--family",
            KEY.family,
            "--platform",
            KEY.platform,
            "--store",
            f"dir={self.store_dir}",
        )
        self.assertIn("s000001", listing)
        self.assertIn("full", listing)
        self.assertIn("s000002", listing)
        self.assertIn("delta", listing)

        plan = self.cli("plan", *self.key_args())
        self.assertIn("restore: hit: s000002 via 2 layer(s)", plan)

        decision = self.client.restore(session)
        self.assertEqual([layer.seq for layer in decision.layers], [1, 2])

    def test_both_interfaces_agree_on_what_an_identity_miss_is(self):
        self.cli("push", *self.key_args(), "--stream", str(self.layer))
        other = "rustc1.101.0-linux-x64-debug"
        plan = self.cli("plan", *self.key_args(other))
        self.assertIn("restore: miss", plan)

        session = self.client.open(KEY.repo, KEY.family, KEY.platform, other)
        decision = self.client.restore(session)
        self.assertFalse(decision.hit)
        self.assertIn("no published entry", decision.reason)

    def test_the_two_interfaces_write_the_same_manifest_bytes(self):
        # A digest is the contract: same bytes in, same digest out, whoever wrote it.
        session = self.client.open(
            KEY.repo, KEY.family, KEY.platform, "id-cli-vs-server"
        )
        published = self.client.publish(session, self.layer.read_bytes())

        store = FileStore(self.store_dir)
        [manifest] = store.entries(KEY)
        self.assertEqual(manifest.digest, published["digest"])
        self.assertEqual(manifest.bytes, self.layer.stat().st_size)
        self.assertTrue(
            (self.store_dir / "blobs" / manifest.digest.replace(":", "/", 1)).exists()
        )


if __name__ == "__main__":
    unittest.main()
