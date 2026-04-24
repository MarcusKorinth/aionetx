"""
CI smoke runner for `examples/`.

Runs every example as a subprocess, asserts it exits 0 inside a short
timeout, and prints a one-line result per example. Intended for a
non-blocking CI lane that keeps the examples executable without gating
releases on environment-sensitive networking behavior.

Exit code is the number of failures (0 on success).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
REPO_ROOT = EXAMPLES_DIR.parent
SRC_DIR = REPO_ROOT / "src"
PER_EXAMPLE_TIMEOUT_SECONDS = 30

EXAMPLE_SCRIPTS: tuple[str, ...] = (
    "tcp_echo_server_and_client.py",
    "udp_round_trip.py",
    "tcp_framing_length_prefix.py",
    "tcp_reconnect_with_heartbeat.py",
    "backpressure_policies.py",
)


def run_one(script_name: str) -> bool:
    script_path = EXAMPLES_DIR / script_name
    if not script_path.is_file():
        print(f"[FAIL] {script_name}: missing file at {script_path}")
        return False

    env = dict(os.environ)
    pythonpath_parts = [str(SRC_DIR)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

    print(f"[ RUN] {script_name}")
    try:
        completed = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            env=env,
            text=True,
            timeout=PER_EXAMPLE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        print(f"[FAIL] {script_name}: timeout after {exc.timeout}s")
        return False

    if completed.returncode != 0:
        print(f"[FAIL] {script_name}: exit code {completed.returncode}")
        if completed.stdout:
            print("----- stdout -----")
            print(completed.stdout)
        if completed.stderr:
            print("----- stderr -----")
            print(completed.stderr)
        return False

    print(f"[ OK ] {script_name}")
    return True


def main() -> int:
    if not EXAMPLES_DIR.is_dir():
        print(f"examples directory not found at {EXAMPLES_DIR}")
        return 1

    failures = sum(0 if run_one(name) else 1 for name in EXAMPLE_SCRIPTS)
    total = len(EXAMPLE_SCRIPTS)
    print(f"\nexamples smoke: {total - failures}/{total} passed")
    return failures


if __name__ == "__main__":
    sys.exit(main())
