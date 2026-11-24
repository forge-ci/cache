//! The reference store, in Rust: content addressed, race-safe, no lock file.
//!
//! Same layout as `cachelib/store.py`, deliberately: `<root>/<repo>/<family>/
//! <platform>/s000001.json` next to `<root>/blobs/sha256/<hex>`. A store written
//! by the Python CLI is readable here and vice versa; `tests/interop.rs` holds
//! that to a real check with the Python tool on the other side.
//!
//! Two properties carry the design:
//!
//! * **The blob precedes the manifest.** A manifest that exists always names
//!   bytes that exist, so a crash mid-publish leaves garbage at worst, never a
//!   dangling entry a restore would trust.
//! * **The sequence number is claimed with a hard link.** Two publishers that
//!   pick the same number race to `link(2)`; the loser gets `AlreadyExists` and
//!   retries at the next number. No lock file, no coordinator, no torn document.

use crate::{
    for_identity, newest, next_seq, order, parse_seq, plan_push, plan_restore, Kind, Key, Manifest,
    ManifestError, PushPlan, RestorePlan, MAX_DELTA_CHAIN,
};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

/// `sha256:<64 hex>` of a file, read in chunks.
pub fn digest_of(path: &Path) -> Result<String, ManifestError> {
    let mut file = fs::File::open(path)
        .map_err(|error| ManifestError(format!("{}: {error}", path.display())))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|error| ManifestError(format!("{}: {error}", path.display())))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("sha256:{:x}", hasher.finalize()))
}

/// The filesystem backend: the reference implementation both sides can read.
#[derive(Debug, Clone)]
pub struct FileStore {
    root: PathBuf,
}

impl FileStore {
    pub fn open(root: impl Into<PathBuf>) -> Self {
        FileStore { root: root.into() }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    fn key_dir(&self, key: &Key) -> PathBuf {
        let mut path = self.root.clone();
        for segment in key.prefix().split('/') {
            path.push(segment);
        }
        path
    }

    fn blob_path(&self, digest: &str) -> PathBuf {
        let hex = digest.strip_prefix("sha256:").unwrap_or(digest);
        self.root.join("blobs").join("sha256").join(hex)
    }

    // -- manifests ---------------------------------------------------------
    /// Every entry for KEY, in publish order. Read order is a directory
    /// listing, so nothing here needs a database.
    pub fn entries(&self, key: &Key) -> Result<Vec<Manifest>, ManifestError> {
        let directory = self.key_dir(key);
        let reader = match fs::read_dir(&directory) {
            Ok(reader) => reader,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => {
                return Err(ManifestError(format!(
                    "{}: {error}",
                    directory.display()
                )))
            }
        };
        let mut found = Vec::new();
        for entry in reader {
            let path = entry
                .map_err(|error| ManifestError(format!("{}: {error}", directory.display())))?
                .path();
            let name = match path.file_name().and_then(|name| name.to_str()) {
                Some(name) => name.to_string(),
                None => continue,
            };
            if !name.ends_with(".json") || parse_seq(&name).is_err() {
                continue;
            }
            let text = fs::read_to_string(&path)
                .map_err(|error| ManifestError(format!("{}: {error}", path.display())))?;
            found.push(Manifest::from_wire(key.clone(), &text)?);
        }
        Ok(order(&found))
    }

    /// Publish a manifest. Fails rather than overwriting a different layer.
    pub fn put_manifest(&self, manifest: &Manifest) -> Result<(), ManifestError> {
        manifest.validate()?;
        let directory = self.key_dir(&manifest.key);
        fs::create_dir_all(&directory)
            .map_err(|error| ManifestError(format!("{}: {error}", directory.display())))?;
        let target = directory.join(format!("{}.json", manifest.key.tag(manifest.seq)?));
        let staging = directory.join(format!(
            "{}.json.tmp-{}",
            manifest.key.tag(manifest.seq)?,
            std::process::id()
        ));
        fs::write(&staging, serde_json::to_string_pretty(manifest)? + "\n")
            .map_err(|error| ManifestError(format!("{}: {error}", staging.display())))?;
        let linked = fs::hard_link(&staging, &target);
        let _ = fs::remove_file(&staging);
        match linked {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                let existing = fs::read_to_string(&target)
                    .map_err(|error| ManifestError(format!("{}: {error}", target.display())))?;
                let existing = Manifest::from_wire(manifest.key.clone(), &existing)?;
                if existing == *manifest {
                    Ok(()) // republishing the same layer is a no-op
                } else {
                    Err(ManifestError(format!(
                        "{} already holds a different layer; publish under the next sequence",
                        target.display()
                    )))
                }
            }
            Err(error) => Err(ManifestError(format!("{}: {error}", target.display()))),
        }
    }

