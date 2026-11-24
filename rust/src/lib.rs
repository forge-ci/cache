//! Embedded-mode forge-ci/cache: the model, without a daemon or an HTTP hop.
//!
//! This crate is deployment 1 of `docs/design.md`. It implements the part that
//! must be identical in both implementations -- keys, manifest validation, chain
//! integrity and the publish/restore decisions -- so a compiler launcher can
//! restore and publish in-process.
//!
//! The wire format is the contract: `Manifest` (de)serialises to the same JSON
//! the Python package writes, so an entry published by either side is readable
//! by the other. `tests/conformance.rs` holds both to the shared corpus in
//! `fixtures/conformance.json`; a fixture one side disagrees with is a bug.
//!
//! Not here yet (tracked in forge-ci/cache#6): the file store, digesting blobs
//! and the registry transport. This crate decides; the caller moves bytes.

use serde::{Deserialize, Serialize};
use std::fmt;

pub mod registry;
pub mod retention;
pub mod store;

/// Publish a delta while the chain is this short; past it, cut a new base.
pub const MAX_DELTA_CHAIN: usize = 5;
pub const SEQ_DIGITS: usize = 6;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManifestError(pub String);

impl fmt::Display for ManifestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for ManifestError {}

impl From<serde_json::Error> for ManifestError {
    fn from(error: serde_json::Error) -> Self {
        ManifestError(format!("manifest is not valid JSON: {error}"))
    }
}

/// What a cache belongs to. `repo` is `owner/name`; the other two are single
/// path segments, because the key is the storage prefix.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Key {
    pub repo: String,
    pub family: String,
    pub platform: String,
}

impl Key {
    pub fn new(repo: &str, family: &str, platform: &str) -> Result<Self, ManifestError> {
        let key = Key {
            repo: repo.to_string(),
            family: family.to_string(),
            platform: platform.to_string(),
        };
        key.validate()?;
        Ok(key)
    }

    pub fn validate(&self) -> Result<(), ManifestError> {
        for (name, value) in [
            ("repo", &self.repo),
            ("family", &self.family),
            ("platform", &self.platform),
        ] {
            if value.is_empty() || value.trim() != value {
                return Err(ManifestError(format!("bad {name} {value:?}")));
            }
            let segments: Vec<&str> = value.split('/').collect();
            if segments
                .iter()
                .any(|segment| segment.is_empty() || *segment == "." || *segment == "..")
            {
                return Err(ManifestError(format!(
                    "bad {name} {value:?}: empty or escaping segment"
                )));
            }
            if name != "repo" && segments.len() != 1 {
                return Err(ManifestError(format!(
                    "bad {name} {value:?}: a slash would nest the key"
                )));
            }
            if name == "repo" && segments.len() != 2 {
                return Err(ManifestError(format!(
                    "bad repo {value:?}: expected owner/name"
                )));
            }
        }
        Ok(())
    }

    pub fn prefix(&self) -> String {
        format!("{}/{}/{}", self.repo, self.family, self.platform)
    }

    pub fn entry(&self, seq: u32) -> Result<String, ManifestError> {
        Ok(format!("{}/{}", self.prefix(), seq_name(seq)?))
    }

    pub fn tag(&self, seq: u32) -> Result<String, ManifestError> {
        seq_name(seq)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Full,
    Delta,
}

impl Kind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Kind::Full => "full",
            Kind::Delta => "delta",
        }
    }
}

/// `s000042` for 42: zero-padded so names sort in publish order.
pub fn seq_name(seq: u32) -> Result<String, ManifestError> {
    if seq == 0 {
        return Err(ManifestError(
            "sequence numbers start at 1, got 0".to_string(),
        ));
    }
    Ok(format!("s{seq:0width$}", width = SEQ_DIGITS))
}

/// `s000042` / `s000042.json` -> 42.
pub fn parse_seq(name: &str) -> Result<u32, ManifestError> {
    let stem = name.strip_suffix(".json").unwrap_or(name);
    let digits = stem
        .strip_prefix('s')
        .ok_or_else(|| ManifestError(format!("{name:?} is not a cache entry name")))?;
    if digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
        return Err(ManifestError(format!(
            "{name:?} is not a cache entry name"
        )));
    }
    digits
        .parse::<u32>()
        .map_err(|_| ManifestError(format!("{name:?} is not a cache entry name")))
}

