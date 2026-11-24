//! The embedded store: publish, read back, and refuse anything doubtful.

use cachelib::store::{digest_of, now, FileStore};
use cachelib::{Key, Kind};
use std::fs;
use std::path::PathBuf;

fn tempdir(name: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!("cachelib-store-{}-{}", std::process::id(), name));
    let _ = fs::remove_dir_all(&base);
    fs::create_dir_all(&base).unwrap();
    base
}

fn key() -> Key {
    Key::new("obs-rust/obs-studio", "app", "linux-x64").unwrap()
}

fn layer(dir: &PathBuf, name: &str, bytes: &[u8]) -> PathBuf {
    let path = dir.join(name);
    fs::write(&path, bytes).unwrap();
    path
}

#[test]
fn a_published_layer_reads_back_byte_for_byte() {
    let root = tempdir("roundtrip");
    let store = FileStore::open(root.join("store"));
    let stream = layer(&root, "layer.bin", b"embedded-mode-layer");

    let manifest = store.publish(&key(), "id-a", &stream).unwrap();
    assert_eq!(manifest.kind, Kind::Full);
    assert_eq!(manifest.seq, 1);
    assert_eq!(manifest.bytes, 19);
    assert_eq!(manifest.digest, digest_of(&stream).unwrap());

    let entries = store.entries(&key()).unwrap();
    assert_eq!(entries.len(), 1);

    let restored = root.join("restored.bin");
    store.get_blob(&entries[0], &restored).unwrap();
    assert_eq!(fs::read(&restored).unwrap(), fs::read(&stream).unwrap());
}

#[test]
fn a_second_publish_in_the_same_identity_is_a_delta() {
    let root = tempdir("delta");
    let store = FileStore::open(root.join("store"));
    store.publish(&key(), "id-a", &layer(&root, "a.bin", b"one")).unwrap();
    let second = store
        .publish(&key(), "id-a", &layer(&root, "b.bin", b"two"))
        .unwrap();
    assert_eq!(second.kind, Kind::Delta);
    assert_eq!(second.seq, 2);
    assert!(second.parent.is_some());
    assert_eq!(store.entries(&key()).unwrap().len(), 2);
}

#[test]
fn a_different_identity_starts_its_own_full_base() {
    let root = tempdir("identity");
    let store = FileStore::open(root.join("store"));
    store.publish(&key(), "id-a", &layer(&root, "a.bin", b"one")).unwrap();
    let other = store
        .publish(&key(), "id-b", &layer(&root, "b.bin", b"two"))
        .unwrap();
    assert_eq!(other.kind, Kind::Full);
    assert_eq!(other.seq, 2);
    assert!(store.restore_plan(&key(), "id-c").unwrap().layers.is_empty());
}

#[test]
fn a_blob_that_does_not_match_its_manifest_is_refused() {
    let root = tempdir("mismatch");
    let store = FileStore::open(root.join("store"));
    let stream = layer(&root, "layer.bin", b"expected");
    let manifest = store.publish(&key(), "id-a", &stream).unwrap();

    let tampered = layer(&root, "tampered.bin", b"something else");
    assert!(store.put_blob(&manifest, &tampered).is_err());
}

#[test]
fn a_corrupt_blob_is_deleted_rather_than_returned() {
    let root = tempdir("corrupt");
    let store = FileStore::open(root.join("store"));
    let stream = layer(&root, "layer.bin", b"good bytes");
    let manifest = store.publish(&key(), "id-a", &stream).unwrap();

    let blob = root
        .join("store/blobs/sha256")
        .join(manifest.digest.trim_start_matches("sha256:"));
    fs::write(&blob, b"bad").unwrap();

    let destination = root.join("out.bin");
    assert!(store.get_blob(&manifest, &destination).is_err());
    assert!(!destination.exists(), "a corrupt blob must not be left behind");
}

#[test]
fn two_publishers_cannot_share_a_sequence_number() {
    let root = tempdir("clash");
    let store = FileStore::open(root.join("store"));
    let first = store.publish(&key(), "id-a", &layer(&root, "a.bin", b"one")).unwrap();
    let mut clashing = first.clone();
    clashing.digest = format!("sha256:{}", "c".repeat(64));
    // Same entry name, different content: refused rather than overwritten.
    assert!(store.put_manifest(&clashing).is_err());

    // The same layer again is a no-op, not an error.
    store.put_manifest(&first).unwrap();
    assert_eq!(store.entries(&key()).unwrap().len(), 1);
}

#[test]
fn a_manifest_that_exists_always_names_bytes_that_exist() {
    let root = tempdir("order");
    let store = FileStore::open(root.join("store"));
    // Publishing writes the blob before the manifest, so a restore of anything
    // `entries` returns can never fail on a missing blob.
    let manifest = store
        .publish(&key(), "id-a", &layer(&root, "a.bin", b"bytes"))
        .unwrap();
    for entry in store.entries(&key()).unwrap() {
        let blob = root
            .join("store/blobs/sha256")
            .join(entry.digest.trim_start_matches("sha256:"));
        assert!(blob.exists(), "{} names a missing blob", entry.digest);
    }
    assert_eq!(manifest.seq, 1);
}

#[test]
fn timestamps_are_iso_utc_seconds() {
    let stamp = now();
    assert_eq!(stamp.len(), 20, "{stamp}");
    assert!(stamp.ends_with('Z'));
    assert_eq!(&stamp[4..5], "-");
    assert_eq!(&stamp[10..11], "T");
    assert!(stamp.starts_with("20"), "{stamp}");
}

#[test]
fn an_empty_store_plans_a_full_base_and_reports_a_miss() {
    let root = tempdir("empty");
    let store = FileStore::open(root.join("store"));
    let plan = store.plan(&key(), "id-a").unwrap();
    assert!(plan.is_full());
    assert_eq!(plan.seq, 1);
    assert!(!store.restore_plan(&key(), "id-a").unwrap().hit());
}