    // -- blobs -------------------------------------------------------------
    /// Store a layer's bytes under the digest the manifest claims.
    pub fn put_blob(&self, manifest: &Manifest, source: &Path) -> Result<(), ManifestError> {
        let actual = digest_of(source)?;
        if actual != manifest.digest {
            return Err(ManifestError(format!(
                "{} hashes to {actual}, manifest says {}",
                source.display(),
                manifest.digest
            )));
        }
        let target = self.blob_path(&manifest.digest);
        if target.exists() {
            return Ok(()); // content addressed: already there
        }
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| ManifestError(format!("{}: {error}", parent.display())))?;
        }
        let staging = target.with_extension(format!("part-{}", std::process::id()));
        fs::copy(source, &staging)
            .map_err(|error| ManifestError(format!("{}: {error}", staging.display())))?;
        fs::rename(&staging, &target)
            .map_err(|error| ManifestError(format!("{}: {error}", target.display())))?;
        Ok(())
    }

    /// Copy a layer's bytes out, verifying the digest on the way.
    pub fn get_blob(&self, manifest: &Manifest, destination: &Path) -> Result<(), ManifestError> {
        let source = self.blob_path(&manifest.digest);
        if !source.exists() {
            return Err(ManifestError(format!(
                "blob {} is not in {}",
                manifest.digest,
                self.root.display()
            )));
        }
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| ManifestError(format!("{}: {error}", parent.display())))?;
        }
        fs::copy(&source, destination)
            .map_err(|error| ManifestError(format!("{}: {error}", destination.display())))?;
        let actual = digest_of(destination)?;
        if actual != manifest.digest {
            let _ = fs::remove_file(destination);
            return Err(ManifestError(format!(
                "blob {} arrived corrupt ({actual})",
                manifest.digest
            )));
        }
        Ok(())
    }

    // -- deletion (retention) ---------------------------------------------
    /// Every key with at least one entry, derived from the manifests themselves.
    ///
    /// A blob is shared by every key that names its digest, so deleting bytes
    /// requires knowing which keys still reference it -- hence a full scan rather
    /// than trusting the one key a sweep started from.
    pub fn keys(&self) -> Result<Vec<Key>, ManifestError> {
        let mut found: Vec<Key> = Vec::new();
        let mut stack = vec![self.root.clone()];
        while let Some(directory) = stack.pop() {
            let reader = match fs::read_dir(&directory) {
                Ok(reader) => reader,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => {
                    return Err(ManifestError(format!("{}: {error}", directory.display())))
                }
            };
            for entry in reader {
                let path = entry
                    .map_err(|error| ManifestError(format!("{}: {error}", directory.display())))?
                    .path();
                if path.is_dir() {
                    stack.push(path);
                    continue;
                }
                let name = match path.file_name().and_then(|name| name.to_str()) {
                    Some(name) => name.to_string(),
                    None => continue,
                };
                if parse_seq(&name).is_err() {
                    continue;
                }
                let Some(parent) = path.parent() else { continue };
                let Ok(relative) = parent.strip_prefix(&self.root) else { continue };
                let parts: Vec<&str> = relative
                    .components()
                    .filter_map(|component| component.as_os_str().to_str())
                    .collect();
                if parts.len() < 3 {
                    continue;
                }
                let repo = parts[..parts.len() - 2].join("/");
                let key = Key::new(&repo, parts[parts.len() - 2], parts[parts.len() - 1])?;
                if !found.contains(&key) {
                    found.push(key);
                }
            }
        }
        found.sort_by(|a, b| a.prefix().cmp(&b.prefix()));
        Ok(found)
    }

    /// Remove one entry's manifest. Missing is not an error: a sweep may have run.
    pub fn delete_manifest(&self, manifest: &Manifest) -> Result<bool, ManifestError> {
        let path = self
            .key_dir(&manifest.key)
            .join(format!("{}.json", manifest.key.tag(manifest.seq)?));
        match fs::remove_file(&path) {
            Ok(()) => Ok(true),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(ManifestError(format!("{}: {error}", path.display()))),
        }
    }

    /// Whether the bytes a digest names are present.
    pub fn has_blob(&self, digest: &str) -> bool {
        self.blob_path(digest).exists()
    }

    /// Remove a blob. Only safe once no manifest in the store names its digest.
    pub fn delete_blob(&self, digest: &str) -> Result<bool, ManifestError> {
        let path = self.blob_path(digest);
        match fs::remove_file(&path) {
            Ok(()) => Ok(true),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(ManifestError(format!("{}: {error}", path.display()))),
        }
    }

    // -- publishing --------------------------------------------------------
    /// Plan, then write: the same two steps the Python CLI takes, so the
    /// decisions stay comparable.
    pub fn publish(&self, key: &Key, identity: &str, stream: &Path) -> Result<Manifest, ManifestError> {
        let entries = self.entries(key)?;
        let plan = plan_push(&entries, key, identity, MAX_DELTA_CHAIN)?;
        let manifest = Manifest::from_value(
            key.clone(),
            serde_json::json!({
                "seq": plan.seq,
                "identity": identity,
                "kind": plan.kind.as_str(),
                "parent": plan.parent,
                "digest": digest_of(stream)?,
                "bytes": fs::metadata(stream)
                    .map_err(|error| ManifestError(format!("{}: {error}", stream.display())))?
                    .len(),
                "created_at": now(),
            }),
        )?;
        self.put_blob(&manifest, stream)?;
        self.put_manifest(&manifest)?;
        Ok(manifest)
    }

    /// The plan a publish would take, without writing anything.
    pub fn plan(&self, key: &Key, identity: &str) -> Result<PushPlan, ManifestError> {
        plan_push(&self.entries(key)?, key, identity, MAX_DELTA_CHAIN)
    }

    /// The layers a restore would apply, base first.
    pub fn restore_plan(&self, key: &Key, identity: &str) -> Result<RestorePlan, ManifestError> {
        Ok(plan_restore(&self.entries(key)?, identity, None))
    }
}

/// `2026-11-24T00:00:00Z`, from the system clock, without a chrono dependency.
pub fn now() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    // days since epoch -> civil date (Howard Hinnant's algorithm, integer math)
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    if month <= 2 {
        year += 1;
    }
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rem / 3_600,
        (rem % 3_600) / 60,
        rem % 60
    )
}

/// Convenience for callers that only need "what is the newest compatible tip".
pub fn tip_for(store: &FileStore, key: &Key, identity: &str) -> Result<Option<Manifest>, ManifestError> {
    Ok(newest(&for_identity(&store.entries(key)?, identity)))
}

/// One past the highest entry for KEY.
pub fn next_sequence(store: &FileStore, key: &Key) -> Result<u32, ManifestError> {
    Ok(next_seq(&store.entries(key)?))
}

#[allow(dead_code)]
fn _kind_of(manifest: &Manifest) -> Kind {
    manifest.kind
}

/// Write a stream to DEST with a trailing byte, used by tests to prove a digest
/// mismatch is caught.
#[doc(hidden)]
pub fn corrupt(destination: &Path) -> std::io::Result<()> {
    let mut file = fs::OpenOptions::new().append(true).open(destination)?;
    file.write_all(b"!")
}
