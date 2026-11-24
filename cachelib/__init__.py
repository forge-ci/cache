"""forge-ci/cache: a delta-chained, content-addressed build cache."""

from .chain import (
    MAX_DELTA_CHAIN,
    PushPlan,
    RestorePlan,
    describe,
    plan_push,
    plan_restore,
)
from .manifest import (
    DELTA,
    FULL,
    Entry,
    Key,
    Manifest,
    ManifestError,
    chain_of,
    for_identity,
    newest,
    next_seq,
    order,
    parse_seq,
    seq_name,
)
from .sessions import ReplayReport, ReplayStep, Session, SessionLog, replay
from .snapshot import Snapshotter, SnapshotUnavailable, available
from .store import FileStore, OciStore, Registry, digest_of, repository_name

__all__ = [
    "DELTA",
    "FULL",
    "MAX_DELTA_CHAIN",
    "Entry",
    "FileStore",
    "Key",
    "Manifest",
    "ManifestError",
    "OciStore",
    "PushPlan",
    "Registry",
    "ReplayReport",
    "ReplayStep",
    "RestorePlan",
    "Session",
    "SessionLog",
    "SnapshotUnavailable",
    "Snapshotter",
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
    "replay",
    "repository_name",
    "seq_name",
]
