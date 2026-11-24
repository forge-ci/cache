"""The file store is the reference backend: publish, list, restore, and no lying."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cachelib.manifest import DELTA, FULL, Key, Manifest, ManifestError
from cachelib.store import FileStore, digest_of, repository_name

KEY = Key("obs-rust/obs-studio", "app", "linux-x64")


def layer(
    key: Key,
    seq: int,
    kind: str,
    identity: str = "id",
    parent: str | None = None,
    blob: Path | None = None,
) -> Manifest:
    if blob is None:
        return Manifest(
            key=key,
            seq=seq,
            identity=identity,
            kind=kind,
            parent=parent,
            digest="sha256:" + str(seq) * 64,
            bytes=1,
            created_at="2026-11-24T00:00:00Z",
        )
    return Manifest(
        key=key,
        seq=seq,
        identity=identity,
        kind=kind,
        parent=parent,
        digest=digest_of(blob),
        bytes=blob.stat().st_size,
        created_at="2026-11-24T00:00:00Z",
    )


class FileStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = FileStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_empty_store_lists_nothing_and_does_not_create_directories(self):
        self.assertEqual(self.store.entries(KEY), [])
        self.assertFalse((self.root / KEY.prefix).exists())

    def test_a_published_layer_round_trips_byte_for_byte(self):
        source = self.root / "layer.bin"
        source.write_bytes(b"cache-" * 1000)
        manifest = layer(KEY, 1, FULL, blob=source)
        self.store.put_blob(manifest, source)
        self.store.put_manifest(manifest)

        self.assertEqual([m.seq for m in self.store.entries(KEY)], [1])
        restored = self.store.get_blob(manifest, self.root / "restored.bin")
        self.assertEqual(restored.read_bytes(), source.read_bytes())

    def test_a_blob_whose_bytes_do_not_match_its_manifest_is_refused(self):
        source = self.root / "layer.bin"
        source.write_bytes(b"good")
        manifest = layer(KEY, 1, FULL, blob=source)
        source.write_bytes(b"tampered")
        with self.assertRaises(ManifestError):
            self.store.put_blob(manifest, source)

    def test_a_corrupt_blob_is_deleted_rather_than_returned(self):
        source = self.root / "layer.bin"
        source.write_bytes(b"good")
        manifest = layer(KEY, 1, FULL, blob=source)
        self.store.put_blob(manifest, source)
        self.store._blob(manifest.digest).write_bytes(b"bad")
        with self.assertRaises(ManifestError):
            self.store.get_blob(manifest, self.root / "out.bin")
        self.assertFalse((self.root / "out.bin").exists())

    def test_two_publishers_cannot_share_a_sequence_number(self):
        first = layer(KEY, 1, FULL, blob=self._blob(b"one"))
        self.store.put_manifest(first)
        clashing = layer(KEY, 1, FULL, blob=self._blob(b"two"))
        with self.assertRaises(ManifestError):
            self.store.put_manifest(clashing)

    def test_republishing_the_same_layer_is_idempotent(self):
        manifest = layer(KEY, 1, FULL, blob=self._blob(b"same"))
        self.store.put_manifest(manifest)
        self.store.put_manifest(manifest)
        self.assertEqual(len(self.store.entries(KEY)), 1)

    def test_a_chain_lists_in_publish_order(self):
        base = layer(KEY, 1, FULL, blob=self._blob(b"base"))
        self.store.put_manifest(base)
        delta = layer(KEY, 2, DELTA, parent=base.digest, blob=self._blob(b"delta"))
        self.store.put_manifest(delta)
        self.assertEqual([m.seq for m in self.store.entries(KEY)], [1, 2])

    def test_keys_of_different_families_do_not_collide(self):
        other = Key(KEY.repo, "rust-tests", KEY.platform)
        self.store.put_manifest(layer(KEY, 1, FULL, blob=self._blob(b"a")))
        self.store.put_manifest(layer(other, 1, FULL, blob=self._blob(b"b")))
        self.assertEqual(len(self.store.entries(KEY)), 1)
        self.assertEqual(len(self.store.entries(other)), 1)

    def test_keys_are_found_for_a_nested_owner_name(self):
        # `repo` is `owner/name`, so a key sits four segments deep; a
        # fixed-depth glob used to miss these entirely.
        nested = Key("obs-rust/obs-studio", "app", "linux-x64")
        self.store.put_manifest(layer(nested, 1, FULL, blob=self._blob(b"nested")))
        self.assertEqual([k.prefix for k in self.store.keys()], [nested.prefix])

    def _blob(self, content: bytes) -> Path:
        path = self.root / f"src-{content.decode()}.bin"
        path.write_bytes(content)
        return path


class RepositoryNameTest(unittest.TestCase):
    def test_each_key_gets_its_own_registry_repository(self):
        self.assertEqual(
            repository_name(KEY, "forge-ci"),
            "forge-ci/obs-rust-obs-studio-app-linux-x64",
        )
        self.assertEqual(
            repository_name(Key("FastLED/FastLED", "app", "windows-arm64"), "forge-ci"),
            "forge-ci/fastled-fastled-app-windows-arm64",
        )

    def test_platform_suffixes_survive_the_slug(self):
        self.assertIn("macos-arm64", repository_name(Key("a/b", "app", "macos-arm64"), "ns"))


if __name__ == "__main__":
    unittest.main()
