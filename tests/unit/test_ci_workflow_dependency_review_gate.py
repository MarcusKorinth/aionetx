from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _job_block(workflow_text: str, job_name: str) -> str:
    header = f"  {job_name}:\n"
    start = workflow_text.index(header)
    body_start = start + len(header)
    next_job = re.search(r"^  [A-Za-z0-9_-]+:\n", workflow_text[body_start:], re.MULTILINE)
    end = body_start + next_job.start() if next_job is not None else len(workflow_text)
    return workflow_text[start:end]


def _direct_needs(job_block: str) -> set[str]:
    inline_needs = re.search(r"^    needs: \[(?P<needs>[^\]]+)\]$", job_block, re.MULTILINE)
    if inline_needs is not None:
        return {need.strip() for need in inline_needs.group("needs").split(",")}

    needs_start = job_block.index("    needs:\n") + len("    needs:\n")
    steps_start = job_block.index("    steps:\n", needs_start)
    needs_block = job_block[needs_start:steps_start]
    return {
        line.removeprefix("      - ").strip()
        for line in needs_block.splitlines()
        if line.startswith("      - ")
    }


def _job_if_expression(job_block: str) -> str:
    match = re.search(r"^    if: (?P<expression>.+)$", job_block, re.MULTILINE)
    assert match is not None

    expression = match.group("expression").strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        return expression.removeprefix("${{").removesuffix("}}").strip()
    return expression


def _run_script(job_block: str) -> str:
    run_start = job_block.index("        run: |\n") + len("        run: |\n")
    script_lines: list[str] = []
    for line in job_block[run_start:].splitlines():
        if line.startswith("          "):
            script_lines.append(line.removeprefix("          "))
            continue
        if line.strip() == "":
            script_lines.append("")
            continue
        break
    return "\n".join(script_lines)


def _run_ci_profile_gate(
    script: str,
    *,
    event_name: str,
    required_suite_result: str = "success",
    build_install_result: str = "success",
    dependency_review_result: str = "success",
) -> subprocess.CompletedProcess[str]:
    if shutil.which("bash") is None:
        pytest.skip("bash is required to execute the GitHub Actions gate script")

    env = {
        **os.environ,
        "EVENT_NAME": event_name,
        "REQUIRED_SUITE_RESULT": required_suite_result,
        "BUILD_INSTALL_RESULT": build_install_result,
        "DEPENDENCY_REVIEW_RESULT": dependency_review_result,
    }
    return subprocess.run(
        ["bash", "-c", script],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )


def test_ci_profile_gate_accounts_for_pull_request_dependency_review() -> None:
    workflow_text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")

    dependency_review = _job_block(workflow_text, "dependency_review")
    ci_profile_gate = _job_block(workflow_text, "ci_profile_gate")
    gate_before_steps = ci_profile_gate.split("    steps:\n", maxsplit=1)[0]

    assert _job_if_expression(dependency_review) == "github.event_name == 'pull_request'"
    assert _direct_needs(ci_profile_gate) == {
        "required_suite_with_coverage",
        "build_install_verify",
        "dependency_review",
    }
    assert _job_if_expression(ci_profile_gate) == "always()"
    assert "needs.dependency_review.result == 'success'" not in gate_before_steps
    assert "DEPENDENCY_REVIEW_RESULT: ${{ needs.dependency_review.result }}" in ci_profile_gate
    assert 'if [ "$DEPENDENCY_REVIEW_RESULT" = "success" ]; then' in ci_profile_gate
    assert "failed=1" in ci_profile_gate
    assert 'exit "$failed"' in ci_profile_gate
    assert "required CI trust gate profile passed" in ci_profile_gate
    assert "dependency review is pull-request only" in ci_profile_gate


def test_ci_profile_gate_script_requires_dependency_review_on_pull_requests() -> None:
    workflow_text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
    script = _run_script(_job_block(workflow_text, "ci_profile_gate"))

    passed = _run_ci_profile_gate(script, event_name="pull_request")
    assert passed.returncode == 0
    assert "- dependency review result: success" in passed.stdout
    assert "- required CI trust gate profile passed" in passed.stdout

    failed = _run_ci_profile_gate(
        script,
        event_name="pull_request",
        dependency_review_result="failure",
    )
    assert failed.returncode == 1
    assert "::error::Dependency review result: failure" in failed.stdout
    assert "- required CI trust gate profile passed" not in failed.stdout


def test_ci_profile_gate_script_allows_skipped_dependency_review_outside_prs() -> None:
    workflow_text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
    script = _run_script(_job_block(workflow_text, "ci_profile_gate"))

    result = _run_ci_profile_gate(
        script,
        event_name="push",
        dependency_review_result="skipped",
    )

    assert result.returncode == 0
    assert "- dependency review is pull-request only" in result.stdout
    assert "- dependency review was not part of this push run" in result.stdout
    assert "- required CI trust gate profile passed" in result.stdout


@pytest.mark.parametrize(
    ("required_suite_result", "build_install_result", "expected_error"),
    [
        ("failure", "success", "::error::Required suite with coverage result: failure"),
        ("success", "skipped", "::error::Build and install verification result: skipped"),
    ],
)
def test_ci_profile_gate_script_fails_when_required_non_policy_gates_do_not_pass(
    required_suite_result: str,
    build_install_result: str,
    expected_error: str,
) -> None:
    workflow_text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
    script = _run_script(_job_block(workflow_text, "ci_profile_gate"))

    result = _run_ci_profile_gate(
        script,
        event_name="push",
        required_suite_result=required_suite_result,
        build_install_result=build_install_result,
        dependency_review_result="skipped",
    )

    assert result.returncode == 1
    assert expected_error in result.stdout
    assert "- required CI trust gate profile passed" not in result.stdout
