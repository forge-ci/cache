"""btrfs is the delta engine: prove the absence is detected, skip the rest.

The send/receive half needs a btrfs filesystem. This host is ext4, so the test
asserts what is actually checkable here -- that `available()` tells the truth and
that the snapshotter refuses rather than silently writing a full tree labelled a
delta. A btrfs host runs the rest.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cachelib.snapshot import (
    BTRFS_MAGIC,
    Snapshotter,
    SnapshotUnavailable,
    available,
    magic,
)


class DetectionTest(unittest.TestCase):
    def test_a_filesystem_reports_its_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(magic(tmp))

    def test_available_agrees_with_the_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(available(tmp), magic(tmp) == BTRFS_MAGIC)

    def test_the_snapshotter_refuses_a_non_btrfs_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            if available(tmp):
                self.skipTest("this host is btrfs")
            with self.assertRaises(SnapshotUnavailable):
                Snapshotter(Path(tmp))


class SendReceiveTest(unittest.TestCase):
    """Runs only where a btrfs subvolume can be created."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        if not available(self.base):
            self.skipTest("no btrfs here: the low-level proof runs on a btrfs host")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_delta_stream_is_smaller_than_the_full_one(self):
        root = self.base
        live = root / "live"
        live.mkdir()
        (live / "unchanged.bin").write_bytes(b"u" * (1 << 20))
        (live / "changing.bin").write_bytes(b"before")
        snapshotter = Snapshotter(root)

        first = snapshotter.create(live, root / "snap1")
        full = snapshotter.send(None, first, root / "full.stream")
        (live / "changing.bin").write_bytes(b"after")
        second = snapshotter.create(live, root / "snap2")
        delta = snapshotter.send(first, second, root / "delta.stream")

        self.assertLess(delta, full / 10, "a 6-byte change must not re-send 1 MiB")
        snapshotter.receive(root / "full.stream", root / "restored")
        snapshotter.receive(root / "delta.stream", root / "restored")
        self.assertEqual((root / "restored" / "changing.bin").read_bytes(), b"after")


if __name__ == "__main__":
    unittest.main()
