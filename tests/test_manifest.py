"""Manifests are the index: naming, validation and chain integrity."""

from __future__ import annotations

import json
import unittest

from cachelib.manifest import (
    DELTA,
    FULL,
    Key,
    Manifest,
    ManifestError,
    chain_of,
    for_identity,
    newest,
    next_seq,
    parse_seq,
    seq_name,
)

KEY = Key("obs-rust/obs-studio", "app", "linux-x64")


def dig(char: str) -> str:
    return "sha256:" + char * 64


def manifest(
    seq: int,
    kind: str = FULL,
    identity: str = "id",
    digest: str | None = None,
    parent: str | None = None,
) -> Manifest:
    return Manifest(
        key=KEY,
        seq=seq,
        identity=identity,
        kind=kind,
        digest=dig(str(seq % 10)) if digest is None else digest,
        parent=parent,
        bytes=1,
        created_at="2026-11-24T00:00:00Z",
    )


class NamingTest(unittest.TestCase):
    def test_a_sequence_is_zero_padded_so_names_sort_in_publish_order(self):
        names = [seq_name(seq) for seq in (1, 2, 10, 999)]
        self.assertEqual(names, ["s000001", "s000002", "s000010", "s000999"])
        self.assertEqual(sorted(names), names)

    def test_a_name_round_trips(self):
        self.assertEqual(parse_seq("s000042"), 42)
        self.assertEqual(parse_seq("s000042.json"), 42)

    def test_a_foreign_name_is_not_an_entry(self):
        for name in ("index.json", "latest", "s", "sabc", "000042"):
            with self.assertRaises(ManifestError):
                parse_seq(name)

    def test_sequence_numbers_start_at_one(self):
        with self.assertRaises(ManifestError):
            seq_name(0)

    def test_the_key_names_one_prefix(self):
        self.assertEqual(KEY.prefix, "obs-rust/obs-studio/app/linux-x64")
        self.assertEqual(KEY.entry(7), "obs-rust/obs-studio/app/linux-x64/s000007")
        self.assertEqual(KEY.tag(7), "s000007")

    def test_a_key_cannot_escape_its_prefix(self):
        with self.assertRaises(ManifestError):
            Key("obs-rust/../etc", "app", "linux-x64")
        with self.assertRaises(ManifestError):
            Key("", "app", "linux-x64")


class ValidationTest(unittest.TestCase):
    def test_a_delta_must_name_a_parent(self):
        with self.assertRaises(ManifestError):
            manifest(2, DELTA, parent=None)

    def test_a_full_layer_cannot_have_a_parent(self):
        with self.assertRaises(ManifestError):
            manifest(1, FULL, parent=dig("a"))

    def test_an_unusable_digest_is_refused(self):
        for digest in ("", "sha256:short", "md5:" + "a" * 32, "a" * 64):
            with self.assertRaises(ManifestError):
                manifest(1, digest=digest)

    def test_identity_cannot_be_empty(self):
        with self.assertRaises(ManifestError):
            manifest(1, identity="")

    def test_json_round_trips_and_is_stable(self):
        original = Manifest(
            key=KEY,
            seq=3,
            identity="id",
            kind=DELTA,
            parent=dig("a"),
            digest=dig("b"),
            bytes=17,
            created_at="2026-11-24T00:00:00Z",
            ref="refs/heads/master",
            source_sha="deadbeef",
            run_id="123",
        )
        text = original.to_json()
        self.assertEqual(Manifest.from_json(text), original)
        self.assertEqual(text, original.to_json())
        self.assertEqual(list(json.loads(text)), sorted(json.loads(text)))


class IndexTest(unittest.TestCase):
    def test_newest_is_the_highest_sequence_not_the_last_written(self):
        entries = [manifest(3), manifest(1), manifest(2)]
        self.assertEqual(newest(entries).seq, 3)
        self.assertEqual(next_seq(entries), 4)

    def test_next_seq_starts_at_one(self):
        self.assertEqual(next_seq([]), 1)

    def test_identity_filter_is_exact(self):
        entries = [manifest(1, identity="a"), manifest(2, identity="b")]
        self.assertEqual([m.seq for m in for_identity(entries, "a")], [1])
        self.assertEqual(for_identity(entries, "A"), [])

    def test_the_chain_walks_parents_back_to_the_base(self):
        base = manifest(1, FULL, digest=dig("1"))
        second = manifest(2, DELTA, digest=dig("2"), parent=base.digest)
        third = manifest(3, DELTA, digest=dig("3"), parent=second.digest)
        entries = [third, base, second]  # storage order is irrelevant
        self.assertEqual([m.seq for m in chain_of(entries, third)], [1, 2, 3])

    def test_a_missing_ancestor_breaks_the_chain(self):
        entries = [
            manifest(1, FULL, digest=dig("1")),
            manifest(2, DELTA, digest=dig("2"), parent=dig("9")),
        ]
        self.assertEqual(chain_of(entries, entries[-1]), [])

    def test_a_delta_that_names_itself_is_refused(self):
        loops = Manifest(
            key=KEY,
            seq=2,
            identity="id",
            kind=DELTA,
            digest=dig("2"),
            bytes=1,
            created_at="2026-11-24T00:00:00Z",
            parent=dig("2"),
        )
        self.assertEqual(chain_of([loops], loops), [])


if __name__ == "__main__":
    unittest.main()
