"""The chain model is the whole design: test it without a registry or a disk."""

from __future__ import annotations

import unittest

from cachelib.chain import MAX_DELTA_CHAIN, plan_push, plan_restore
from cachelib.manifest import DELTA, FULL, Key, Manifest

KEY = Key("obs-rust/obs-studio", "app", "linux-x64")


def layer(
    seq: int, kind: str, identity: str = "id-a", parent: str | None = None
) -> Manifest:
    digest = f"sha256:{seq:064x}"
    return Manifest(
        key=KEY,
        seq=seq,
        identity=identity,
        kind=kind,
        digest=digest,
        bytes=100 * seq,
        created_at="2026-11-24T00:00:00Z",
        parent=parent
        if parent is not None
        else (f"sha256:{seq - 1:064x}" if kind == DELTA else None),
    )


def chain(seqs: list[int], identity: str = "id-a") -> list[Manifest]:
    """A chain as a producer would build it: full base first, then deltas."""
    built: list[Manifest] = []
    for index, seq in enumerate(seqs):
        if index == 0:
            kind = FULL
        else:
            deltas = sum(1 for m in built if m.kind == DELTA)
            kind = FULL if deltas >= MAX_DELTA_CHAIN else DELTA
        built.append(layer(seq, kind, identity))
    return built


class PushPolicyTest(unittest.TestCase):
    def test_the_first_publish_is_a_full_base(self):
        plan = plan_push([], KEY, "id-a")
        self.assertEqual((plan.kind, plan.parent, plan.seq), (FULL, None, 1))

    def test_a_second_publish_deltas_from_the_tip(self):
        entries = chain([1])
        plan = plan_push(entries, KEY, "id-a")
        self.assertEqual(plan.kind, DELTA)
        self.assertEqual(plan.parent.digest, entries[-1].digest)
        self.assertEqual(plan.seq, 2)

    def test_the_cap_cuts_a_new_base_instead_of_stacking_forever(self):
        entries = chain([1], identity="id-a")
        for seq in range(2, 2 + MAX_DELTA_CHAIN):
            entries.append(layer(seq, DELTA, "id-a"))
        self.assertEqual(sum(1 for m in entries if m.kind == DELTA), MAX_DELTA_CHAIN)
        plan = plan_push(entries, KEY, "id-a")
        self.assertEqual(plan.kind, FULL)
        self.assertIsNone(plan.parent)
        self.assertIn("cap", plan.reason)

    def test_one_below_the_cap_still_deltas(self):
        entries = [layer(1, FULL)]
        for seq in range(2, 1 + MAX_DELTA_CHAIN):  # MAX-1 deltas
            entries.append(layer(seq, DELTA))
        plan = plan_push(entries, KEY, "id-a")
        self.assertEqual(plan.kind, DELTA)

    def test_a_different_identity_never_deltas_against_this_one(self):
        entries = chain([1, 2, 3], identity="id-a")
        plan = plan_push(entries, KEY, "id-b")
        self.assertEqual(plan.kind, FULL)
        self.assertEqual(plan.seq, 4)
        self.assertIn("first entry", plan.reason)

    def test_an_incomplete_chain_is_not_deltaed_onto(self):
        entries = [layer(1, FULL), layer(2, DELTA)]
        entries[1] = layer(2, DELTA, parent="sha256:" + "f" * 64)  # ancestor gone
        plan = plan_push(entries, KEY, "id-a")
        self.assertEqual(plan.kind, FULL)
        self.assertIn("incomplete", plan.reason)


class RestorePolicyTest(unittest.TestCase):
    def test_a_missing_identity_is_a_miss_not_an_empty_hit(self):
        plan = plan_restore(chain([1, 2]), "id-z")
        self.assertFalse(plan.hit)
        self.assertEqual(plan.layers, [])

    def test_restore_applies_base_first_then_deltas(self):
        entries = chain([1, 2, 3])
        plan = plan_restore(entries, "id-a")
        self.assertTrue(plan.hit)
        self.assertEqual([m.seq for m in plan.layers], [1, 2, 3])
        self.assertEqual(plan.tip.seq, 3)
        self.assertEqual(plan.missing_bytes(), 100 + 200 + 300)

    def test_an_incomplete_chain_restores_nothing(self):
        entries = [layer(1, FULL), layer(2, DELTA), layer(3, DELTA)]
        entries[0] = layer(1, FULL)
        entries[1] = layer(2, DELTA, parent="sha256:" + "e" * 64)
        plan = plan_restore(entries, "id-a")
        self.assertFalse(plan.hit)
        self.assertIn("incomplete", plan.reason)

    def test_a_pin_restores_that_layer_and_its_ancestors(self):
        entries = chain([1, 2, 3])
        plan = plan_restore(entries, "id-a", pinned_digest=entries[1].digest)
        self.assertEqual([m.seq for m in plan.layers], [1, 2])

    def test_a_pin_to_another_identity_is_refused(self):
        entries = chain([1, 2], identity="id-a")
        other = layer(3, DELTA, "id-b")
        plan = plan_restore(entries + [other], "id-a", pinned_digest=other.digest)
        self.assertFalse(plan.hit)

    def test_the_newest_identity_chain_wins(self):
        entries = chain([1, 2], identity="id-a") + chain([3], identity="id-b")
        plan = plan_restore(entries, "id-b")
        self.assertEqual([m.seq for m in plan.layers], [3])


if __name__ == "__main__":
    unittest.main()
