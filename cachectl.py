#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# ///
"""cachectl: publish and restore a build cache as a delta chain.

    # what would a publish do?
    cachectl.py plan   --repo obs-rust/obs-studio --family app \\
                       --platform linux-x64 --identity "$IDENTITY"

    # publish a layer (a `btrfs send` stream, or a tar+zstd made by `pack`)
    cachectl.py push   ...same key... --stream layer.stream [--ref refs/heads/master]

    # restore the newest compatible chain into a directory
    cachectl.py pull   ...same key... --identity "$IDENTITY" --dest out/ --apply tar

Stores: `--store dir=/path` (reference implementation, default `.cachectl`) or
`--store oci` (registry; needs CACHECTL_REGISTRY_TOKEN, see docs/design.md).
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cachelib import (
    FileStore,
    Key,
    ManifestError,
    OciStore,
    Registry,
    Snapshotter,
    digest_of,
    plan_push,
    plan_restore,
)
from cachelib.chain import describe
from cachelib.publish import publish_layer
from cachelib.retention import SweepPlan, apply_sweep, plan_sweep, sweep_all  # noqa: F401


def key_from(args: argparse.Namespace) -> Key:
    if not (args.repo and args.family and args.platform):
        raise SystemExit("--repo, --family and --platform are required (or --all for every key)")
    return Key(args.repo, args.family, args.platform)


def store_from(args: argparse.Namespace, key: Key | None):
    if args.store == "oci":
        if key is None:
            raise SystemExit("--store oci needs one key; --all works on a directory store")
        return OciStore(
            Registry(
                host=args.registry,
                namespace=args.namespace,
                scheme=getattr(args, "registry_scheme", "https"),
            ),
            key,
        )
    _, _, path = args.store.partition("=")
    return FileStore(Path(path or ".cachectl"))


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pack(directory: Path, out: Path) -> int:
    """A full layer for hosts without btrfs: sorted tar inside zstd."""
    script = [
        sys.executable,
        "-c",
        """
import sys, tarfile, pathlib
from compression import zstd
directory, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
entries = sorted(p for p in directory.rglob('*'))
with out.open('wb') as raw, zstd.open(raw, 'wb') as compressed:
    with tarfile.open(fileobj=compressed, mode='w|') as tar:
        for path in entries:
            info = tar.gettarinfo(str(path), arcname=path.relative_to(directory).as_posix())
            info.uid, info.gid, info.uname, info.gname = 0, 0, '', ''
            if info.isreg():
                with path.open('rb') as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)
