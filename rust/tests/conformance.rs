//! The two implementations must agree on every case in the shared corpus.
//!
//! `fixtures/conformance.json` is generated once and read by both sides; the
//! Python suite asserts the same expectations in `tests/test_fixtures.py`. If
//! this test and that one both pass, the Rust crate and the Python package
//! decide identically on the corpus -- which is the acceptance criterion in
//! forge-ci/cache#6.

use cachelib::{
    chain_of, for_identity, newest, next_seq, parse_seq, plan_push, plan_restore, seq_name, Key,
    Manifest, Kind, MAX_DELTA_CHAIN,
};
use serde_json::Value;

fn corpus() -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("fixtures")
        .join("conformance.json");
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_str(&text).expect("fixtures/conformance.json is valid JSON")
}

fn key_of(doc: &Value) -> Key {
    let key = &doc["key"];
    Key::new(
        key["repo"].as_str().unwrap(),
        key["family"].as_str().unwrap(),
        key["platform"].as_str().unwrap(),
    )
    .expect("fixture key is valid")
}

fn manifests_of(key: &Key, case: &Value) -> Vec<Manifest> {
    case["manifests"]
        .as_array()
        .expect("manifests is an array")
        .iter()
        .map(|entry| Manifest::from_value(key.clone(), entry.clone()).expect("fixture manifest"))
        .collect()
}

fn expected_seqs(case: &Value, field: &str) -> Vec<u32> {
    case[field]["expect"]["layers"]
        .as_array()
        .expect("layers is an array")
        .iter()
        .map(|seq| seq.as_u64().unwrap() as u32)
        .collect()
}

#[test]
fn every_conformance_case_matches() {
    let doc = corpus();
    assert_eq!(doc["max_delta_chain"].as_u64().unwrap() as usize, MAX_DELTA_CHAIN);
    let key = key_of(&doc);
    let cases = doc["cases"].as_array().expect("cases is an array");
    assert!(cases.len() >= 7, "the corpus must not shrink silently");

    for case in cases {
        let name = case["name"].as_str().unwrap();
        let manifests = manifests_of(&key, case);

        if let Some(push) = case.get("push").filter(|value| !value.is_null()) {
            let identity = push["identity"].as_str().unwrap();
            let expect = &push["expect"];
            let plan = plan_push(&manifests, &key, identity, MAX_DELTA_CHAIN)
                .unwrap_or_else(|error| panic!("{name}: {error}"));
            assert_eq!(
                plan.kind.as_str(),
                expect["kind"].as_str().unwrap(),
                "{name}: publish kind"
            );
            assert_eq!(
                plan.seq,
                expect["seq"].as_u64().unwrap() as u32,
                "{name}: publish seq"
            );
            let expected_parent = expect["parent"].as_u64().map(|seq| seq as u32);
            match expected_parent {
                None => assert!(plan.parent.is_none(), "{name}: expected a base"),
                Some(parent_seq) => {
                    let parent = manifests
                        .iter()
                        .find(|manifest| manifest.seq == parent_seq)
                        .expect("fixture names a real parent");
                    assert_eq!(
                        plan.parent.as_deref(),
                        Some(parent.digest.as_str()),
                        "{name}: parent digest"
                    );
                }
            }
        }

        if let Some(restore) = case.get("restore").filter(|value| !value.is_null()) {
            let identity = restore["identity"].as_str().unwrap();
            let plan = plan_restore(&manifests, identity, None);
            assert_eq!(
                plan.hit(),
                restore["expect"]["hit"].as_bool().unwrap(),
                "{name}: restore hit"
            );
            let seqs: Vec<u32> = plan.layers.iter().map(|layer| layer.seq).collect();
            assert_eq!(seqs, expected_seqs(case, "restore"), "{name}: restore layers");
        }

        if let Some(pin) = case.get("pin").filter(|value| !value.is_null()) {
            let identity = case["restore"]["identity"].as_str().unwrap();
            let digest = pin["digest"].as_str().unwrap();
            let plan = plan_restore(&manifests, identity, Some(digest));
            assert_eq!(
                plan.hit(),
                pin["expect"]["hit"].as_bool().unwrap(),
                "{name}: pin hit"
            );
            let seqs: Vec<u32> = plan.layers.iter().map(|layer| layer.seq).collect();
            assert_eq!(seqs, expected_seqs(case, "pin"), "{name}: pinned layers");
        }
    }
}

#[test]
fn a_manifest_round_trips_the_python_wire_format() {
    let doc = corpus();
    let key = key_of(&doc);
    let case = &doc["cases"][1];
    let manifest = Manifest::from_value(key.clone(), case["manifests"][0].clone()).unwrap();

    let json = serde_json::to_string(&manifest).unwrap();
    let reparsed = Manifest::from_wire(key, &json).unwrap();
    assert_eq!(manifest, reparsed);
    // Field names are part of the contract, not an internal detail.
    for field in [
        "\"seq\"",
        "\"identity\"",
        "\"kind\"",
        "\"digest\"",
        "\"bytes\"",
        "\"created_at\"",
        "\"parent\"",
        "\"source_sha\"",
        "\"run_id\"",
        "\"tool\"",
        "\"schema\"",
    ] {
        assert!(json.contains(field), "wire format is missing {field}");
    }
}

#[test]
fn validation_refuses_what_the_python_side_refuses() {
    let doc = corpus();
    let key = key_of(&doc);
    let mut entry = doc["cases"][1]["manifests"][0].clone();

    // A delta with no parent.
    entry["kind"] = Value::String("delta".to_string());
    assert!(Manifest::from_value(key.clone(), entry.clone()).is_err());

    // A full layer that names a parent.
    entry["kind"] = Value::String("full".to_string());
    entry["parent"] = Value::String(format!("sha256:{}", "a".repeat(64)));
    assert!(Manifest::from_value(key.clone(), entry.clone()).is_err());

    // An unusable digest.
    entry["parent"] = Value::Null;
    entry["digest"] = Value::String("sha256:short".to_string());
    assert!(Manifest::from_value(key.clone(), entry.clone()).is_err());

    // An empty identity is not a compatibility contract.
    entry["digest"] = Value::String(format!("sha256:{}", "b".repeat(64)));
    entry["identity"] = Value::String(String::new());
    assert!(Manifest::from_value(key.clone(), entry).is_err());

    // A key with a path escape is not a prefix.
    assert!(Key::new("obs-rust/../etc", "app", "linux-x64").is_err());
    assert!(Key::new("obs-rust/obs-studio", "app/x", "linux-x64").is_err());
}

#[test]
fn sequence_helpers_match_the_python_names() {
    assert_eq!(seq_name(1).unwrap(), "s000001");
    assert_eq!(seq_name(999).unwrap(), "s000999");
    assert_eq!(parse_seq("s000042").unwrap(), 42);
    assert_eq!(parse_seq("s000042.json").unwrap(), 42);
    assert!(parse_seq("index.json").is_err());
    assert!(parse_seq("sabc").is_err());
    assert!(seq_name(0).is_err());

    let doc = corpus();
    let key = key_of(&doc);
    let manifests = manifests_of(&key, &doc["cases"][1]);
    assert_eq!(next_seq(&manifests), 2);
    assert_eq!(newest(&manifests).unwrap().seq, 1);
    assert_eq!(for_identity(&manifests, "id-a").len(), 1);
    assert!(for_identity(&manifests, "id-b").is_empty());
    assert_eq!(chain_of(&manifests, &manifests[0]).len(), 1);
    assert_eq!(manifests[0].kind, Kind::Full);
}
