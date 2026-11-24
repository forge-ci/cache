"""Interop: one store directory, written by one implementation, read by the other.

The conformance corpus proves the two sides *decide* identically. This proves they
agree on the bytes on disk, which is the thing that actually breaks: a digest
computed differently, a manifest field spelled differently, or a layout that only
one side can list. The Rust binary is driven through `soldr cargo` (bare `cargo`
is hook-blocked in this estate) and then the same directory is read by the
Python package.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cachelib.manifest import Key, Manifest
from cachelib.store import FileStore, digest_of

ROOT = Path(__file__).resolve().parent.parent
CRATE = ROOT / "rust"
REPO = "obs-rust/obs-studio"
KEY = Key(REPO, "app", "linux-x64")
IDENTITY = "rustc1.98.1-linux-x64-debug"


def driver() -> list[str] | None:
    for candidate in ("soldr", "cargo"):
        if shutil.which(candidate):
            return [candidate, "cargo"] if candidate == "soldr" else [candidate]
    return None


class RustInteropTest(unittest.TestCase):
    binary: Path | None = None

    @classmethod
    def setUpClass(cls):
        command = driver()
        if command is None:
            raise unittest.SkipTest("no soldr/cargo on PATH")
        built = subprocess.run(
            [*command, "build", "--offline", "--quiet"],
            cwd=CRATE,
            capture_output=True,
            text=True,
            env={**os.environ, "CARGO_TERM_COLOR": "never"},
        )
        binary = CRATE / "target" / "debug" / "cachestore"
        if built.returncode != 0 or not binary.exists():
            raise unittest.SkipTest(f"rust build unavailable: {built.stderr[-400:]}")
        cls.binary = binary

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store_dir = self.root / "store"
        self.layer = self.root / "layer.bin"
        self.layer.write_bytes(b"interop-layer" * 300)

    def tearDown(self):
        self._tmp.cleanup()

    def rust(self, *args: str, expect: int = 0) -> str:
        result = subprocess.run(
            [str(self.binary), *args], capture_output=True, text=True
        )
        self.assertEqual(
            result.returncode, expect, f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        return result.stdout

    def key_args(self, identity: str = IDENTITY) -> list[str]:
        return [
            "--store",
            str(self.store_dir),
            "--repo",
            REPO,
            "--family",
            KEY.family,
            "--platform",
            KEY.platform,
            "--identity",
            identity,
        ]

    def test_a_layer_published_by_rust_is_read_by_python(self):
        out = self.rust("push", *self.key_args(), "--stream", str(self.layer))
        self.assertIn("published full s000001", out)

        store = FileStore(self.store_dir)
        entries = store.entries(KEY)
        self.assertEqual([entry.seq for entry in entries], [1])
        self.assertEqual(entries[0].digest, digest_of(self.layer))
        self.assertEqual(entries[0].identity, IDENTITY)

        restored = store.get_blob(entries[0], self.root / "from-python.bin")
        self.assertEqual(restored.read_bytes(), self.layer.read_bytes())

    def test_a_layer_published_by_python_is_listed_and_planned_by_rust(self):
        store = FileStore(self.store_dir)
        first = Manifest(
            key=KEY,
            seq=1,
            identity=IDENTITY,
            kind="full",
            digest=digest_of(self.layer),
            bytes=self.layer.stat().st_size,
            created_at="2026-11-24T00:00:00Z",
        )
        store.put_blob(first, self.layer)
        store.put_manifest(first)

        second_bytes = self.root / "second.bin"
        second_bytes.write_bytes(self.layer.read_bytes() + b"v2")
        second = Manifest(
            key=KEY,
            seq=2,
            identity=IDENTITY,
            kind="delta",
            parent=first.digest,
            digest=digest_of(second_bytes),
            bytes=second_bytes.stat().st_size,
            created_at="2026-11-24T00:00:01Z",
        )
        store.put_blob(second, second_bytes)
        store.put_manifest(second)

        listing = self.rust("list", *self.key_args())
        self.assertIn("s000001", listing)
        self.assertIn("full", listing)
        self.assertIn("s000002", listing)
        self.assertIn("delta", listing)

        plan = self.rust("plan", *self.key_args())
        self.assertIn("restore: hit: s000002 via 2 layer(s)", plan)
        self.assertIn("layers: s000001,s000002", plan)

    def test_rust_pull_returns_the_bytes_python_stored(self):
        store = FileStore(self.store_dir)
        manifest = Manifest(
            key=KEY,
            seq=1,
            identity=IDENTITY,
            kind="full",
            digest=digest_of(self.layer),
            bytes=self.layer.stat().st_size,
            created_at="2026-11-24T00:00:00Z",
        )
        store.put_blob(manifest, self.layer)
        store.put_manifest(manifest)

        destination = self.root / "pulled"
        out = self.rust("pull", *self.key_args(), "--dest", str(destination))
        self.assertIn("restored s000001", out)
        pulled = destination / "layer-000001"
        self.assertTrue(pulled.exists())
        self.assertEqual(pulled.read_bytes(), self.layer.read_bytes())

    def test_both_sides_see_the_same_identity_miss(self):
        self.rust("push", *self.key_args(), "--stream", str(self.layer))
        out = self.rust("plan", *self.key_args("rustc1.101.0-linux-x64-debug"))
        self.assertIn("restore: miss", out)

        from cachelib.chain import plan_restore

        python_plan = plan_restore(
            FileStore(self.store_dir).entries(KEY), "rustc1.101.0-linux-x64-debug"
        )
        self.assertFalse(python_plan.hit)


if __name__ == "__main__":
    unittest.main()
