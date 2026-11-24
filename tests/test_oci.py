"""The OCI backend against a real registry, when one is offered.

Skipped unless `CACHECTL_TEST_REGISTRY` points at a registry that accepts
anonymous pushes (a localhost `registry:2` in CI). The point of this test is that
the OCI path stops being a specification: it uploads, tags, lists and downloads
through the same code the fleet will use.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from cachelib.manifest import FULL, Key, Manifest, ManifestError
from cachelib.store import OciStore, Registry, digest_of

ENDPOINT = os.environ.get("CACHECTL_TEST_REGISTRY", "")
SCHEME = os.environ.get("CACHECTL_TEST_REGISTRY_SCHEME", "http")


def registry() -> Registry:
    host = ENDPOINT or "127.0.0.1:5001"
    return Registry(host=host, namespace="", token="", scheme=SCHEME)


@unittest.skipUnless(ENDPOINT, "set CACHECTL_TEST_REGISTRY=host:port to run against a registry")
class OciRoundTripTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # A unique repo per run: registries keep blobs and tags between runs, and
        # a shared tag would make "seq 1" ambiguous.
        self.key = Key("obs-rust/obs-studio", "app", f"linux-x64-{uuid.uuid4().hex[:8]}")
        self.store = OciStore(registry(), self.key)
        self.payload = self.root / "layer.bin"
        self.payload.write_bytes(b"oci-layer" * 500)

    def tearDown(self):
        self._tmp.cleanup()

    def publish(self, seq: int, parent: Manifest | None = None) -> Manifest:
        manifest = Manifest(
            key=self.key,
            seq=seq,
            identity="fc-1-linux-x64-debug-1.98.1",
            kind=FULL if parent is None else "delta",
            parent=parent.digest if parent else None,
            digest=digest_of(self.payload),
            bytes=self.payload.stat().st_size,
            created_at="2026-11-24T00:00:00Z",
            ref="refs/heads/master",
            run_id="1",
        )
        self.store.put_blob(manifest, self.payload)
        return manifest

    def test_a_layer_round_trips_through_the_registry(self):
        published = self.publish(1)
        entries = self.store.entries()
        self.assertEqual([m.seq for m in entries], [1], "the tag must be visible as an entry")
        restored = entries[0]
        self.assertEqual(restored.digest, published.digest)
        self.assertEqual(restored.identity, published.identity)
        self.assertEqual(restored.kind, FULL)
        self.assertEqual(restored.bytes, published.bytes)

        destination = self.root / "from-registry.bin"
        self.store.get_blob(restored, destination)
        self.assertEqual(destination.read_bytes(), self.payload.read_bytes())

    def test_a_second_layer_is_a_delta_of_the_first(self):
        first = self.publish(1)
        second = self.publish(2, parent=first)
        entries = self.store.entries()
        self.assertEqual([m.seq for m in entries], [1, 2])
        self.assertEqual(entries[1].parent, first.digest)
        self.assertEqual(entries[1].kind, "delta")
        self.assertEqual(second.parent, first.digest)

    def test_a_missing_blob_is_reported_as_missing(self):
        ghost = Manifest(
            key=self.key,
            seq=9,
            identity="id",
            kind=FULL,
            digest="sha256:" + "9" * 64,
            bytes=1,
            created_at="2026-11-24T00:00:00Z",
        )
        with self.assertRaises(FileNotFoundError):
            self.store.get_blob(ghost, self.root / "nope.bin")

    def test_the_registry_is_not_a_manifest_store(self):
        manifest = Manifest(
            key=self.key,
            seq=1,
            identity="id",
            kind=FULL,
            digest=digest_of(self.payload),
            bytes=self.payload.stat().st_size,
            created_at="2026-11-24T00:00:00Z",
        )
        with self.assertRaises(ManifestError):
            self.store.put_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