/// The manifest fields as stored. `key` comes from the storage path, not the
/// document, exactly as the Python side reads it from the directory.
#[derive(Debug, Clone, Deserialize)]
struct ManifestWire {
    seq: u32,
    identity: String,
    kind: Kind,
    digest: String,
    bytes: u64,
    created_at: String,
    #[serde(default)]
    parent: Option<String>,
    #[serde(default, rename = "ref")]
    git_ref: String,
    #[serde(default)]
    source_sha: String,
    #[serde(default)]
    run_id: String,
    #[serde(default)]
    entries: Option<u32>,
    #[serde(default = "default_tool")]
    tool: String,
    #[serde(default = "default_schema")]
    schema: u32,
}

fn default_tool() -> String {
    "cachectl".to_string()
}

fn default_schema() -> u32 {
    1
}

/// One layer: a full tree, or a delta from `parent`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Manifest {
    pub key: Key,
    pub seq: u32,
    pub identity: String,
    pub kind: Kind,
    pub digest: String,
    pub bytes: u64,
    pub created_at: String,
    pub parent: Option<String>,
    #[serde(rename = "ref")]
    pub git_ref: String,
    pub source_sha: String,
    pub run_id: String,
    pub entries: Option<u32>,
    pub tool: String,
    pub schema: u32,
}

impl Manifest {
    /// Parse one stored manifest and refuse anything that cannot be restored.
    pub fn from_wire(key: Key, json: &str) -> Result<Self, ManifestError> {
        let value: serde_json::Value = serde_json::from_str(json)?;
        Self::from_value(key, value)
    }

    pub fn from_value(key: Key, value: serde_json::Value) -> Result<Self, ManifestError> {
        let wire: ManifestWire = serde_json::from_value(value)?;
        let manifest = Manifest {
            key,
            seq: wire.seq,
            identity: wire.identity,
            kind: wire.kind,
            digest: wire.digest,
            bytes: wire.bytes,
            created_at: wire.created_at,
            parent: wire.parent,
            git_ref: wire.git_ref,
            source_sha: wire.source_sha,
            run_id: wire.run_id,
            entries: wire.entries,
            tool: wire.tool,
            schema: wire.schema,
        };
        manifest.validate()?;
        Ok(manifest)
    }

    pub fn validate(&self) -> Result<(), ManifestError> {
        self.key.validate()?;
        if seq_name(self.seq).is_err() {
            return Err(ManifestError(format!(
                "sequence numbers start at 1, got {}",
                self.seq
            )));
        }
        match self.kind {
            Kind::Delta if self.parent.is_none() => {
                return Err(ManifestError(
                    "a delta layer must name its parent".to_string(),
                ))
            }
            Kind::Full if self.parent.is_some() => {
                return Err(ManifestError(
                    "a full layer cannot have a parent".to_string(),
                ))
            }
            _ => {}
        }
        let digest = &self.digest;
        let hex_ok = digest.len() == "sha256:".len() + 64
            && digest.starts_with("sha256:")
            && digest["sha256:".len()..]
                .chars()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase());
        if !hex_ok {
            return Err(ManifestError(format!(
                "digest must be sha256:<64 hex>, got {digest:?}"
            )));
        }
        if self.identity.is_empty() {
            return Err(ManifestError(
                "identity is the compatibility contract; it cannot be empty".to_string(),
            ));
        }
        Ok(())
    }
}

/// Oldest first. Sequence numbers are publish order, so this is the chain order.
pub fn order(manifests: &[Manifest]) -> Vec<Manifest> {
    let mut ordered = manifests.to_vec();
    ordered.sort_by_key(|manifest| manifest.seq);
    ordered
}

pub fn newest(manifests: &[Manifest]) -> Option<Manifest> {
    order(manifests).pop()
}

pub fn for_identity(manifests: &[Manifest], identity: &str) -> Vec<Manifest> {
    order(manifests)
        .into_iter()
        .filter(|manifest| manifest.identity == identity)
        .collect()
}

pub fn next_seq(manifests: &[Manifest]) -> u32 {
    newest(manifests).map_or(1, |manifest| manifest.seq + 1)
}

