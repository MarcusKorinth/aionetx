from __future__ import annotations

import os
import pathlib
import re
import tomllib


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[a-zA-Z0-9.\-+]+)?$")


def read_pyproject_version(pyproject_path: pathlib.Path = pathlib.Path("pyproject.toml")) -> str:
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    try:
        version = data["project"]["version"]
    except KeyError:
        raise AssertionError("Could not read [project].version from pyproject.toml.") from None
    if not isinstance(version, str):
        raise AssertionError(f"[project].version in pyproject.toml is not a string: {version!r}")
    return version


def validate_tag_release(tag_name: str, pyproject_version: str) -> None:
    expected_tag = f"v{pyproject_version}"
    if tag_name != expected_tag:
        raise AssertionError(
            f"Tag/version mismatch: tag is {tag_name!r}, pyproject version is "
            f"{pyproject_version!r} (expected tag {expected_tag!r})"
        )


def validate_manual_testpypi_release(
    *,
    event_name: str,
    publish_target: str,
    declared_release_version: str,
    pyproject_version: str,
) -> None:
    if event_name != "workflow_dispatch" or publish_target != "testpypi":
        raise AssertionError(
            "Manual TestPyPI provenance check can only run for "
            "workflow_dispatch with publish_target=testpypi."
        )
    if not declared_release_version:
        raise AssertionError(
            "Manual TestPyPI publish requires RELEASE_VERSION input (expected pyproject version)."
        )
    if not SEMVER_RE.match(declared_release_version):
        raise AssertionError(
            f"Invalid RELEASE_VERSION format {declared_release_version!r}; expected semver-like format."
        )
    if declared_release_version != pyproject_version:
        raise AssertionError(
            f"Release provenance mismatch: RELEASE_VERSION is {declared_release_version!r}, "
            f"but pyproject version is {pyproject_version!r}."
        )


def main() -> None:
    pyproject_version = read_pyproject_version()
    tag_name = os.getenv("TAG_NAME", "")
    event_name = os.getenv("EVENT_NAME", "")
    publish_target = os.getenv("PUBLISH_TARGET", "")
    declared_release_version = os.getenv("RELEASE_VERSION", "")

    if event_name == "workflow_dispatch" and publish_target == "testpypi":
        validate_manual_testpypi_release(
            event_name=event_name,
            publish_target=publish_target,
            declared_release_version=declared_release_version,
            pyproject_version=pyproject_version,
        )
        print(
            "Manual TestPyPI provenance verified: "
            f"RELEASE_VERSION={declared_release_version} matches pyproject version {pyproject_version}."
        )
        return

    if tag_name:
        validate_tag_release(tag_name=tag_name, pyproject_version=pyproject_version)
        print(f"Tag/version match verified: {tag_name}")
        return

    raise AssertionError(
        "Unsupported provenance check context. Expected either a tag-triggered release "
        "or workflow_dispatch with publish_target=testpypi."
    )


if __name__ == "__main__":
    main()
