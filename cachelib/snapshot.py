"""The low-level half: a delta layer is a btrfs send stream, not a file diff.

`btrfs send -p <parent snapshot> <child snapshot>` emits only the extents that
changed, and `btrfs receive` applies them to produce a byte-exact subvolume.
That is why the chain model works at all: a delta is small because the
filesystem knows what changed, not because a tool compared directory listings.

Constraints this module encodes rather than hides:

* the source must live on btrfs (`available()` answers that without guessing);
* snapshots are read-only (`-r`), so a build cannot mutate the tree a published
  layer was cut from;
* receive needs an empty destination subvolume, so a restore is apply-to-empty,
  never apply-over-live-tree.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

BTRFS_MAGIC = "9123683e"


class SnapshotUnavailable(RuntimeError):
    """The path is not on btrfs, or the tools are missing."""


def magic(path: str | Path) -> str:
    result = subprocess.run(
        ["stat", "-f", "-c", "%t", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def available(path: str | Path) -> bool:
    return magic(path) == BTRFS_MAGIC


def _run(*args: str) -> None:
    result = subprocess.run(
        ["btrfs", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise SnapshotUnavailable(f"btrfs {' '.join(args)}: {result.stderr.strip()}")


@dataclass(frozen=True)
class Snapshotter:
    """Snapshot/send/receive for one btrfs root."""

    root: Path

    def __post_init__(self) -> None:
        if not available(self.root):
            raise SnapshotUnavailable(
                f"{self.root} is not on btrfs (magic {magic(self.root) or 'unknown'}); "
                "delta layers need btrfs send/receive"
            )

    def create(self, source: str | Path, destination: str | Path) -> Path:
        _run("subvolume", "snapshot", "-r", str(source), str(destination))
        return Path(destination)

    def send(
        self, previous: str | Path | None, current: str | Path, stream: str | Path
    ) -> int:
        """Write a full (previous=None) or incremental stream; return its size."""
        stream = Path(stream)
        command = ["btrfs", "send"]
        if previous is not None:
            command += ["-p", str(previous)]
        command.append(str(current))
        with stream.open("wb") as sink:
            result = subprocess.run(
                command, stdout=sink, stderr=subprocess.PIPE, check=False
            )
        if result.returncode != 0:
            raise SnapshotUnavailable(f"btrfs send: {result.stderr.decode().strip()}")
        return stream.stat().st_size

    def receive(self, stream: str | Path, destination: str | Path) -> None:
        """Apply a stream into DESTINATION (which must be empty or absent)."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        with Path(stream).open("rb") as source:
            result = subprocess.run(
                ["btrfs", "receive", str(destination)],
                stdin=source,
                capture_output=True,
                text=True,
                check=False,
            )
        if result.returncode != 0:
            raise SnapshotUnavailable(f"btrfs receive: {result.stderr.strip()}")

    def delete(self, path: str | Path) -> None:
        _run("subvolume", "delete", str(path))
