"""The retention corpus both implementations must reproduce.

`fixtures/retention.json` is the contract: `rust/tests/retention.rs` asserts the
same expectations. Retention deleting the wrong entry silently turns a working
cache into a permanent miss, so "both sides agree" is worth a test of its own.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from cachelib.manifest import Key, Manifest
from cachelib.retention import chains_of, plan_sweep

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "retention.json"


class RetentionFixtureTest(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(FIXTURE.read_text())
        self.key = Key(**self.doc["key"])

    def manifests(self, case: dict) -> list[Manifest]:
        return [Manifest(key=self.key, **entry) for entry in case["manifests"]]

    def test_the_corpus_is_shared_and_does_not_shrink(self):
        self.assertGreaterEqual(len(self.doc["cases"]), 7)

    def test_every_case_matches(self):
        for case in self.doc["cases"]:
            with self.subTest(case=case["name"]):
                plan = plan_sweep(
                    self.manifests(case),
                    self.key,
                    keep=case["keep"],
                    max_bytes=case["max_bytes"],
                    protect=set(case["protect"]),
                )
                self.assertEqual([m.seq for m in plan.keep], case["expect"]["keep"])
                self.assertEqual([m.seq for m in plan.evict], case["expect"]["evict"])

    def test_chains_split_on_every_full_base(self):
        case = next(c for c in self.doc["cases"] if c["name"] == "two chains, the older goes")
        chains = chains_of(self.manifests(case))
        self.assertEqual([chain.base.seq for chain in chains], [1, 4])
        self.assertEqual(chains[1].bytes, 900)

    def test_an_entry_is_never_evicted_while_its_child_survives(self):
        # The invariant behind chain-granular eviction, checked across the corpus.
        for case in self.doc["cases"]:
            with self.subTest(case=case["name"]):
                plan = plan_sweep(
                    self.manifests(case),
                    self.key,
                    keep=case["keep"],
                    max_bytes=case["max_bytes"],
                    protect=set(case["protect"]),
                )
                kept = {m.seq for m in plan.keep}
                evicted = {m.seq for m in plan.evict}
                for case_entry in case["manifests"]:
                    if case_entry["kind"] != "delta":
                        continue
                    if case_entry["seq"] in kept:
                        self.assertNotIn(
                            case_entry["parent"], evicted, "a kept delta lost its base"
                        )


if __name__ == "__main__":
    unittest.main()