""",
    ]
    import subprocess

    subprocess.run([*script, str(directory), str(out)], check=True)
    return out.stat().st_size


def unpack(stream: Path, directory: Path) -> int:
    from compression import zstd

    directory.mkdir(parents=True, exist_ok=True)
    count = 0
    with zstd.open(stream, "rb") as decompressed:
        with tarfile.open(fileobj=decompressed, mode="r|") as tar:
            for member in tar:
                tar.extract(member, directory, filter="data")
                count += 1
    return count


def command_plan(args: argparse.Namespace) -> int:
    key = key_from(args)
    store = store_from(args, key)
    entries = store.entries(key)
    push = plan_push(entries, key, args.identity)
    print(f"publish: {push.kind} s{push.seq:06d} ({push.reason})")
    if push.parent is not None:
        print(f"parent: {push.parent.digest}")
    restore = plan_restore(entries, args.identity)
    print(f"restore: {describe(restore)}")
    return 0


def command_push(args: argparse.Namespace) -> int:
    key = key_from(args)
    store = store_from(args, key)
    try:
        manifest = publish_layer(
            store,
            key,
            args.identity,
            Path(args.stream),
            ref=args.ref,
            source_sha=args.sha,
            run_id=args.run_id,
        )
    except FileNotFoundError as error:
        raise SystemExit(str(error)) from error
    print(
        f"published {manifest.kind} s{manifest.seq:06d} {manifest.digest} "
        f"({manifest.bytes} B) for {key.prefix} identity {args.identity}"
    )
    return 0


def command_pull(args: argparse.Namespace) -> int:
    key = key_from(args)
    store = store_from(args, key)
    plan = plan_restore(store.entries(key), args.identity, args.pin)
    if not plan.hit:
        print(f"miss: {plan.reason}", file=sys.stderr)
        return 3
    destination = Path(args.dest)
    with tempfile.TemporaryDirectory() as tmp:
        for index, layer in enumerate(plan.layers):
            local = store.get_blob(layer, Path(tmp) / f"layer-{index}")
            if args.apply == "btrfs":
                Snapshotter(destination.parent).receive(local, destination)
            elif args.apply == "tar":
                unpack(local, destination)
            else:
                print(f"downloaded {layer.kind} s{layer.seq:06d} ({layer.bytes} B), not applied")
    print(f"restored {key.prefix} identity {args.identity}: {describe(plan)}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    key = key_from(args)
    store = store_from(args, key)
    for manifest in store.entries(key):
        print(
            f"s{manifest.seq:06d}  {manifest.kind:5s}  {manifest.bytes:>12d}  "
            f"{manifest.identity}  {manifest.created_at}"
        )
    return 0


def parse_size(text: str | None) -> int | None:
    """`512MB`, `2GiB`, `1500000` -> bytes. None stays None (no cap)."""
    if text is None or text == "":
        return None
    suffixes = {
        "kb": 1 << 10,
        "kib": 1 << 10,
        "mb": 1 << 20,
        "mib": 1 << 20,
        "gb": 1 << 30,
        "gib": 1 << 30,
        "b": 1,
    }
    lowered = text.strip().lower()
    for suffix, factor in sorted(suffixes.items(), key=lambda item: -len(item[0])):
        if lowered.endswith(suffix):
            return int(float(lowered[: -len(suffix)]) * factor)
    return int(float(lowered))


def command_prune(args: argparse.Namespace) -> int:
    if args.all_keys:
        store = store_from(args, None)
        reports = sweep_all(
            store,
            keep=args.keep,
            max_bytes=parse_size(args.max_bytes),
            protect={d.strip() for d in args.protect.split(",") if d.strip()},
            dry_run=args.dry_run,
        )
        print(json.dumps(reports, indent=2))
        return 0
    key = key_from(args)
    store = store_from(args, key)
    protect = {digest.strip() for digest in args.protect.split(",") if digest.strip()}
    plan = plan_sweep(
        store.entries(key),
        key,
        keep=args.keep,
        max_bytes=parse_size(args.max_bytes),
        protect=protect,
    )
    report = apply_sweep(store, plan, dry_run=args.dry_run)
    print(json.dumps(report, indent=2))
    return 0


def command_pack(args: argparse.Namespace) -> int:
    size = pack(Path(args.directory), Path(args.out))
    print(f"{args.out}: {size} B, {digest_of(Path(args.out))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    stages = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "push", "pull", "list", "prune"):
        sub = stages.add_parser(name)
        # Optional so `prune --all` can sweep every key; key_from enforces them
        # everywhere a single key is required.
        sub.add_argument("--repo", default=None)
        sub.add_argument("--family", default=None)
        sub.add_argument("--platform", default=None)
        sub.add_argument("--store", default="dir=.cachectl")
        sub.add_argument("--registry", default="ghcr.io")
        sub.add_argument("--namespace", default="forge-ci")
        # A plain-http registry is a real deployment shape (localhost, private
        # mirror); https stays the default because ghcr is.
        sub.add_argument("--registry-scheme", default="https", choices=("https", "http"))
        if name in ("plan", "push", "pull"):
            sub.add_argument("--identity", required=True)
        if name == "push":
            sub.add_argument("--stream", required=True)
            sub.add_argument("--ref", default="")
            sub.add_argument("--sha", default="")
            sub.add_argument("--run-id", default="")
        if name == "prune":
            sub.add_argument(
                "--all",
                dest="all_keys",
                action="store_true",
                help="sweep every key in a directory store",
            )
            sub.add_argument(
                "--keep",
                type=int,
                default=2,
                help="chains to keep per identity (a chain is a base plus its deltas)",
            )
            sub.add_argument("--max-bytes", default=None, help="e.g. 500MB, 2GiB")
            sub.add_argument("--protect", default="", help="comma-separated digests to keep")
            sub.add_argument("--dry-run", action="store_true")
        if name == "pull":
            sub.add_argument("--dest", required=True)
            sub.add_argument("--apply", choices=("tar", "btrfs", "none"), default="tar")
            sub.add_argument("--pin", default=None, help="restore this exact layer digest")
        sub.set_defaults(
            handler={
                "plan": command_plan,
                "push": command_push,
                "pull": command_pull,
                "list": command_list,
                "prune": command_prune,
            }[name]
        )
    pack_parser = stages.add_parser("pack", help="build a full tar+zstd layer from a directory")
    pack_parser.add_argument("--directory", required=True)
    pack_parser.add_argument("--out", required=True)
    pack_parser.set_defaults(handler=command_pack)
    args = parser.parse_args()
    try:
        return args.handler(args)
    except ManifestError as error:
        print(f"cachectl: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
