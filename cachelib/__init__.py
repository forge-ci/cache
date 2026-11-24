"""forge-ci/cache: a delta-chained, content-addressed build cache."""

from .chain import (
    MAX_DELTA_CHAIN,
    PushPlan,
    RestorePlan,
    describe,
    plan_push,
    plan_restore,
)
from .manifest import DELTA, FULL, Entry, Key, Manifest, ManifestError, chain_of
from .manifest import for_identity, newest, next_seq, order, parse_seq, seq_name
from .snapshot import Snapshotter, SnapshotUnavailable, available
from .store import FileStore, OciStore, Registry, digest_of, repository_name

__all__ = [
    "DELTA",
    "FULL",
    "Entry",
    "FileStore",
    "Key",
    "MAX_DELTA_CHAIN",
    "Manifest",
    "ManifestError",
    "OciStore",
    "PushPlan",
    "Registry",
    "RestorePlan",
    "Snapshotter",
    "SnapshotUnavailable",
    "available",
    "chain_of",
    "describe",
    "digest_of",
    "for_identity",
    "newest",
    "next_seq",
    "order",
    "parse_seq",
    "plan_push",
    "plan_restore",
    "repository_name",
    "seq_name",
]
