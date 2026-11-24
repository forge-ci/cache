"""The corpus the Rust crate must also satisfy.

`fixtures/conformance.json` is the contract between the two implementations
(forge-ci/cache#6). This side pins the expectations against the Python
implementation; `rust/tests/conformance.rs` asserts the same file. If only one
of them passes, the implementations have drifted.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from cachelib.chain import MAX_DELTA_CHAIN, plan_push, plan_restore
from cachelib.manifest import Key, Manifest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "conformance.json"


def load() -> dict:
    return json.loads(FIXTURES.read_text())


def key_of(doc: dict) -> Key:
    return Key(**doc["key"])


def manifests_of(key: Key, case: dict) -> list[Manifest]:
    return [Manifest(key=key, **entry) for entry in case["manifests"]]


class CorpusTest(unittest.TestCase):
    def setUp(self):
        self.doc = load()
        self.key = key_of(self.doc)

    def test_the_corpus_is_shared_and_does_not_shrink_silently(self):
        self.assertGreaterEqual(len(self.doc["cases"]), 7)
        self.assertEqual(self.doc["max_delta_chain"], MAX_DELTA_CHAIN)
        # The Rust test reads this same file, from the same path.
        self.assertEqual(self.doc["key"]["repo"], "obs-rust/obs-studio")

    def test_every_publish_expectation_holds(self):
        for case in self.doc["cases"]:
            manifests = manifests_of(self.key, case)
            push = case.get("push")
            if not push:
                continue
            with self.subTest(case=case["name"]):
                plan = plan_push(manifests, self.key, push["identity"])
                expect = push["expect"]
                self.assertEqual(plan.kind, expect["kind"])
                self.assertEqual(plan.seq, expect["seq"])
                if expect["parent"] is None:
                    self.assertIsNone(plan.parent)
                else:
                    parent = next(m for m in manifests if m.seq == expect["parent"])
                    self.assertEqual(plan.parent.digest, parent.digest)

    def test_every_restore_expectation_holds(self):
        for case in self.doc["cases"]:
            manifests = manifests_of(self.key, case)
            restore = case.get("restore")
            if not restore:
                continue
            with self.subTest(case=case["name"]):
                plan = plan_restore(manifests, restore["identity"])
                self.assertEqual(plan.hit, restore["expect"]["hit"])
                self.assertEqual([layer.seq for layer in plan.layers], restore["expect"]["layers"])

    def test_every_pin_expectation_holds(self):
        for case in self.doc["cases"]:
            pin = case.get("pin")
            if not pin:
                continue
            with self.subTest(case=case["name"]):
                manifests = manifests_of(self.key, case)
                plan = plan_restore(
                    manifests, case["restore"]["identity"], pinned_digest=pin["digest"]
                )
                self.assertEqual(plan.hit, pin["expect"]["hit"])
                self.assertEqual([layer.seq for layer in plan.layers], pin["expect"]["layers"])

    def test_the_wire_format_keeps_its_field_names(self):
        case = self.doc["cases"][1]
        manifest = manifests_of(self.key, case)[0]
        text = manifest.to_json()
        for field in (
            '"seq"',
            '"identity"',
            '"kind"',
            '"digest"',
            '"bytes"',
            '"created_at"',
            '"parent"',
            '"source_sha"',
            '"run_id"',
            '"tool"',
            '"schema"',
        ):
            self.assertIn(field, text)


if __name__ == "__main__":
    unittest.main()
