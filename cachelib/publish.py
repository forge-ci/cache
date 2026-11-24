"""Publishing a layer, for any backend.

The CLI used to inline "plan, write the blob, write the manifest", which quietly
assumed the filesystem store. A registry has no separate manifest write — the tag
*is* the manifest and it goes up with the blob — so the two-step shape is a
FileStore detail, not part of publishing.

Everything that publishes goes through here, so "publish" means the same thing no
matter which store is behind it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .chain import plan_push
from .manifest import Key, Manifest
from .store import FileStore, OciStore, digest_of


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def publish_layer(
    store,
    key: Key,
    identity: str,
    stream: str | Path,
    *,
    ref: str = "",
    source_sha: str = "",
    run_id: str = "",
) -> Manifest:
    """Publish one layer and return the manifest that describes it."""
    stream = Path(stream)
    if not stream.is_file():
        raise FileNotFoundError(f"{stream} is not a file")
    entries = store.entries(key)
    plan = plan_push(entries, key, identity)
    manifest = Manifest(
        key=key,
        seq=plan.seq,
        identity=identity,
        kind=plan.kind,
        parent=plan.parent.digest if plan.parent else None,
        digest=digest_of(stream),
        bytes=stream.stat().st_size,
        created_at=now(),
        ref=ref,
        source_sha=source_sha,
        run_id=run_id,
    )
    store.put_blob(manifest, stream)
    # A registry publishes the tag with the blob; a filesystem store needs the
    # entry written separately.
    if isinstance(store, FileStore) and not isinstance(store, OciStore):
        store.put_manifest(manifest)
    return manifest


__all__ = ["now", "publish_layer"]
