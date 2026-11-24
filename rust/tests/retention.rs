//! Retention conformance: the shared corpus decides what survives.
//!
//! `fixtures/retention.json` is generated from the Python implementation and read
//! by both sides (`tests/test_retention_fixtures.py` is the other half). A case one
//! implementation answers differently is a bug in one of them.

use cachelib::retention::{chains_of, plan_sweep};
use cachelib::{Key, Manifest, ManifestError};
use serde_json::Value;

fn corpus() -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("fixtures")
        .join("retention.json");
    let text = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_str(&text).expect("fixtures/retention.json is valid JSON")
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

fn seqs(case: &Value, field: &str) -> Vec<u32> {
    case["expect"][field]
        .as_array()
        .expect("expect.<field> is an array")
        .iter()
        .map(|seq| seq.as_u64().unwrap() as u32)
        .collect()
}

#[test]
fn every_retention_case_matches_the_python_implementation() {
    let doc = corpus();
    let key = key_of(&doc);
    let cases = doc["cases"].as_array().expect("cases is an array");
    assert!(cases.len() >= 7, "the corpus must not shrink silently");

    for case in cases {
        let name = case["name"].as_str().unwrap();
        let entries = manifests_of(&key, case);
        let keep = case["keep"].as_u64().expect("keep") as usize;
        let max_bytes = case["max_bytes"].as_u64();
        let protect: Vec<String> = case["protect"]
            .as_array()
            .map(|values| {
                values
                    .iter()
                    .map(|value| value.as_str().unwrap().to_string())
                    .collect()
            })
            .unwrap_or_default();

        let plan = plan_sweep(&entries, &key, keep, max_bytes, &protect)
            .unwrap_or_else(|error: ManifestError| panic!("{name}: {error}"));
        let kept: Vec<u32> = plan.keep.iter().map(|manifest| manifest.seq).collect();
        let evicted: Vec<u32> = plan.evict.iter().map(|manifest| manifest.seq).collect();
        assert_eq!(kept, seqs(case, "keep"), "{name}: kept");
        assert_eq!(evicted, seqs(case, "evict"), "{name}: evicted");
    }
}

#[test]
fn chains_split_on_every_full_base() {
    let doc = corpus();
    let key = key_of(&doc);
    let case = doc["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"].as_str() == Some("two chains, the older goes"))
        .expect("fixture case present");
    let entries = manifests_of(&key, case);
    let chains = chains_of(&entries);
    assert_eq!(chains.len(), 2, "one chain per full base");
    assert_eq!(chains[0].base().seq, 1);
    assert_eq!(chains[1].base().seq, 4);
    assert_eq!(chains[1].bytes(), 400 + 500);
}

#[test]
fn keep_must_be_positive() {
    let doc = corpus();
    let key = key_of(&doc);
    let entries = manifests_of(&key, &doc["cases"][0]);
    assert!(plan_sweep(&entries, &key, 0, None, &[]).is_err());
}

#[test]
fn an_empty_key_plans_nothing() {
    let doc = corpus();
    let key = key_of(&doc);
    let plan = plan_sweep(&[], &key, 2, None, &[]).unwrap();
    assert!(plan.empty());
    assert!(plan.keep.is_empty());
}
