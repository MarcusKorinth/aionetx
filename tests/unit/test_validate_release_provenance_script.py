from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "ci" / "validate_release_provenance.py"
SCRIPT_GLOBALS = runpy.run_path(str(SCRIPT_PATH))

validate_tag_release = SCRIPT_GLOBALS["validate_tag_release"]
validate_manual_testpypi_release = SCRIPT_GLOBALS["validate_manual_testpypi_release"]
read_pyproject_version = SCRIPT_GLOBALS["read_pyproject_version"]


def run_script_cli(
    *,
    tag_name: str = "",
    event_name: str = "",
    publish_target: str = "",
    release_version: str = "",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "TAG_NAME": tag_name,
            "EVENT_NAME": event_name,
            "PUBLISH_TARGET": publish_target,
            "RELEASE_VERSION": release_version,
        }
    )
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        env=env,
    )


def test_validate_tag_release_accepts_matching_tag() -> None:
    validate_tag_release(tag_name="v1.2.3", pyproject_version="1.2.3")


def test_validate_tag_release_rejects_mismatch() -> None:
    with pytest.raises(AssertionError, match="Tag/version mismatch"):
        validate_tag_release(tag_name="v1.2.4", pyproject_version="1.2.3")


def test_validate_manual_testpypi_release_requires_dispatch_target() -> None:
    with pytest.raises(AssertionError, match="workflow_dispatch"):
        validate_manual_testpypi_release(
            event_name="push",
            publish_target="testpypi",
            declared_release_version="1.2.3",
            pyproject_version="1.2.3",
        )


def test_validate_manual_testpypi_release_rejects_empty_version() -> None:
    with pytest.raises(AssertionError, match="requires RELEASE_VERSION"):
        validate_manual_testpypi_release(
            event_name="workflow_dispatch",
            publish_target="testpypi",
            declared_release_version="",
            pyproject_version="1.2.3",
        )


def test_validate_manual_testpypi_release_rejects_mismatch() -> None:
    with pytest.raises(AssertionError, match="Release provenance mismatch"):
        validate_manual_testpypi_release(
            event_name="workflow_dispatch",
            publish_target="testpypi",
            declared_release_version="1.2.4",
            pyproject_version="1.2.3",
        )


def test_validate_manual_testpypi_release_accepts_match() -> None:
    validate_manual_testpypi_release(
        event_name="workflow_dispatch",
        publish_target="testpypi",
        declared_release_version="1.2.3",
        pyproject_version="1.2.3",
    )


def test_script_cli_succeeds_for_manual_testpypi_with_matching_version() -> None:
    version = read_pyproject_version()
    result = run_script_cli(
        event_name="workflow_dispatch",
        publish_target="testpypi",
        release_version=version,
    )

    assert result.returncode == 0
    assert "Manual TestPyPI provenance verified" in result.stdout


def test_script_cli_fails_for_manual_testpypi_with_missing_version() -> None:
    result = run_script_cli(
        event_name="workflow_dispatch",
        publish_target="testpypi",
    )

    assert result.returncode != 0
    assert "requires RELEASE_VERSION" in result.stderr


def test_script_cli_ignores_tag_name_for_manual_testpypi_context() -> None:
    version = read_pyproject_version()
    result = run_script_cli(
        tag_name="main",
        event_name="workflow_dispatch",
        publish_target="testpypi",
        release_version=version,
    )

    assert result.returncode == 0
    assert "Manual TestPyPI provenance verified" in result.stdout


def test_script_cli_succeeds_for_matching_tag_context() -> None:
    version = read_pyproject_version()
    result = run_script_cli(
        tag_name=f"v{version}",
        event_name="push",
    )

    assert result.returncode == 0
    assert f"Tag/version match verified: v{version}" in result.stdout


def test_script_cli_fails_for_unsupported_context() -> None:
    result = run_script_cli(
        event_name="workflow_dispatch",
        publish_target="none",
    )

    assert result.returncode != 0
    assert "Unsupported provenance check context" in result.stderr
