from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"
BUILD_EPOCH_ACTION_PATH = REPO_ROOT / ".github" / "actions" / "capture-build-epoch" / "action.yml"
DEV_INSTALL_ACTION_PATH = (
    REPO_ROOT / ".github" / "actions" / "install-dev-dependencies" / "action.yml"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _job_block(workflow_text: str, job_name: str) -> str:
    header = f"  {job_name}:\n"
    start = workflow_text.index(header)
    body_start = start + len(header)
    next_job = re.search(r"^  [A-Za-z0-9_-]+:\n", workflow_text[body_start:], re.MULTILINE)
    end = body_start + next_job.start() if next_job is not None else len(workflow_text)
    return workflow_text[start:end]


def _action_step_block(action_text: str, step_name: str) -> str:
    header = f"    - name: {step_name}\n"
    start = action_text.index(header)
    body_start = start + len(header)
    next_step = action_text.find("\n    - name: ", body_start)
    end = next_step if next_step != -1 else len(action_text)
    return action_text[start:end]


def _assert_local_actions_run_after_checkout(job_block: str) -> None:
    local_action_uses = [
        match.start() for match in re.finditer(r"uses: \./\.github/actions/", job_block)
    ]
    if not local_action_uses:
        return

    checkout_position = job_block.find("uses: actions/checkout@")
    assert checkout_position != -1
    assert all(checkout_position < action_position for action_position in local_action_uses)


def test_release_reuses_setup_phases_without_hiding_publish_steps() -> None:
    workflow_text = _read(RELEASE_WORKFLOW_PATH)

    jobs_that_capture_build_epoch = {
        "cross_platform_portability_smoke",
        "verify_reproducible_build",
        "release_codeql_gate",
        "verify_and_build",
    }
    jobs_that_install_dev_dependencies = {
        "api_symbol_stability",
        "ruff_checks",
        "mypy_supported_bounds",
        "required_behavior_tests",
        "behavior_critical_tests",
        "integration_tests_supported_bounds",
        "integration_smoke_tests_intermediate_versions",
        "integration_semantic_tests_intermediate_versions",
        "reliability_smoke_tests",
        "multicast_contract_required",
        "required_suite_with_coverage",
        "verify_and_build",
    }

    for job_name in jobs_that_capture_build_epoch:
        job = _job_block(workflow_text, job_name)
        assert "uses: ./.github/actions/capture-build-epoch" in job
        _assert_local_actions_run_after_checkout(job)

    for job_name in jobs_that_install_dev_dependencies:
        job = _job_block(workflow_text, job_name)
        assert "uses: ./.github/actions/install-dev-dependencies" in job
        _assert_local_actions_run_after_checkout(job)

    verify_and_build = _job_block(workflow_text, "verify_and_build")
    assert 'upgrade-pip: "false"' not in verify_and_build
    assert 'use-python-module: "true"' not in verify_and_build
    assert "git log -1 --format=%ct" not in workflow_text
    assert "pip install -e .[dev]" not in workflow_text

    assert "uses: pypa/gh-action-pypi-publish@" in _job_block(workflow_text, "publish")
    assert "uses: pypa/gh-action-pypi-publish@" in _job_block(workflow_text, "publish_testpypi")
    assert "uses: softprops/action-gh-release@" in _job_block(workflow_text, "github_release")
    assert "uses: softprops/action-gh-release@" in _job_block(
        workflow_text,
        "github_release_dry_run",
    )
    assert "uses: anchore/sbom-action@" in _job_block(workflow_text, "generate_sbom")
    assert "uses: actions/attest-build-provenance@" in _job_block(
        workflow_text,
        "attest_provenance",
    )


def test_capture_build_epoch_action_has_narrow_release_contract() -> None:
    action_text = _read(BUILD_EPOCH_ACTION_PATH)

    assert "name: Capture deterministic build timestamp" in action_text
    assert "description: Capture the current commit timestamp for reproducible release builds." in (
        action_text
    )
    assert "epoch:" in action_text
    assert 'git log -1 --format=%ct "$GITHUB_SHA"' in action_text
    assert 'echo "epoch=${epoch}" >> "$GITHUB_OUTPUT"' in action_text


def test_install_dev_dependencies_action_has_narrow_release_contract() -> None:
    action_text = _read(DEV_INSTALL_ACTION_PATH)
    build_tools_step = _action_step_block(
        action_text,
        "Install pinned CI build tools",
    )
    dev_tools_step = _action_step_block(
        action_text,
        "Install pinned CI development tools",
    )
    editable_install_step = _action_step_block(
        action_text,
        "Install project in editable mode without dependency resolution",
    )

    assert "name: Install project development dependencies" in action_text
    assert "description: Install pinned CI build/dev tools and the editable package." in (
        action_text
    )
    assert "python -m pip install --upgrade pip" not in action_text
    assert "run: python -m pip install --require-hashes -r requirements/ci-build-tools.txt" in (
        build_tools_step
    )
    assert "run: python -m pip install --require-hashes -r requirements/ci-dev-tools.txt" in (
        dev_tools_step
    )
    assert "run: python -m pip install --no-build-isolation --no-deps -e ." in (
        editable_install_step
    )
    assert "pip install -e .[dev]" not in action_text