/// Base -> tip, following `parent` by digest. Empty when the chain is broken:
/// an incomplete chain is a miss, never a partial restore.
pub fn chain_of(manifests: &[Manifest], tip: &Manifest) -> Vec<Manifest> {
    let ordered = order(manifests);
    let mut walk = vec![tip.clone()];
    let mut seen = vec![tip.digest.clone()];
    while walk.last().map(|m| m.kind) == Some(Kind::Delta) {
        let parent_digest = walk
            .last()
            .and_then(|manifest| manifest.parent.clone())
            .unwrap_or_default();
        let parent = ordered
            .iter()
            .find(|manifest| manifest.digest == parent_digest);
        match parent {
            Some(parent) if !seen.contains(&parent.digest) => {
                seen.push(parent.digest.clone());
                walk.push(parent.clone());
            }
            _ => return Vec::new(),
        }
    }
    walk.reverse();
    walk
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PushPlan {
    pub kind: Kind,
    pub parent: Option<String>,
    pub seq: u32,
    pub reason: String,
}

impl PushPlan {
    pub fn is_full(&self) -> bool {
        self.kind == Kind::Full
    }
}

pub fn plan_push(
    manifests: &[Manifest],
    _key: &Key,
    identity: &str,
    max_delta: usize,
) -> Result<PushPlan, ManifestError> {
    if max_delta < 1 {
        return Err(ManifestError("max_delta must be at least 1".to_string()));
    }
    let seq = next_seq(manifests);
    let compatible = for_identity(manifests, identity);
    let full = |seq: u32, reason: String| PushPlan {
        kind: Kind::Full,
        parent: None,
        seq,
        reason,
    };
    let Some(tip) = newest(&compatible) else {
        return Ok(full(
            seq,
            "first entry for this identity: nothing to delta against".to_string(),
        ));
    };
    let chain = chain_of(&compatible, &tip);
    if chain.is_empty() {
        return Ok(full(
            seq,
            format!(
                "chain for {} is incomplete: a new base is cheaper than an unverifiable delta",
                &tip.digest[..tip.digest.len().min(19)]
            ),
        ));
    }
    let deltas = chain.iter().filter(|m| m.kind == Kind::Delta).count();
    if deltas >= max_delta {
        return Ok(full(
            seq,
            format!("{deltas} delta layer(s) already at the cap of {max_delta}: start a new base"),
        ));
    }
    Ok(PushPlan {
        kind: Kind::Delta,
        parent: Some(tip.digest.clone()),
        seq,
        reason: format!("{deltas} delta layer(s) below the cap of {max_delta}"),
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestorePlan {
    pub identity: String,
    pub layers: Vec<Manifest>,
    pub reason: String,
}

impl RestorePlan {
    pub fn hit(&self) -> bool {
        !self.layers.is_empty()
    }

    pub fn tip(&self) -> Option<&Manifest> {
        self.layers.last()
    }

    pub fn missing_bytes(&self) -> u64 {
        self.layers.iter().map(|layer| layer.bytes).sum()
    }
}

pub fn plan_restore(
    manifests: &[Manifest],
    identity: &str,
    pinned_digest: Option<&str>,
) -> RestorePlan {
    let compatible = for_identity(manifests, identity);
    let miss = |reason: String| RestorePlan {
        identity: identity.to_string(),
        layers: Vec::new(),
        reason,
    };
    if compatible.is_empty() {
        return miss(format!("no published entry for identity {identity:?}"));
    }
    if let Some(pinned) = pinned_digest {
        let Some(target) = compatible.iter().find(|m| m.digest == pinned) else {
            return miss(format!(
                "no entry with digest {pinned} for this identity"
            ));
        };
        let chain = chain_of(&compatible, target);
        if chain.is_empty() {
            return miss(format!(
                "pinned layer {} has an incomplete chain",
                &pinned[..pinned.len().min(19)]
            ));
        }
        return RestorePlan {
            identity: identity.to_string(),
            layers: chain,
            reason: "pinned layer".to_string(),
        };
    }
    let tip = newest(&compatible).expect("checked non-empty");
    let chain = chain_of(&compatible, &tip);
    if chain.is_empty() {
        return miss(format!(
            "newest entry {} has an incomplete chain",
            &tip.digest[..tip.digest.len().min(19)]
        ));
    }
    RestorePlan {
        identity: identity.to_string(),
        reason: format!("newest entry s{:06} ({} layer(s))", tip.seq, chain.len()),
        layers: chain,
    }
}
