# forge-ci/cache

A build-cache store built the way a compiler cache should be built: content
addressed, delta-chained, and readable without a database.

This is the systematised home for the transport that
`obs-rust/obs-studio#5565` asks for. It replaces the per-repository ad-hoc
stores (GitHub Actions cache, a DigitalOcean Spaces bucket, release assets) with
one model, so a merge-queue run in any repo can *publish* a cache instead of
only consuming what master happened to save.

## The model

A cache entry is a **layer**, and layers form a **chain**:

```
key = (repo, family, platform)            e.g. (obs-rust/obs-studio, app, linux-x64)

s000001  full     identity A  ◄── base
s000002  delta    identity A  parent s000001
s000003  delta    identity A  parent s000002
s000004  delta    identity A  parent s000003
s000005  delta    identity A  parent s000004
s000006  delta    identity A  parent s000005   (5 deltas: the cap)
s000007  full     identity A  ◄── new base, chain closed
```

- **Restore** = latest compatible manifest, then walk `parent` back to the base
  and apply base → tip. No base, no restore: a broken chain is a miss, never a
  partial tree.
- **Publish** = decide full or delta from the existing chain, never from a
  clock. Five deltas is a *policy default* (`MAX_DELTA_CHAIN`), not a limit on
  the number of builds: past it, the next publish writes a thin full base.
- **Identity** is the compatibility contract (toolchain pin, target, build
  configuration, cache schema). A manifest whose identity does not match the
  consumer's is never selected, which is what makes "newest by number" safe.

Manifests are one small JSON document per entry, named by an **ordered integer**
(`s000123.json` / tag `s000123`), so "what is the newest cache for this key" is
a single prefix listing that returns in order — no database, no index rebuild,
no read-modify-write. Object stores and registries both list by prefix natively.

## Stores

| store | manifests | blobs | notes |
|---|---|---|---|
| `FileStore` | `<root>/<repo>/<family>/<platform>/s%06d.json` | `<root>/blobs/sha256/<hex>` | the reference implementation and the test backend |
| `OciStore` | image **tag** `s%06d` | OCI **layer** | container registry used as an artifact cache |

## Layers are btrfs send streams

The low-level point: a delta layer is not a diff of file lists, it is a
`btrfs send -p parent child` stream. `btrfs receive` applies it to produce a
byte-exact subvolume, and the stream is already compact because btrfs encodes
changed extents, not changed files.

That means the producer needs a btrfs filesystem with a snapshot per publish.
`snapshot.py` wraps the four commands (`subvolume snapshot`, `send`, `receive`,
`subvolume delete`) and reports `available() == False` where btrfs is absent, so
the filesystem-level proof runs on a host that has it (self-hosted runner) and
the manifest/chain layer stays testable everywhere.

## The server (deployment 2)

    uv run cache_server.py --store .cachectl --db sqlite.db --port 8787

    curl -s -XPOST localhost:8787/sessions -d '{"repo":"obs-rust/obs-studio","family":"app","platform":"linux-x64","identity":"rustc1.98.1-linux-x64-debug"}'
    curl -s -XPOST localhost:8787/sessions/<id>/restore         # decision + layer digests
    curl -s -XPOST localhost:8787/sessions/<id>/publish --data-binary @layer.tar.zst
    curl -s localhost:8787/blobs/sha256:<hex> -o layer.tar.zst   # the bytes it named
    curl -s -XPOST localhost:8787/sessions/<id>/close
    curl -s -XPOST localhost:8787/sessions/<id>/replay -d '{"dry_run":true}'

The server decides, the client transfers; a miss is a `200` with `"hit": false`,
never an error. Every operation appends to `sqlite.db`, and replay reports
per-step whether the recorded decision still holds.

`cachelib/client.py` is the same thing from Python, in five calls:

```python
with CacheClient("http://127.0.0.1:8787") as cache:
    session = cache.open("obs-rust/obs-studio", "app", "linux-x64", identity)
    decision = cache.restore(session)          # hit False is a result, not a raise
    for layer in decision.to_download:
        cache.download(layer.digest, workdir / f"layer-{layer.seq}")
    cache.publish(session, layer_bytes)
    cache.close(session)
```

The CLI and the service are two front doors onto one store, and
`tests/test_parity.py` holds them to it: a layer published by `cachectl.py` is
restored (byte-identical) by the service, a chain published by the service is
listed and planned by the CLI, and both agree on what an identity miss is.

## Status

Prototype. The manifest, chain, file-store, session and server layers are
implemented and tested in Python; the Rust crate (`rust/`) implements the same
model and is held to the shared corpus in `fixtures/conformance.json` by
`rust/tests/conformance.rs`, so the two decide identically today. Still missing
from the crate: the file store, blob digesting and the registry transport — it
decides, the caller moves bytes. the OCI transport has its naming and addressing implemented and its
push/pull sequences specified but not yet exercised against a registry —
`gh`'s OAuth token carries no `write:packages`, which is one of the reasons a
GitHub App is required (see `docs/design.md`).

## How projects consume this

Two deployments of the same model (`docs/design.md`): a **Rust library** for
embedded use (in-process, no daemon, for compiler launchers and runner agents)
and a **Python HTTP server** with session control, built-in session replay and
an append-only `sqlite.db` per session log.

Not by calling `cachectl.py`. **Rust projects get this through
`forge-ci/setup-soldr`**, which owns the restore-before-build and save-in-post
lifecycle they already run. It gains a `fleet-cache` tier (`auto` by default)
that computes the key and the identity itself, so a lane opts in with three
inputs and no credentials:

```
with:
  fleet-cache: auto
  fleet-cache-family: app
```

`docs/design.md` has the full surface, the "computed, not passed" rule it
follows, and the failure semantics (an unreachable store is a miss, never a red
build).
