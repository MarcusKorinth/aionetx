from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_TOOL_LOCKFILE = "requirements/ci-build-tools.txt"
WORKFLOW_PATHS = [
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_ci_build_tooling_uses_hash_locked_requirements_file() -> None:
    lockfile_text = _read(BUILD_TOOL_LOCKFILE)
    assert "--hash=sha256:" in lockfile_text

    install_command = f"python -m pip install --require-hashes -r {BUILD_TOOL_LOCKFILE}"
    for workflow_path in WORKFLOW_PATHS:
        workflow_text = _read(workflow_path)
        assert install_command in workflow_text
        assert "python -m pip install --upgrade pip build" not in workflow_text
        assert "python -m pip install build twine" not in workflow_text


def test_reproducible_build_docs_use_hash_locked_build_tooling() -> None:
    docs_text = _read("docs/reproducible_build.md")

    assert f"python -m pip install --require-hashes -r {BUILD_TOOL_LOCKFILE}" in docs_text
    assert "python -m pip install --upgrade pip build" not in docs_text
