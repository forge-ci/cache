"""Full-or-delta policy and the restore plan it implies.

Two decisions live here, and nothing else in the store is allowed to invent
them:

* **What does the next publish write?** A delta from the current tip while the
  chain is short, a fresh full base once it is not. The trigger is the chain
  length, never a clock: a nightly that finds nothing new must not cut a new
  base, and a burst of merge-queue publishes must not turn into an unbounded
  delta stack.
* **What does a restore apply?** The newest chain whose identity matches, base
  first. A consumer never receives an empty directory pretending to be a hit.
"""

from __future__ import annotations

from dataclasses import dataclass

from .manifest import (
    DELTA,
    FULL,
    Manifest,
    chain_of,
    for_identity,
    newest,
    next_seq,
)

# Publish a delta while the chain is this short or shorter; past it, cut a new
# full base. Five keeps the per-restore apply cost bounded (one base plus at
# most five `btrfs receive` calls) without making every publish a multi-GB
# upload: OBS's C++ store moves ~1-2 GB, so six of them is already a store
# nobody wants to re-download after an identity change.
MAX_DELTA_CHAIN = 5


@dataclass(frozen=True)
class PushPlan:
    kind: str
    parent: Manifest | None
    seq: int
    reason: str

    @property
    def is_full(self) -> bool:
        return self.kind == FULL


@dataclass(frozen=True)
class RestorePlan:
    identity: str
    layers: list[Manifest]
    reason: str

    @property
    def hit(self) -> bool:
        return bool(self.layers)

    @property
    def tip(self) -> Manifest | None:
        return self.layers[-1] if self.layers else None

    def missing_bytes(self) -> int:
        return sum(layer.bytes for layer in self.layers)


def plan_push(
    manifests: list[Manifest], key, identity: str, max_delta: int = MAX_DELTA_CHAIN
) -> PushPlan:
    """Decide the layer a new publish should write for KEY/IDENTITY."""
    if max_delta < 1:
        raise ValueError("max_delta must be at least 1")
    seq = next_seq(manifests)
    compatible = for_identity(manifests, identity)
    tip = newest(compatible)
    if tip is None:
        return PushPlan(
            FULL, None, seq, "first entry for this identity: nothing to delta against"
        )
    chain = chain_of(compatible, tip)
    if not chain:
        return PushPlan(
            FULL,
            None,
            seq,
            f"chain for {tip.digest[:19]} is incomplete: a new base is cheaper than an unverifiable delta",
        )
    deltas = sum(1 for layer in chain if layer.kind == DELTA)
    if deltas >= max_delta:
        return PushPlan(
            FULL,
            None,
            seq,
            f"{deltas} delta layer(s) already at the cap of {max_delta}: start a new base",
        )
    return PushPlan(
        DELTA, tip, seq, f"{deltas} delta layer(s) below the cap of {max_delta}"
    )


def plan_restore(
    manifests: list[Manifest], identity: str, pinned_digest: str | None = None
) -> RestorePlan:
    """Decide which layers a consumer applies, base first.

    `pinned_digest` restores one exact layer (a bisect, a reproduction). It is
    still subject to the chain check: an exact layer whose ancestors are gone is
    a miss, not a partial tree.
    """
    compatible = for_identity(manifests, identity)
    if not compatible:
        return RestorePlan(
            identity, [], f"no published entry for identity {identity!r}"
        )
    if pinned_digest is not None:
        target = next((m for m in compatible if m.digest == pinned_digest), None)
        if target is None:
            return RestorePlan(
                identity, [], f"no entry with digest {pinned_digest} for this identity"
            )
        chain = chain_of(compatible, target)
        if not chain:
            return RestorePlan(
                identity,
                [],
                f"pinned layer {pinned_digest[:19]} has an incomplete chain",
            )
        return RestorePlan(identity, chain, "pinned layer")
    tip = newest(compatible)
    assert tip is not None
    chain = chain_of(compatible, tip)
    if not chain:
        return RestorePlan(
            identity, [], f"newest entry {tip.digest[:19]} has an incomplete chain"
        )
    return RestorePlan(
        identity, chain, f"newest entry s{tip.seq:06d} ({len(chain)} layer(s))"
    )


def describe(plan: RestorePlan) -> str:
    if not plan.hit:
        return f"miss: {plan.reason}"
    return (
        f"hit: s{plan.tip.seq:06d} via {len(plan.layers)} layer(s), "
        f"{plan.missing_bytes()} B to apply ({plan.reason})"
    )
