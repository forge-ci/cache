#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependency = []
# ///
"""The repo's gate: `./ci.py` runs everything, `./ci.py <gate>` runs one lane.

Lanes:
    test    the manifest/chain/store suites (no network, no btrfs needed)
    lint    ruff on the package and the CLI
    all     both (the default)
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def run(command: list[str], env: dict[str, str] | None = None) -> int:
    print(f"+ {' '.join(command)}", flush=True)
    return subprocess.run(
        command, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})}
    ).returncode


def gate_test() -> int:
    return run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."])


def gate_lint() -> int:
    ruff = subprocess.run(
        ["uv", "run", "--no-project", "ruff", "--version"],
        capture_output=True,
        text=True,
    )
    if ruff.returncode != 0:
        print("ruff unavailable; skipping lint", file=sys.stderr)
        return 0
    code = run(
        [
            "uv",
            "run",
            "--no-project",
            "ruff",
            "check",
            "cachelib",
            "cachectl.py",
            "tests",
        ]
    )
    if code != 0:
        return code
    return run(
        [
            "uv",
            "run",
            "--no-project",
            "ruff",
            "format",
            "--check",
            "cachelib",
            "cachectl.py",
            "tests",
        ]
    )


GATES = {"test": gate_test, "lint": gate_lint}


def main(argv: list[str]) -> int:
    requested = argv[1:] or ["all"]
    lanes = list(GATES) if requested == ["all"] else requested
    failed: list[str] = []
    for lane in lanes:
        if lane not in GATES:
            print(
                f"unknown gate {lane!r}; known: {', '.join(GATES)}, all",
                file=sys.stderr,
            )
            return 2
        if GATES[lane]() != 0:
            failed.append(lane)
    if failed:
        print(f"gates failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"gates passed: {', '.join(lanes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
