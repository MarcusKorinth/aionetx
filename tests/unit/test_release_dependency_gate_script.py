from __future__ import annotations

from email.message import Message
import json
import runpy
import urllib.error
import urllib.request
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "ci" / "check_release_dependency_gate.py"
SCRIPT_GLOBALS = runpy.run_path(str(SCRIPT_PATH))

main = SCRIPT_GLOBALS["main"]


class FakeResponse:
    def __init__(self, payload: object, link: str | None = None) -> None:
        self._payload = payload
        self.headers = {"Link": link} if link else {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _set_release_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "MarcusKorinth/aionetx")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "token")


def test_release_dependency_gate_queries_open_high_critical_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_release_env(monkeypatch)
    requested_urls: list[str] = []

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        requested_urls.append(request.full_url)
        return FakeResponse([])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 0

    assert requested_urls == [
        "https://api.github.com/repos/MarcusKorinth/aionetx/dependabot/alerts"
        "?state=open,dismissed,auto_dismissed&severity=high,critical&per_page=100"
    ]


def test_release_dependency_gate_passes_when_no_blocking_alerts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_release_env(monkeypatch)

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        return FakeResponse([])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 0

    captured = capsys.readouterr()
    assert "No high/critical Dependabot alerts without accepted rationale" in captured.out


@pytest.mark.parametrize(
    ("ecosystem", "package_name"),
    [
        ("pip", "example-package"),
        ("github_actions", "actions/checkout"),
    ],
)
def test_release_dependency_gate_blocks_high_or_critical_alerts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ecosystem: str,
    package_name: str,
) -> None:
    _set_release_env(monkeypatch)

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        return FakeResponse(
            [
                {
                    "number": 8,
                    "state": "open",
                    "html_url": "https://github.com/MarcusKorinth/aionetx/security/dependabot/8",
                    "dependency": {
                        "package": {
                            "ecosystem": ecosystem,
                            "name": package_name,
                        }
                    },
                    "security_advisory": {"ghsa_id": "GHSA-test"},
                    "security_vulnerability": {
                        "package": {
                            "ecosystem": ecosystem,
                            "name": package_name,
                        },
                        "severity": "critical",
                        "vulnerable_version_range": "< 2.0",
                    },
                }
            ]
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 1

    captured = capsys.readouterr()
    assert "Found 1 blocking dependency alert(s)" in captured.err
    assert f"{ecosystem}/{package_name}" in captured.err
    assert "critical" in captured.err


@pytest.mark.parametrize("dismissed_reason", ["not_used", "inaccurate"])
def test_release_dependency_gate_accepts_dismissed_non_exploitable_alert_with_comment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dismissed_reason: str,
) -> None:
    _set_release_env(monkeypatch)

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        return FakeResponse(
            [
                {
                    "number": 9,
                    "state": "dismissed",
                    "dismissed_reason": dismissed_reason,
                    "dismissed_comment": "This package is not present in release artifacts.",
                    "dependency": {
                        "package": {
                            "ecosystem": "pip",
                            "name": "unused-package",
                        }
                    },
                    "security_advisory": {"ghsa_id": "GHSA-unused"},
                    "security_vulnerability": {
                        "package": {
                            "ecosystem": "pip",
                            "name": "unused-package",
                        },
                        "severity": "high",
                        "vulnerable_version_range": "< 3.0",
                    },
                }
            ]
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 0

    captured = capsys.readouterr()
    assert "No high/critical Dependabot alerts without accepted rationale" in captured.out


@pytest.mark.parametrize(
    ("dismissed_reason", "dismissed_comment"),
    [
        ("tolerable_risk", "Accepted until next dependency refresh."),
        ("not_used", ""),
        ("auto_dismissed", ""),
    ],
)
def test_release_dependency_gate_blocks_dismissed_alert_without_non_exploitability_rationale(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dismissed_reason: str,
    dismissed_comment: str,
) -> None:
    _set_release_env(monkeypatch)

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        return FakeResponse(
            [
                {
                    "number": 10,
                    "state": (
                        "auto_dismissed" if dismissed_reason == "auto_dismissed" else "dismissed"
                    ),
                    "dismissed_reason": (
                        None if dismissed_reason == "auto_dismissed" else dismissed_reason
                    ),
                    "dismissed_comment": dismissed_comment,
                    "html_url": "https://github.com/MarcusKorinth/aionetx/security/dependabot/10",
                    "dependency": {
                        "package": {
                            "ecosystem": "pip",
                            "name": "dismissed-package",
                        }
                    },
                    "security_advisory": {"ghsa_id": "GHSA-dismissed"},
                    "security_vulnerability": {
                        "package": {
                            "ecosystem": "pip",
                            "name": "dismissed-package",
                        },
                        "severity": "high",
                        "vulnerable_version_range": "< 4.0",
                    },
                }
            ]
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 1

    captured = capsys.readouterr()
    assert "dismissed-package" in captured.err
    assert "missing an explicit non-exploitability rationale" in captured.err


def test_release_dependency_gate_fails_closed_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_release_env(monkeypatch)

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            hdrs=Message(),
            fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 1

    captured = capsys.readouterr()
    assert "HTTP Error 403: Forbidden" in captured.err


def test_release_dependency_gate_fails_closed_on_url_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_release_env(monkeypatch)

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        raise urllib.error.URLError("network unavailable")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 1

    captured = capsys.readouterr()
    assert "network unavailable" in captured.err


def test_release_dependency_gate_fails_closed_on_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_release_env(monkeypatch)

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        raise json.JSONDecodeError("invalid json", doc="", pos=0)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 1

    captured = capsys.readouterr()
    assert "invalid json" in captured.err


def test_release_dependency_gate_fails_closed_on_unexpected_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_release_env(monkeypatch)

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        return FakeResponse({"message": "unexpected"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 1

    captured = capsys.readouterr()
    assert "Unexpected Dependabot alerts response shape" in captured.err


def test_release_dependency_gate_reads_paginated_dependabot_alerts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_release_env(monkeypatch)

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        if request.full_url.endswith("page=2"):
            return FakeResponse([])
        return FakeResponse(
            [],
            link='<https://api.github.com/repos/MarcusKorinth/aionetx/dependabot/alerts?page=2>; rel="next"',
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 0

    captured = capsys.readouterr()
    assert "inspected 0 high/critical Dependabot alert(s)" in captured.out
