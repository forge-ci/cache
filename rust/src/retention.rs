//! Retention: bound the store without breaking a chain or a neighbour's cache.
//!
//! The unit of eviction is a **chain** (one full base plus the deltas stacked on
//! it), not an entry. A delta is useless without its parent, so a middle entry
//! can never be dropped — and counting entries would silently keep everything,
//! because the newest tip's ancestors reach back to the first base.
//!
//! This mirrors `cachelib/retention.py`; `fixtures/retention.json` is the shared
//! corpus both implementations must reproduce, so the two cannot drift.
//!
//! Deletion itself (manifests plus orphaned blobs) is deliberately not here yet:
//! planning is the part that must be identical in both implementations, and the
//! blob store is global and content addressed, so removing bytes is the last
//! step to add, not the first.

use crate::{order, Key, Kind, Manifest, ManifestError};

/// Chains kept per identity when a caller does not ask for a specific number.
pub const DEFAULT_KEEP: usize = 2;

/// One full base and the deltas stacked on it, oldest first.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Chain {
    pub layers: Vec<Manifest>,
}

impl Chain {
    pub fn base(&self) -> &Manifest {
        &self.layers[0]
    }

    pub fn tip(&self) -> &Manifest {
        &self.layers[self.layers.len() - 1]
    }

    pub fn bytes(&self) -> u64 {
        self.layers.iter().map(|layer| layer.bytes).sum()
    }
}

/// Split one identity's ordered entries into base-anchored chains, oldest first.
pub fn chains_of(entries: &[Manifest]) -> Vec<Chain> {
    let mut built: Vec<Chain> = Vec::new();
    for manifest in order(entries) {
        if manifest.kind == Kind::Full || built.is_empty() {
            built.push(Chain {
                layers: vec![manifest],
            });
        } else {
            built
                .last_mut()
                .expect("just pushed")
                .layers
                .push(manifest);
        }
    }
    built
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SweepPlan {
    pub key: Key,
    pub keep: Vec<Manifest>,
    pub evict: Vec<Manifest>,
}

impl SweepPlan {
    pub fn empty(&self) -> bool {
        self.evict.is_empty()
    }

    pub fn kept_bytes(&self) -> u64 {
        self.keep.iter().map(|manifest| manifest.bytes).sum()
    }

    pub fn evicted_bytes(&self) -> u64 {
        self.evict.iter().map(|manifest| manifest.bytes).sum()
    }
}

/// Decide which chains of KEY survive: the newest KEEP per identity, then MAX_BYTES.
///
/// `protect` holds digests that must survive — the publishing run passes its own
/// writes, so a sweep can never evict the cache it just produced.
pub fn plan_sweep(
    entries: &[Manifest],
    key: &Key,
    keep: usize,
    max_bytes: Option<u64>,
    protect: &[String],
) -> Result<SweepPlan, ManifestError> {
    if keep < 1 {
        return Err(ManifestError("keep must be at least 1".to_string()));
    }
    let ordered = order(entries);
    if ordered.is_empty() {
        return Ok(SweepPlan {
            key: key.clone(),
            keep: Vec::new(),
            evict: Vec::new(),
        });
    }

    // Identities in first-seen order, each with its chains oldest-first.
    let mut identities: Vec<String> = Vec::new();
    for manifest in &ordered {
        if !identities.contains(&manifest.identity) {
            identities.push(manifest.identity.clone());
        }
    }
    let mut by_identity: Vec<Vec<Chain>> = Vec::new();
    for identity in &identities {
        let mine: Vec<Manifest> = ordered
            .iter()
            .filter(|manifest| manifest.identity == *identity)
            .cloned()
            .collect();
        by_identity.push(chains_of(&mine));
    }

    let mut survived: Vec<u32> = Vec::new();
    let mut evicted: Vec<u32> = Vec::new();
    // Surviving chain indices per identity, newest first, so the cap can act on a
    // whole chain rather than on the oldest entries.
    let mut surviving: Vec<Vec<usize>> = Vec::new();

    for chains in by_identity.iter() {
        let newest_first: Vec<usize> = (0..chains.len()).rev().collect();
        let mut kept: Vec<usize> = Vec::new();
        for (position, index) in newest_first.iter().enumerate() {
            let chain = &chains[*index];
            let pinned = chain
                .layers
                .iter()
                .any(|layer| protect.iter().any(|digest| digest == &layer.digest));
            if position < keep || pinned {
                kept.push(*index);
                for layer in &chain.layers {
                    if !survived.contains(&layer.seq) {
                        survived.push(layer.seq);
                    }
                }
            } else {
                for layer in &chain.layers {
                    if !evicted.contains(&layer.seq) {
                        evicted.push(layer.seq);
                    }
                }
            }
        }
        kept.sort_unstable();
        kept.reverse(); // newest first
        surviving.push(kept);
    }

    if let Some(cap) = max_bytes {
        let mut total: u64 = ordered
            .iter()
            .filter(|manifest| survived.contains(&manifest.seq))
            .map(|manifest| manifest.bytes)
            .sum();
        while total > cap {
            // Oldest surviving chain across identities; never the last one.
            let mut oldest: Option<(usize, usize)> = None;
            for (identity, indices) in surviving.iter().enumerate() {
                if indices.len() > 1 {
                    let candidate = indices[indices.len() - 1];
                    oldest = match oldest {
                        None => Some((identity, candidate)),
                        Some((_, index)) if candidate < index => Some((identity, candidate)),
                        other => other,
                    };
                }
            }
            let Some((identity, index)) = oldest else { break };
            surviving[identity].pop();
            for layer in &by_identity[identity][index].layers {
                survived.retain(|seq| *seq != layer.seq);
                if !evicted.contains(&layer.seq) {
                    evicted.push(layer.seq);
                }
                total -= layer.bytes;
            }
        }
    }

    let kept = ordered
        .iter()
        .filter(|manifest| survived.contains(&manifest.seq))
        .cloned()
        .collect();
    let gone = ordered
        .iter()
        .filter(|manifest| evicted.contains(&manifest.seq))
        .cloned()
        .collect();
    Ok(SweepPlan {
        key: key.clone(),
        keep: kept,
        evict: gone,
    })
}
