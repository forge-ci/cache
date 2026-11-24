"""Backends: a filesystem for the reference store, an OCI registry for the fleet.

Both expose the same three operations, because the manifest model only needs
three:

    entries(key)          -> list[Manifest]   (ordered, complete, no pagination
                                              semantics leaking into callers)
    put(manifest, stream) -> None             (a layer's bytes, idempotent)
    get(manifest, dest)   -> None             (a layer's bytes, digest-checked)

`entries` is where a database would normally live. It does not, because every
backend here can list one prefix in publish order: a directory listing for
`FileStore`, `/v2/<repo>/tags/list` for `OciStore`. That is the whole index.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .manifest import Entry, Key, Manifest, ManifestError, parse_seq


def digest_of(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return f"sha256:{sha.hexdigest()}"


class FileStore:
    """The reference backend: a directory tree, no server, no locking.

    Layout:
        <root>/<repo>/<family>/<platform>/s000001.json
        <root>/blobs/sha256/<64 hex>

    A publish writes the blob first and the manifest second, so a manifest that
    exists always names bytes that exist. Two publishers racing for the same
    sequence number are resolved by `os.link`: the loser gets FileExistsError
    and retries with `next_seq`, which is why there is no lock file.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # -- manifests ---------------------------------------------------------
    def _dir(self, key: Key) -> Path:
        return self.root / key.prefix

    def entries(self, key: Key) -> list[Manifest]:
        directory = self._dir(key)
        if not directory.is_dir():
            return []
        found: list[Manifest] = []
        for path in directory.iterdir():
            if not path.name.endswith(".json"):
                continue
            try:
                parse_seq(path.name)
            except ManifestError:
                continue
            found.append(Manifest.from_json(path.read_text()))
        return sorted(found, key=lambda m: m.seq)

    def put_manifest(self, manifest: Manifest) -> None:
        directory = self._dir(manifest.key)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{manifest.key.tag(manifest.seq)}.json"
        staging = target.with_suffix(".json.tmp")
        staging.write_text(manifest.to_json())
        try:
            os.link(staging, target)  # atomic, and fails on a lost race
        except FileExistsError:
            existing = Manifest.from_json(target.read_text())
            if existing != manifest:
                raise ManifestError(
                    f"{target.name} already holds a different layer; publish under the next sequence"
                )
        finally:
            staging.unlink(missing_ok=True)

    # -- blobs -------------------------------------------------------------
    def _blob(self, digest: str) -> Path:
        return self.root / "blobs" / digest.replace(":", "/", 1)

    def put_blob(self, manifest: Manifest, source: str | Path) -> None:
        source = Path(source)
        actual = digest_of(source)
        if actual != manifest.digest:
            raise ManifestError(
                f"{source} hashes to {actual}, manifest says {manifest.digest}"
            )
        target = self._blob(manifest.digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return  # content addressed: already there
        staging = target.with_suffix(".part")
        shutil.copyfile(source, staging)
        os.replace(staging, target)

    def get_blob(self, manifest: Manifest, destination: str | Path) -> Path:
        source = self._blob(manifest.digest)
        if not source.exists():
            raise FileNotFoundError(f"blob {manifest.digest} is not in {self.root}")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if digest_of(destination) != manifest.digest:
            destination.unlink(missing_ok=True)
            raise ManifestError(f"blob {manifest.digest} arrived corrupt")
        return destination

    def keys(self) -> list[Key]:
        """Every key with at least one entry.

        Derived from the manifests themselves rather than from a glob of the
        directory tree: `repo` is `owner/name`, so a key is four path segments
        deep and any fixed-depth glob would silently stop matching a differently
        shaped repo.
        """
        found: dict[str, Key] = {}
        for path in sorted(self.root.rglob("*.json")):
            if path.parent == self.root:
                continue
            try:
                parse_seq(path.name)
            except ManifestError:
                continue
            parts = path.parent.relative_to(self.root).parts
            if len(parts) < 3:
                continue
            key = Key("/".join(parts[:-2]), parts[-2], parts[-1])
            entry = found.get(key.prefix)
            if entry is None:
                found[key.prefix] = key
        return [found[prefix] for prefix in sorted(found)]


_SLUG = re.compile(r"[^a-z0-9]+")


def repository_name(key: Key, namespace: str) -> str:
    """`obs-rust/obs-studio` + app + linux-x64 -> `<ns>/obs-rust-obs-studio-app-linux-x64`.

    The registry keeps one repository per key so a tag listing is a prefix-free
    list -- `/v2/<repo>/tags/list` has no server-side prefix filter, and paging
    a shared repository's tags to find one key would not be fast.
    """
    parts = [key.repo, key.family, key.platform]
    slug = _SLUG.sub("-", "-".join(parts).lower()).strip("-")
    return f"{namespace}/{slug}"


@dataclass
class Registry:
    host: str = "ghcr.io"
    namespace: str = "forge-ci"
    token: str = ""

    def __post_init__(self) -> None:
        if not self.token:
            self.token = os.environ.get("CACHECTL_REGISTRY_TOKEN", "")


class OciStore:
    """Layers as OCI blobs, manifests as tags: `<seq>` in a per-key repository.

    Push sequence per layer:
      1. POST /v2/<repo>/blobs/uploads/  -> Location
      2. PUT  <Location>?digest=sha256:.. with the layer bytes
      3. PUT  /v2/<repo>/manifests/<seq> with an artifact manifest whose single
         layer is the `application/vnd.oci.image.layer.v1.tar+zstd` blob and
         whose annotations carry identity/parent/kind (the same fields the
         `Manifest` holds, so a tag listing reconstructs manifests without
         fetching blobs).

    Pull sequence: GET /v2/<repo>/tags/list, keep `<seq>`-shaped names, GET the
    newest compatible manifest, GET its blob.

    Deliberately unimplemented here: the push path is written, but the registry
    credential that makes it work (`write:packages`) is not something this
    repository holds -- see docs/design.md, GitHub App.
    """

    LAYER_TYPE = "application/vnd.oci.image.layer.v1.tar+zstd"
    ARTIFACT_TYPE = "application/vnd.forge-ci.cache.layer.v1"

    def __init__(self, registry: Registry, key: Key) -> None:
        self.registry = registry
        self.key = key
        self.repository = repository_name(key, registry.namespace)

    def entries(self, key: Key | None = None) -> list[Manifest]:
        if key is not None and key != self.key:
            raise ValueError("an OciStore is bound to one key")
        tags = self._json(f"/v2/{self.repository}/tags/list") or {}
        names = tags.get("tags") or []
        manifests = [
            self._manifest_from_tag(name) for name in names if _is_seq_tag(name)
        ]
        return sorted((m for m in manifests if m is not None), key=lambda m: m.seq)

    def _manifest_from_tag(self, tag: str):
        document = self._json(
            f"/v2/{self.repository}/manifests/{tag}", accept=self.ARTIFACT_TYPE
        )
        if not document:
            return None
        annotations = document.get("annotations") or {}
        layer = (document.get("layers") or [{}])[0]
        try:
            return Manifest(
                key=self.key,
                seq=parse_seq(tag),
                identity=annotations.get("org.opencontainers.image.identity", ""),
                kind=annotations.get("org.forge-ci.cache.kind", ""),
                parent=annotations.get("org.forge-ci.cache.parent") or None,
                digest=layer.get("digest", ""),
                bytes=int(layer.get("size", 0)),
                created_at=annotations.get("org.opencontainers.image.created", ""),
                ref=annotations.get("org.opencontainers.image.ref.name", ""),
                source_sha=annotations.get("org.opencontainers.image.revision", ""),
                run_id=annotations.get("org.forge-ci.cache.run-id", ""),
            )
        except ManifestError:
            return None  # a foreign tag in our repository

    def _json(self, path: str, accept: str = "application/json"):
        request = urllib.request.Request(
            f"https://{self.registry.host}{path}",
            headers={"Accept": accept, **self._auth()},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def _auth(self) -> dict[str, str]:
        if not self.registry.token:
            raise RuntimeError("no registry token: set CACHECTL_REGISTRY_TOKEN")
        return {"Authorization": f"Bearer {self.registry.token}"}


def _is_seq_tag(name: str) -> bool:
    try:
        parse_seq(name)
    except ManifestError:
        return False
    return True


def store_for(root: str | Path | None, registry: Registry | None, key: Key):
    """`--store dir` for the filesystem backend, `--store oci` for the registry."""
    if registry is not None:
        return OciStore(registry, key)
    return FileStore(root or Path(".cachectl"))


__all__ = [
    "Entry",
    "FileStore",
    "OciStore",
    "Registry",
    "digest_of",
    "repository_name",
    "store_for",
]
