#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependency = []
# ///
"""The repo's gate: `./ci.py` runs everything, `./ci.py <gate>` runs one lane.

Lanes:
    test    the manifest/chain/store/server suites (no network, no btrfs needed)
    lint    ruff on the package, the CLI and the server
    rust    the Rust crate plus the shared conformance corpus
    all     every lane (the default)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(
    command: list[str], env: dict[str, str] | None = None, cwd: Path | None = None
) -> int:
    print(f"+ {' '.join(command)}", flush=True)
    return subprocess.run(
        command,
        cwd=cwd or ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
        check=False,
    ).returncode


def gate_test() -> int:
    return run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."])


def gate_lint() -> int:
    ruff = subprocess.run(
        ["uv", "run", "--no-project", "ruff", "--version"],
        capture_output=True,
        text=True,
        check=False,
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
            "cache_server.py",
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
            "cache_server.py",
            "tests",
        ]
    )


def gate_rust() -> int:
    """The crate plus the corpus both implementations are held to.

    `cargo` is hook-blocked in this estate, so go through soldr; fall back to
    cargo only where soldr is absent (a bare clone on a foreign host).
    """
    manifest = ROOT / "rust" / "Cargo.toml"
    if not manifest.exists():
        print("no rust/ crate; skipping", file=sys.stderr)
        return 0
    for driver in (["soldr", "cargo"], ["cargo"]):
        if (
            subprocess.run(
                ["sh", "-c", f"command -v {driver[0]}"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        ):
            return run(
                [*driver, "test", "--manifest-path", str(manifest)], cwd=ROOT / "rust"
            )
    print("no cargo available; skipping the rust lane", file=sys.stderr)
    return 0


GATES = {"test": gate_test, "lint": gate_lint, "rust": gate_rust}


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
