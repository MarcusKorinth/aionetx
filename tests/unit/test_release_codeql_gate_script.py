from __future__ import annotations

import json
import runpy
import urllib.request
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "ci" / "check_release_codeql_gate.py"
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


def _set_release_env(monkeypatch: pytest.MonkeyPatch, *, sha: str = "release-sha") -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "MarcusKorinth/aionetx")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v1.2.3")
    monkeypatch.setenv("GITHUB_SHA", sha)
    monkeypatch.setenv("GITHUB_TOKEN", "token")


def test_tag_release_fails_closed_without_codeql_analysis_for_release_sha(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_release_env(monkeypatch, sha="release-sha")

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        if "/code-scanning/analyses" in request.full_url:
            return FakeResponse(
                [
                    {
                        "commit_sha": "different-sha",
                        "ref": "refs/heads/main",
                        "tool": {"name": "CodeQL"},
                        "error": "",
                    }
                ]
            )
        return FakeResponse([])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 1

    captured = capsys.readouterr()
    assert "No successful CodeQL analysis was found for release commit release-sha" in (
        captured.err
    )


def test_tag_release_fails_closed_when_only_codeql_analysis_uses_tag_ref(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_release_env(monkeypatch, sha="release-sha")

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        if "/code-scanning/analyses" in request.full_url:
            return FakeResponse(
                [
                    {
                        "commit_sha": "release-sha",
                        "ref": "refs/tags/v1.2.3",
                        "tool": {"name": "CodeQL"},
                        "error": "",
                    }
                ]
            )
        if "/git/ref/tags/v1.2.3" in request.full_url:
            return FakeResponse({"object": {"sha": "release-sha", "type": "commit"}})
        return FakeResponse([])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 1

    captured = capsys.readouterr()
    assert "No successful CodeQL analysis was found for release commit release-sha" in (
        captured.err
    )


def test_tag_release_queries_alerts_for_analyzed_release_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_release_env(monkeypatch, sha="release-sha")
    requested_urls: list[str] = []

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        requested_urls.append(request.full_url)
        if "/code-scanning/analyses" in request.full_url:
            return FakeResponse(
                [
                    {
                        "commit_sha": "release-sha",
                        "ref": "refs/heads/main",
                        "tool": {"name": "CodeQL"},
                        "error": "",
                    }
                ]
            )
        if "/git/ref/heads/main" in request.full_url:
            return FakeResponse({"object": {"sha": "release-sha", "type": "commit"}})
        return FakeResponse([])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 0

    alert_urls = [url for url in requested_urls if "/code-scanning/alerts" in url]
    assert alert_urls == [
        "https://api.github.com/repos/MarcusKorinth/aionetx/"
        "code-scanning/alerts?state=open&ref=refs%2Fheads%2Fmain&per_page=100"
    ]


def test_tag_release_fails_closed_when_analyzed_ref_moved(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_release_env(monkeypatch, sha="release-sha")

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        if "/code-scanning/analyses" in request.full_url:
            return FakeResponse(
                [
                    {
                        "commit_sha": "release-sha",
                        "ref": "refs/heads/main",
                        "tool": {"name": "CodeQL"},
                        "error": "",
                    }
                ]
            )
        if "/git/ref/heads/main" in request.full_url:
            return FakeResponse({"object": {"sha": "newer-sha", "type": "commit"}})
        return FakeResponse([])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 1

    captured = capsys.readouterr()
    assert "does not currently point at release commit release-sha" in captured.err


def test_tag_release_blocks_high_or_critical_alerts_for_current_analyzed_ref(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_release_env(monkeypatch, sha="release-sha")

    def fake_urlopen(request: urllib.request.Request) -> FakeResponse:
        if "/code-scanning/analyses" in request.full_url:
            return FakeResponse(
                [
                    {
                        "commit_sha": "release-sha",
                        "ref": "refs/heads/main",
                        "tool": {"name": "CodeQL"},
                        "error": "",
                    }
                ]
            )
        if "/git/ref/heads/main" in request.full_url:
            return FakeResponse({"object": {"sha": "release-sha", "type": "commit"}})
        return FakeResponse(
            [
                {
                    "number": 7,
                    "html_url": "https://github.com/MarcusKorinth/aionetx/security/code-scanning/7",
                    "rule": {
                        "id": "py/example",
                        "security_severity_level": "high",
                    },
                }
            ]
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert main() == 1

    captured = capsys.readouterr()
    assert "Found 1 blocking CodeQL alert(s)" in captured.err
