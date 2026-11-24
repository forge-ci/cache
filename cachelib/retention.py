"""Retention: bound the store without ever breaking a chain or a live entry.

Entries are immutable and append-only — that is what lets a merge-queue run
publish. The cost is that nothing removes them, so a store grows without bound
until something does. This module is the only thing allowed to delete, and it
deletes from a plan, never from a policy hidden inside a publish.

The unit of eviction is a **chain**, not an entry. A chain is one full base plus
the deltas stacked on it. That follows from the data model: a delta is useless
without its parent, so a middle entry can never be dropped — you can only drop a
whole chain. Counting entries would silently keep the entire history alive, since
the newest tip's ancestors reach back to the first base.

The invariants:

1. **A survivor is never orphaned.** Every kept chain is kept from its base.
2. **The newest chain per identity always survives**, so a key can restart
   cheaply; the newest full base is inside it by construction.
3. **A protected digest keeps its whole chain.** The run that just published
   passes its own writes, so a sweep can never evict the cache it just produced.
4. **Blobs are shared.** The blob store is content addressed and global, so a
   blob is deleted only when no surviving manifest anywhere still names it — one
   key's sweep must never break another key's entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .manifest import FULL, Key, Manifest, order


@dataclass(frozen=True)
class Chain:
    """One full base and the deltas stacked on it."""

    layers: list[Manifest]

    @property
    def base(self) -> Manifest:
        return self.layers[0]

    @property
    def tip(self) -> Manifest:
        return self.layers[-1]

    @property
    def bytes(self) -> int:
        return sum(layer.bytes for layer in self.layers)

    @property
    def digests(self) -> set[str]:
        return {layer.digest for layer in self.layers}


def chains_of(entries: list[Manifest]) -> list[Chain]:
    """Split one identity's ordered entries into base-anchored chains, oldest first."""
    built: list[Chain] = []
    for manifest in order(entries):
        if manifest.kind == FULL or not built:
            built.append(Chain([manifest]))
        else:
            built[-1].layers.append(manifest)
    return built


@dataclass(frozen=True)
class SweepPlan:
    """What a sweep would delete, and why each survivor survived."""

    key: Key
    keep: list[Manifest] = field(default_factory=list)
    evict: list[Manifest] = field(default_factory=list)
    reasons: dict[int, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.evict

    def kept_bytes(self) -> int:
        return sum(manifest.bytes for manifest in self.keep)

    def evicted_bytes(self) -> int:
        return sum(manifest.bytes for manifest in self.evict)


def plan_sweep(
    entries: list[Manifest],
    key: Key,
    *,
    keep: int = 2,
    max_bytes: int | None = None,
    protect: set[str] | None = None,
) -> SweepPlan:
    """Decide which chains of KEY survive: the newest KEEP per identity, then MAX_BYTES.

    `protect` holds digests that must survive regardless — the run that just
    published passes its own writes so a sweep can never evict the cache it just
    produced.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")
    protected = set(protect or ())
    ordered = order(entries)
    if not ordered:
        return SweepPlan(key)

    by_identity: dict[str, list[Chain]] = {}
    for manifest in ordered:
        by_identity.setdefault(manifest.identity, [])
    for identity in by_identity:
        by_identity[identity] = chains_of([m for m in ordered if m.identity == identity])

    reasons: dict[int, str] = {}
    survivors: dict[int, Manifest] = {}

    def survive(chain: Chain, reason: str) -> None:
        for layer in chain.layers:
            if layer.seq not in survivors:
                survivors[layer.seq] = layer
                reasons[layer.seq] = reason

    evicted: dict[int, Manifest] = {}
    # Chain indices that survive per identity, kept so the size cap can act on a
    # whole chain: evicting "the oldest entries" would strip a surviving delta of
    # its base and turn a hit into a silent miss.
    surviving: dict[str, list[int]] = {}
    for identity, chains in by_identity.items():
        newest_first = list(reversed(chains))
        keep_these: list[int] = []
        for index, chain in enumerate(newest_first):
            original = len(chains) - 1 - index
            pinned = any(layer.digest in protected for layer in chain.layers)
            if index < keep or pinned:
                keep_these.append(original)
                reason = (
                    "protected: written by this run"
                    if pinned and index >= keep
                    else f"newest {min(keep, len(chains))} chain(s) of identity {identity}"
                )
                survive(chain, reason)
            else:
                for layer in chain.layers:
                    evicted[layer.seq] = layer
        surviving[identity] = sorted(keep_these, reverse=True)  # newest first

    # Optional size cap: drop the oldest whole chains until it fits. The newest
    # chain of an identity is never dropped, even if it alone exceeds the cap --
    # an oversized cache that works beats a store that cannot restore anything.
    if max_bytes is not None:
        total = sum(manifest.bytes for manifest in survivors.values())
        while total > max_bytes:
            candidates = [
                (identity, indices[-1])
                for identity, indices in surviving.items()
                if len(indices) > 1
            ]
            if not candidates:
                break
            # Oldest chain first: a sweep must be predictable, and freeing bytes
            # in FIFO order is easier to reason about than "biggest first".
            identity, drop = min(candidates, key=lambda item: item[1])
            chain = by_identity[identity][drop]
            surviving[identity].remove(drop)
            for layer in chain.layers:
                survivors.pop(layer.seq, None)
                reasons.pop(layer.seq, None)
                evicted[layer.seq] = layer
                total -= layer.bytes

    kept = [manifest for manifest in ordered if manifest.seq in survivors]
    return SweepPlan(
        key,
        kept,
        [manifest for manifest in ordered if manifest.seq in evicted],
        {m.seq: reasons[m.seq] for m in kept},
    )


def orphan_blobs(store, evicted: list[Manifest]) -> list[str]:
    """Digests referenced only by EVICTED entries, across every key in the store.

    A digest that another key still names is *not* an orphan: the blob store is
    global and content addressed, so deleting it would break an unrelated cache.
    """
    if not evicted:
        return []
    # The manifests being evicted are still on disk when this runs, so they must
    # be excluded explicitly: counting them as references would make every
    # eviction look "still in use" and leak every blob forever.
    going: set[tuple[str, int]] = {(m.key.prefix, m.seq) for m in evicted}
    still_referenced: set[str] = set()
    for key in store.keys():
        for manifest in store.entries(key):
            if (manifest.key.prefix, manifest.seq) in going:
                continue
            still_referenced.add(manifest.digest)
    return [manifest.digest for manifest in evicted if manifest.digest not in still_referenced]


def apply_sweep(store, plan: SweepPlan, *, dry_run: bool = False) -> dict:
    """Delete the planned entries, then the blobs nobody references any more."""
    report = {
        "key": plan.key.prefix,
        "dry_run": dry_run,
        "kept": [
            {"seq": manifest.seq, "reason": plan.reasons.get(manifest.seq, "")}
            for manifest in plan.keep
        ],
        "evicted": [
            {"seq": manifest.seq, "digest": manifest.digest, "bytes": manifest.bytes}
            for manifest in plan.evict
        ],
        "blobs_deleted": [],
        "bytes_freed": 0,
    }
    if dry_run or plan.empty:
        return report

    orphans = orphan_blobs(store, plan.evict)
    for manifest in plan.evict:
        path = store.root / manifest.key.prefix / f"{manifest.key.tag(manifest.seq)}.json"
        path.unlink(missing_ok=True)
    for digest in orphans:
        blob = store.root / "blobs" / digest.replace(":", "/", 1)
        if blob.exists():
            blob.unlink()
            report["blobs_deleted"].append(digest)
    report["bytes_freed"] = plan.evicted_bytes()
    return report
