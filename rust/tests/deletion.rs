//! Deletion is the dangerous half: a sweep that removes the wrong bytes turns a
//! working cache into a permanent miss, and a sweep that keeps everything leaks
//! the store. These run against a real directory.

use cachelib::retention::{apply_sweep, orphan_blobs, plan_sweep};
use cachelib::store::{digest_of, FileStore};
use cachelib::{Key, Manifest, Kind};
use std::fs;
use std::path::PathBuf;

fn tempdir(name: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!("cachelib-retention-{}-{}", std::process::id(), name));
    let _ = fs::remove_dir_all(&base);
    fs::create_dir_all(&base).unwrap();
    base
}

fn key() -> Key {
    Key::new("obs-rust/obs-studio", "app", "linux-x64").unwrap()
}

fn other_key() -> Key {
    Key::new("FastLED/FastLED", "app", "linux-x64").unwrap()
}

/// Publish SEQ entries whose kind is decided by BASE, writing real bytes so the
/// store's digest check passes.
fn publish(store: &FileStore, root: &PathBuf, key: &Key, seqs: &[u32], base: u32, identity: &str) -> Vec<Manifest> {
    let mut published: Vec<Manifest> = Vec::new();
    for seq in seqs {
        let payload = root.join(format!("{}-{}-{}.bin", key.family, key.platform, seq));
        fs::write(&payload, format!("{identity}:{seq}")).unwrap();
        let parent = if *seq == base || published.is_empty() {
            None
        } else {
            published.last().map(|manifest: &Manifest| manifest.digest.clone())
        };
        let kind = if parent.is_none() { Kind::Full } else { Kind::Delta };
        let manifest = Manifest::from_value(
            key.clone(),
            serde_json::json!({
                "seq": seq,
                "identity": identity,
                "kind": kind.as_str(),
                "parent": parent,
                "digest": digest_of(&payload).unwrap(),
                "bytes": fs::metadata(&payload).unwrap().len(),
                "created_at": "2026-11-24T00:00:00Z",
            }),
        )
        .unwrap();
        store.put_blob(&manifest, &payload).unwrap();
        store.put_manifest(&manifest).unwrap();
        published.push(manifest);
    }
    published
}

fn blob(store: &FileStore, manifest: &Manifest) -> PathBuf {
    let hex = manifest.digest.trim_start_matches("sha256:");
    store.root().join("blobs").join("sha256").join(hex)
}

#[test]
fn a_sweep_deletes_the_planned_entries_and_their_blobs() {
    let root = tempdir("apply");
    let store = FileStore::open(root.join("store"));
    // A base at 1, then deltas: one chain, so eviction needs a cap or a second chain.
    publish(&store, &root, &key(), &[1, 2], 1, "id-a");
    publish(&store, &root, &key(), &[3, 4], 3, "id-a");   // second chain

    let entries = store.entries(&key()).unwrap();
    let plan = plan_sweep(&entries, &key(), 1, None, &[]).unwrap();
    assert_eq!(plan.evict.iter().map(|m| m.seq).collect::<Vec<_>>(), vec![1, 2]);

    let report = apply_sweep(&store, &plan, false).unwrap();
    assert_eq!(report.evicted, vec![1, 2]);
    assert_eq!(report.blobs_deleted.len(), 2, "both evicted blobs go");
    assert_eq!(store.entries(&key()).unwrap().len(), 2);
    for manifest in &plan.evict {
        assert!(!blob(&store, manifest).exists(), "blob for s{} survived", manifest.seq);
    }
}

#[test]
fn a_dry_run_deletes_nothing() {
    let root = tempdir("dry");
    let store = FileStore::open(root.join("store"));
    publish(&store, &root, &key(), &[1, 2], 1, "id-a");
    publish(&store, &root, &key(), &[3], 3, "id-a");

    let entries = store.entries(&key()).unwrap();
    let plan = plan_sweep(&entries, &key(), 1, None, &[]).unwrap();
    let report = apply_sweep(&store, &plan, true).unwrap();
    assert_eq!(report.blobs_deleted.len(), 0);
    assert_eq!(store.entries(&key()).unwrap().len(), 3, "nothing was removed");
}

#[test]
fn a_blob_shared_with_another_key_is_never_deleted() {
    let root = tempdir("shared");
    let store = FileStore::open(root.join("store"));
    let mine = publish(&store, &root, &key(), &[1], 1, "id-a");
    // A second chain, so the size cap is allowed to drop the first one: the newest
    // chain of an identity is never evicted, by design.
    publish(&store, &root, &key(), &[2], 2, "id-a");
    // The same bytes are published under another key: one blob, two references.
    let shared = Manifest::from_value(
        other_key(),
        serde_json::json!({
            "seq": 1,
            "identity": "id-a",
            "kind": "full",
            "digest": mine[0].digest,
            "bytes": mine[0].bytes,
            "created_at": "2026-11-24T00:00:00Z",
        }),
    )
    .unwrap();
    store.put_manifest(&shared).unwrap();
    let path = blob(&store, &mine[0]);
    assert!(path.exists());

    let entries = store.entries(&key()).unwrap();
    let plan = plan_sweep(&entries, &key(), 1, Some(0), &[]).unwrap();
    assert!(!plan.empty(), "the cap must force the only chain out");
    apply_sweep(&store, &plan, false).unwrap();
    assert!(path.exists(), "another key still names this blob");
}

#[test]
fn evicting_everything_orphans_every_blob() {
    let root = tempdir("orphans");
    let store = FileStore::open(root.join("store"));
    let published = publish(&store, &root, &key(), &[1, 2], 1, "id-a");
    assert_eq!(orphan_blobs(&store, &[]).unwrap().len(), 0);
    // The manifests are still on disk when orphan_blobs runs, so they must not
    // count as their own references (that would leak every blob).
    assert_eq!(orphan_blobs(&store, &published).unwrap().len(), 2);
}

#[test]
fn keys_are_discovered_across_owner_and_repository() {
    let root = tempdir("keys");
    let store = FileStore::open(root.join("store"));
    publish(&store, &root, &key(), &[1], 1, "id-a");
    publish(&store, &root, &other_key(), &[1], 1, "id-a");
    let prefixes: Vec<String> = store.keys().unwrap().iter().map(|key| key.prefix()).collect();
    assert_eq!(prefixes, vec![
        "FastLED/FastLED/app/linux-x64".to_string(),
        "obs-rust/obs-studio/app/linux-x64".to_string(),
    ]);
}
