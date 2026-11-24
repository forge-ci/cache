# Design: a fleet-wide build cache

## What is broken today

Three stores, none of which is a cache system:

1. **GitHub Actions cache** — hard 10 GiB per repository, writes only on the
   default branch, entries immutable. In `obs-rust/obs-studio` declared families
   already reserve 9.911 GiB of the 10 GiB budget (91 MiB of headroom), so
   nothing new fits; phase A of obs-rust/obs-studio#1801 evicted the six
   app-build families (5.71 GiB) to stay inside it. Every app build now starts
   cold.
2. **A DigitalOcean Spaces bucket** — wired into 13 setup-soldr callsites in the
   same repo and exercised by **nothing**, because the tier rides the Actions
   build family (`save-cache=false` disables both) and every lane runs
   `build-cache: false`.
3. **Release assets** — `ci/precache.py` (obs-rust/obs-studio#5541) can write and
   verify a cache as an asset, but it is a per-repository script with a free-form
   identity, no chain, no retention, and no producer.

The structural consequence: **the merge queue can never fill a cache.** GitHub
gates every Actions-cache save on `push` to the default branch, so a
`merge_group` run can only consume what master already published. A repository
whose cache is empty is cold on every merge-queue run no matter how many builds
it does. That is the defect this system exists to remove.

## What this system does differently

- **Publishing is not privileged.** Any run may publish, because a publish is an
  append-only write to a per-key sequence, not a mutation of a shared namespace.
  A merge-queue run that produces a good cache publishes it; the next run of the
  same identity restores it.
- **Nothing is evicted by policy.** Entries are immutable and addressed by
  sequence. Retention is a separate, explicit sweep (`keep N newest per key`,
  plus a size cap), never a side effect of someone else's publish.
- **A miss is visible.** A restore either produces the full chain or reports a
  miss with a reason. No partially applied tree is ever reported as a hit.
- **Compatibility is explicit.** `identity` (toolchain pin, target, build
  configuration, cache schema) is part of the manifest. "Newest" only ever
  competes among entries with the same identity.
- **Deltas are filesystem-level.** A delta layer is a `btrfs send -p` stream, so
  it encodes changed extents rather than changed files. Five deltas on top of a
  base, then a new base (`MAX_DELTA_CHAIN = 5`), bounds both restore cost and
  upload size.

## Layout

```
Key     = (repo, family, platform)            e.g. (obs-rust/obs-studio, app, linux-x64)
Entry   = <key>/<seq>                          seq: s000001, s000002, ... (ordered names)
Layer   = one blob: full tree, or delta from parent
Manifest= one JSON per entry: identity, kind, parent digest, layer digest+size,
          created_at, ref, source sha, run id
```

One prefix listing per key returns every entry in publish order — that is the
entire index, and it is why no database is needed for the read path.

## Stores

| backend | entries | blobs |
|---|---|---|
| `FileStore` | `<root>/<repo>/<family>/<platform>/s%06d.json` | `<root>/blobs/sha256/<hex>` |
| `OciStore` | tag `s%06d` in `ghcr.io/forge-ci/<repo>-<family>-<platform>` | OCI layer, `application/vnd.oci.image.layer.v1.tar+zstd`, metadata in manifest annotations |

The container registry is the fleet backend because it is the one store that is
already paid for, has per-repository quota, and accepts multi-GB pushes from CI
without a 10 GiB repository-wide budget. Each key gets its own repository: the
registry v2 tag listing has no server-side prefix filter, so a shared repository
would mean paging every other key's tags to find one.

## Decisions taken (and why)

**Distributed manifests, not a database.** An ordered-name listing is O(entries)
on the read side and needs no coordinator, no schema migration, and no uptime
commitment. Concurrent publishers resolve a sequence collision by retry, not by
a lock. A database buys one thing — a transactional "give me the next number" —
and costs a service to run, monitor and back up.

*Prototype concession:* if orchestration (nightly scheduling, retention sweeps,
cross-repo fan-out) turns out to need atomic coordination, a server database is
allowed **for the orchestration plane only**, with the store path left
distributed. That decision must be revisited on evidence, not preference — see
the follow-up issue "revisit the orchestration store: DB vs distributed".

**A GitHub App, not a PAT.** Publishing needs three things a PAT cannot give
here: (a) `packages: write` scoped to a single cache namespace rather than to the
user's whole account; (b) an installation token that works from `merge_group`
runs, which have no default-branch checkout; (c) a webhook target for
`workflow_run` so a nightly precache can be driven by what actually ran, not by
a cron guess. `app/github-app-manifest.json` is the template; it is meant to be
re-created per installation org and adapted (permissions and events) as the
system grows.

**Nightly precache + branch seeding.** A fresh branch has no cache and cannot
create one under the Actions rules. The nightly job builds the common
configurations and publishes them, and a `seed` command pushes a chain onto a
ref-specific identity so a new branch's first run is warm. This is the
"pre-cache with a nightly, seed on a fresh branch" requirement, expressed as two
operations of the same store.

## Open problems

1. **btrfs availability.** Delta layers need a btrfs filesystem with snapshots.
   GitHub-hosted runners are ext4, so delta efficiency is only real on a
   self-hosted btrfs host (the Dell E640 pool). `snapshot.available()` reports
   this honestly and the tests skip rather than pretend. Until a btrfs host is
   in the pool, layers degrade to full tars and the chain is a correctness
   mechanism, not a bandwidth saving.
2. **Registry credential.** `gh`'s OAuth token carries no `write:packages`, so
   the OCI push path cannot be exercised from this checkout. It is implemented
   and specified, not yet proven against a registry.
3. **Identity derivation.** `identity` must be computed by one shared function
   (`native_cache_identity.py` in obs-studio is the current candidate) or a
   producer will publish under an identity no consumer computes. This is the
   reason the integration surface is setup-soldr and not this CLI: see below.
4. **Retention.** A rolling store grows without bound until a sweep exists:
   keep the newest N per key, always keep the newest full base, drop deltas whose
   children are gone.

## Integration: setup-soldr is the only entry point

Rust projects do not call `cachectl.py`. They do not pin a CLI, template a
shell step, or thread an identity through their workflows. **Integration is
through `forge-ci/setup-soldr`**, which owns the restore-before-build and
save-in-post lifecycle in every repo that already uses it — 15 callsites in
`obs-rust/obs-studio` alone.

The rule that makes this work: anything a workflow author could get wrong is
computed, not passed.

| Concern | Who decides |
|---|---|
| key (`repo`, `family`, `platform`) | setup-soldr, from the event, the runner and the input family |
| `identity` | setup-soldr, from the same native toolchain identity it already captures (`native_cache_identity.py`) |
| restore/publish timing | setup-soldr's existing main/post phases |
| publish permission | the GitHub App installation token, not a per-repo secret |
| retention, chain length, base cuts | the store's policy (`MAX_DELTA_CHAIN`), never a workflow input |

### The API extension

setup-soldr grows one tier alongside the ones it already has (`actions/cache`,
the S3 `s3-bucket` tier), with the same no-credential-means-off rule that makes
`s3-bucket` safe today:

```
- uses: forge-ci/setup-soldr@<sha>
  with:
    # The whole integration. No keys, no identity, no policy.
    fleet-cache: auto            # auto | off   (auto = on when the App is installed)
    fleet-cache-family: app      # the build type; defaults to the job's lane name
    fleet-cache-publish: auto    # auto publishes from any ref that built successfully
```

Why these three and no more:

- **`auto` is the default and it must be the frictionless one.** `auto` restores
  when a compatible entry exists and publishes when the build succeeded, on any
  ref — including `merge_group`. That is the entire point: under GitHub's rules
  a merge-queue run can never fill the Actions cache, so a tier that only
  publishes from master reproduces the bug we are removing.
- **Publishing is unprivileged**, so `auto` is safe: an entry is an append-only
  sequence, so a wrong or useless publish costs store space (bounded by
  retention) and never corrupts what another run will restore.
- **`fleet-cache-family` is the only thing a human names**, because only a human
  knows that a job is "app" and not "tier2" — and getting it wrong only splits
  keys, it does not break correctness.
- **The identity is never an input.** If a workflow supplied it, producers and
  consumers could disagree and the mismatch would present as a permanent miss
  with no error. setup-soldr computes it from what it already probes.

### What setup-soldr keeps doing

The existing tiers do not move. `save-cache`, `save-cache-remote`, `s3-bucket`,
`cache-preset` and `build-cache` keep their current meanings, and `fleet-cache`
is a fourth tier that no-ops when its App is absent or its inputs are off. A
repo on `s3-bucket` gains nothing and loses nothing until it opts in, which is
what makes the migration reversible.

### Failure semantics

- App unreachable, registry down, or entry absent → restore **misses** and the
  build proceeds cold, exactly as if the tier were off. A cache must never be
  able to fail a build.
- Publish fails → a warning, never a red job.
- An identity mismatch is a miss with a logged reason (the CLI's exit 3), not a
  silent partial restore.
- `fleet-cache: off` is a hard switch, so a repo can leave during an incident
  without a revert.

## Migration

`obs-rust/obs-studio` first, because it has the hardest case (three app platforms,
a 10 GiB-bound cache, and merge-queue runs that are structurally cold):

1. extend setup-soldr with the `fleet-cache` tier (API above), App installed;
2. `macos-build`, `windows-build`, `ubuntu-build` flip to `fleet-cache: auto` —
   the lanes that need it get the store without touching their steps;
3. publish app-build caches for linux-x64, macOS arm64/x86_64, windows x64/arm64
   from master, nightly **and** merge_queue;
4. measure merge-queue wall time and compiler hit counters, not step colours;
5. only then retire the residual setup-soldr cache machinery (`s3-bucket`, the
   undeclared build family), which is also what frees the 10 GiB budget.
