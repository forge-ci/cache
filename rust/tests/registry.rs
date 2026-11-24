//! The registry backend against a real registry, when one is offered.
//!
//! Skipped unless `CACHECTL_TEST_REGISTRY` points at a registry that accepts
//! anonymous pushes (a localhost `registry:2`, which CI starts). This is the Rust
//! half of the same proof `tests/test_oci.py` makes for Python: the embedded
//! consumer can restore and publish without a Python process.

use cachelib::registry::{OciStore, Registry};
use cachelib::{Key, Kind};
use std::fs;
use std::path::PathBuf;

fn endpoint() -> Option<(String, String)> {
    let host = std::env::var("CACHECTL_TEST_REGISTRY").ok()?;
    let scheme = std::env::var("CACHECTL_TEST_REGISTRY_SCHEME").unwrap_or_else(|_| "http".to_string());
    Some((host, scheme))
}

fn tempdir(name: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!(
        "cachelib-registry-{}-{}-{name}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    fs::create_dir_all(&base).unwrap();
    base
}

fn store(host: &str, scheme: &str, platform: &str) -> OciStore {
    let mut registry = Registry::new(host, "");
    registry.scheme = scheme.to_string();
    OciStore::new(registry, Key::new("obs-rust/obs-studio", "app", platform).unwrap())
}

#[test]
fn a_layer_round_trips_through_the_registry() {
    let Some((host, scheme)) = endpoint() else {
        eprintln!("skipped: set CACHECTL_TEST_REGISTRY=host:port");
        return;
    };
    let root = tempdir("roundtrip");
    // A unique platform per run: registries keep tags between runs, and a shared
    // tag would make "seq 1" ambiguous.
    let platform = format!("linux-x64-{}", std::process::id());
    let store = store(&host, &scheme, &platform);
    let payload = root.join("layer.bin");
    fs::write(&payload, b"rust-registry-layer").unwrap();

    let published = store.publish("fc-1-linux-x64-debug-1.98.1", &payload, 5).unwrap();
    assert_eq!(published.seq, 1);
    assert_eq!(published.kind, Kind::Full);

    let entries = store.entries().unwrap();
    assert_eq!(entries.len(), 1, "the tag must be visible as an entry");
    assert_eq!(entries[0].digest, published.digest);
    assert_eq!(entries[0].identity, published.identity);
    assert_eq!(entries[0].kind, Kind::Full);

    let restored = root.join("restored.bin");
    store.get_blob(&entries[0], &restored).unwrap();
    assert_eq!(fs::read(&restored).unwrap(), fs::read(&payload).unwrap());
}

#[test]
fn a_second_publish_is_a_delta_of_the_first() {
    let Some((host, scheme)) = endpoint() else {
        eprintln!("skipped: set CACHECTL_TEST_REGISTRY=host:port");
        return;
    };
    let root = tempdir("delta");
    let platform = format!("windows-x64-{}", std::process::id());
    let store = store(&host, &scheme, &platform);
    let first = root.join("one.bin");
    fs::write(&first, b"first-layer").unwrap();
    let second = root.join("two.bin");
    fs::write(&second, b"second-layer").unwrap();

    let a = store.publish("id-a", &first, 5).unwrap();
    let b = store.publish("id-a", &second, 5).unwrap();
    assert_eq!(b.kind, Kind::Delta, "the chain must continue, not fork");
    assert_eq!(b.parent.as_deref(), Some(a.digest.as_str()));

    let entries = store.entries().unwrap();
    assert_eq!(entries.iter().map(|m| m.seq).collect::<Vec<_>>(), vec![1, 2]);
    let plan = cachelib::plan_restore(&entries, "id-a", None);
    assert_eq!(plan.layers.iter().map(|m| m.seq).collect::<Vec<_>>(), vec![1, 2]);
}

#[test]
fn an_empty_repository_reads_as_an_empty_store() {
    let Some((host, scheme)) = endpoint() else {
        eprintln!("skipped: set CACHECTL_TEST_REGISTRY=host:port");
        return;
    };
    let platform = format!("macos-arm64-{}", std::process::id());
    let store = store(&host, &scheme, &platform);
    let entries = store.entries().unwrap();
    assert!(entries.is_empty(), "an unseen repository is an empty store, not an error");
    // And the first publish there is a full base.
    let plan = cachelib::plan_push(&entries, &store.key().clone(), "id-a", 5).unwrap();
    assert!(plan.is_full());
    assert_eq!(plan.seq, 1);
}
