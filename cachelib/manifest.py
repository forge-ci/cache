"""Manifests: one JSON document per cache layer, named by an ordered integer.

A distributed index is only usable if "find the newest entry for this key" is
cheap without a coordinator. It is cheap when the entries live under one prefix
and their names sort in publish order:

    <repo>/<family>/<platform>/s000001.json
    <repo>/<family>/<platform>/s000002.json

Every backend this store targets -- a filesystem, S3, an OCI registry -- lists
by prefix and returns keys in lexicographic order, so the newest entry is the
last line of one listing. No read-modify-write of a shared index file, no
database, and concurrent publishers cannot corrupt each other: the sequence
number is chosen from the same listing, and losing a race costs a retry, not a
torn document.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA = 1
SEQ_DIGITS = 6
FULL = "full"
DELTA = "delta"


class ManifestError(ValueError):
    """A manifest that cannot be trusted to name a restorable layer."""


def seq_name(seq: int) -> str:
    if seq < 1:
        raise ManifestError(f"sequence numbers start at 1, got {seq}")
    return f"s{seq:0{SEQ_DIGITS}d}"


def parse_seq(name: str) -> int:
    """`s000123` / `s000123.json` -> 123. Anything else is not an entry."""
    stem = name.removesuffix(".json")
    if not stem.startswith("s") or not stem[1:].isdigit():
        raise ManifestError(f"{name!r} is not a cache entry name")
    return int(stem[1:])


@dataclass(frozen=True)
class Key:
    """What a cache belongs to. `family` is the build type, not the workflow."""

    repo: str
    family: str
    platform: str

    def __post_init__(self) -> None:
        for name, value in (
            ("repo", self.repo),
            ("family", self.family),
            ("platform", self.platform),
        ):
            if not value or value != value.strip():
                raise ManifestError(f"bad {name} {value!r}")
            segments = value.split("/")
            if any(segment in ("", ".", "..") for segment in segments):
                raise ManifestError(f"bad {name} {value!r}: empty or escaping segment")
            # `owner/name` is the only place a slash may appear; the prefix is
            # built from these three fields, so a stray slash would move a key.
            if name != "repo" and len(segments) != 1:
                raise ManifestError(f"bad {name} {value!r}: a slash would nest the key")
            if name == "repo" and len(segments) != 2:
                raise ManifestError(f"bad repo {value!r}: expected owner/name")

    @property
    def prefix(self) -> str:
        return f"{self.repo}/{self.family}/{self.platform}"

    def entry(self, seq: int) -> str:
        return f"{self.prefix}/{seq_name(seq)}"

    def tag(self, seq: int) -> str:
        """OCI tag: the key is in the repository name, so the tag is the seq."""
        return seq_name(seq)


@dataclass(frozen=True)
class Manifest:
    """One layer: either a full tree or a delta from `parent`."""

    key: Key
    seq: int
    identity: str
    kind: str
    digest: str
    bytes: int
    created_at: str
    parent: str | None = None
    ref: str = ""
    source_sha: str = ""
    run_id: str = ""
    entries: int | None = None
    tool: str = "cachectl"
    schema: int = SCHEMA

    def __post_init__(self) -> None:
        if self.kind not in (FULL, DELTA):
            raise ManifestError(
                f"kind must be {FULL!r} or {DELTA!r}, got {self.kind!r}"
            )
        if self.kind == DELTA and not self.parent:
            raise ManifestError("a delta layer must name its parent")
        if self.kind == FULL and self.parent:
            raise ManifestError("a full layer cannot have a parent")
        if (
            not self.digest.startswith("sha256:")
            or len(self.digest) != len("sha256:") + 64
        ):
            raise ManifestError(f"digest must be sha256:<64 hex>, got {self.digest!r}")
        if not self.identity:
            raise ManifestError(
                "identity is the compatibility contract; it cannot be empty"
            )

    def to_json(self) -> str:
        data = asdict(self)
        data["key"] = asdict(self.key)
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        raw: dict[str, Any] = json.loads(text)
        key = raw.pop("key")
        if not isinstance(key, dict):
            raise ManifestError("manifest is missing its key")
        return cls(key=Key(**key), **raw)


def order(manifests: list[Manifest]) -> list[Manifest]:
    """Oldest first. Sequence numbers are publish order, so this is the chain."""
    return sorted(manifests, key=lambda m: m.seq)


def newest(manifests: list[Manifest]) -> Manifest | None:
    ordered = order(manifests)
    return ordered[-1] if ordered else None


def for_identity(manifests: list[Manifest], identity: str) -> list[Manifest]:
    """The subset a consumer may legally restore. Identity is exact."""
    return [m for m in order(manifests) if m.identity == identity]


def next_seq(manifests: list[Manifest]) -> int:
    """One past the highest published entry for the key."""
    current = newest(manifests)
    return (current.seq + 1) if current else 1


def chain_of(manifests: list[Manifest], tip: Manifest) -> list[Manifest]:
    """Base -> tip, following `parent` by digest.

    Returns [] when the chain is incomplete (a missing ancestor, a digest that
    names no entry, a cycle). An incomplete chain is a cache miss, never a
    partial restore: applying deltas to the wrong base silently corrupts the
    tree, which is worse than a cold build.
    """
    by_digest = {m.digest: m for m in manifests}
    walk: list[Manifest] = [tip]
    seen = {tip.digest}
    while walk[-1].kind == DELTA:
        parent = by_digest.get(walk[-1].parent or "")
        if parent is None or parent.digest in seen:
            return []
        seen.add(parent.digest)
        walk.append(parent)
    return list(reversed(walk))


@dataclass(frozen=True)
class Entry:
    """A manifest plus where its bytes are, as a backend reports them."""

    manifest: Manifest
    location: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
