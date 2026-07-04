from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = PROJECT_ROOT / ".github" / "ISSUE_TEMPLATE" / "infrastructure.yml"


def test_maintenance_issue_template_routes_repo_upkeep_proposals() -> None:
    text = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert 'title: "[infra] "' in text
    assert 'labels: ["type:chore", "area:repo", "triage"]' in text
    assert "Feature request" in text
    assert "feature_request.yml" in text
    assert "transport runtime behavior" in text
    assert "CI / GitHub Actions" in text
    assert "Linting / formatting" in text
    assert "Packaging / pyproject.toml" in text
    assert "Developer environment setup" in text
    assert "This proposal concerns project infrastructure, tooling, or maintenance" in text

    proposal_start = text.index("    id: proposal")
    proposal_end = text.index("  - type:", proposal_start + 1)
    proposal_block = text[proposal_start:proposal_end]
    assert "render:" not in proposal_block
