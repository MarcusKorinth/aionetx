from __future__ import annotations

import argparse
import glob
import subprocess
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install exactly one built artifact matched by a glob pattern.",
    )
    parser.add_argument(
        "pattern", help="Glob pattern for an artifact path (for example: dist/*.whl)."
    )
    parser.add_argument(
        "--force-reinstall",
        action="store_true",
        help="Pass --force-reinstall to pip install.",
    )
    parser.add_argument(
        "--no-deps",
        action="store_true",
        help="Pass --no-deps to pip install.",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    matches = sorted(glob.glob(args.pattern))
    if len(matches) != 1:
        raise SystemExit(
            f"Expected exactly one artifact for pattern {args.pattern!r}, found {len(matches)}: {matches}"
        )

    pip_command = [sys.executable, "-m", "pip", "install"]
    if args.force_reinstall:
        pip_command.append("--force-reinstall")
    if args.no_deps:
        pip_command.append("--no-deps")
    pip_command.append(matches[0])

    subprocess.check_call(pip_command)


if __name__ == "__main__":
    main()
