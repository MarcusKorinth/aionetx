from __future__ import annotations

import argparse
import glob
import sys
import tarfile
import tomllib
import zipfile
from email.message import Message
from email.parser import Parser
from email.policy import default
from pathlib import Path, PurePosixPath


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate built wheel and sdist metadata without third-party tooling.",
    )
    parser.add_argument("patterns", nargs="+", help="Artifact glob pattern(s), for example dist/*.")
    parser.add_argument("--expected-name", help="Expected project Name metadata value.")
    parser.add_argument("--expected-version", help="Expected project Version metadata value.")
    return parser


def _load_expected_project_metadata() -> tuple[str, str]:
    with (_repo_root() / "pyproject.toml").open("rb") as pyproject:
        project = tomllib.load(pyproject)["project"]
    return str(project["name"]), str(project["version"])


def _expand_patterns(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(Path(match) for match in glob.glob(pattern))
    unique_paths = sorted(set(paths))
    if not unique_paths:
        raise SystemExit(f"No artifacts matched: {', '.join(patterns)}")
    return unique_paths


def _parse_metadata(raw_metadata: bytes) -> Message:
    return Parser(policy=default).parsestr(raw_metadata.decode("utf-8"))


def _require_core_metadata(
    path: Path,
    metadata: Message,
    *,
    expected_name: str,
    expected_version: str,
) -> None:
    for field in ("Metadata-Version", "Name", "Version", "Summary"):
        if not metadata.get(field):
            raise SystemExit(f"{path.name}: missing {field} metadata")

    if metadata["Name"] != expected_name:
        raise SystemExit(f"{path.name}: expected Name {expected_name}, got {metadata['Name']}")
    if metadata["Version"] != expected_version:
        raise SystemExit(
            f"{path.name}: expected Version {expected_version}, got {metadata['Version']}"
        )


def _validate_wheel(path: Path, *, expected_name: str, expected_version: str) -> None:
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise SystemExit(
                f"{path.name}: expected exactly one wheel METADATA file, found {len(metadata_names)}"
            )

        dist_info_dir = metadata_names[0].rsplit("/", 1)[0]
        for required_name in ("WHEEL", "RECORD"):
            required_path = f"{dist_info_dir}/{required_name}"
            if required_path not in names:
                raise SystemExit(f"{path.name}: missing {required_path}")

        metadata = _parse_metadata(wheel.read(metadata_names[0]))
        _require_core_metadata(
            path,
            metadata,
            expected_name=expected_name,
            expected_version=expected_version,
        )


def _validate_sdist(path: Path, *, expected_name: str, expected_version: str) -> None:
    with tarfile.open(path, "r:gz") as sdist:
        pkg_info_members = []
        for member in sdist.getmembers():
            parts = PurePosixPath(member.name).parts
            if member.isfile() and len(parts) == 2 and parts[1] == "PKG-INFO":
                pkg_info_members.append(member)
        if len(pkg_info_members) != 1:
            raise SystemExit(
                f"{path.name}: expected exactly one sdist PKG-INFO file, found {len(pkg_info_members)}"
            )

        extracted = sdist.extractfile(pkg_info_members[0])
        if extracted is None:
            raise SystemExit(f"{path.name}: could not read {pkg_info_members[0].name}")

        metadata = _parse_metadata(extracted.read())
        _require_core_metadata(
            path,
            metadata,
            expected_name=expected_name,
            expected_version=expected_version,
        )


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    default_name, default_version = _load_expected_project_metadata()
    expected_name = args.expected_name or default_name
    expected_version = args.expected_version or default_version

    found_wheel = False
    found_sdist = False
    for artifact_path in _expand_patterns(args.patterns):
        if artifact_path.suffix == ".whl":
            found_wheel = True
            _validate_wheel(
                artifact_path,
                expected_name=expected_name,
                expected_version=expected_version,
            )
        elif artifact_path.name.endswith(".tar.gz"):
            found_sdist = True
            _validate_sdist(
                artifact_path,
                expected_name=expected_name,
                expected_version=expected_version,
            )
        else:
            raise SystemExit(f"{artifact_path.name}: unsupported artifact type")

    if not found_wheel:
        raise SystemExit("No wheel artifact found")
    if not found_sdist:
        raise SystemExit("No sdist artifact found")

    print(f"Validated distribution metadata for {expected_name} {expected_version}.")


if __name__ == "__main__":
    sys.exit(main())
