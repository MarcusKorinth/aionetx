from __future__ import annotations

import re
import runpy
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "ci" / "check_sha_pins.py"
SCRIPT_GLOBALS = runpy.run_path(str(SCRIPT_PATH))

main = SCRIPT_GLOBALS["main"]

PINNED_USES_RE = re.compile(
    r"uses:\s+(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?:/[A-Za-z0-9_.-]+)*@(?P<sha>[0-9a-f]{40})\s+#\s*(?P<ref>\S+)"
)


def _make_temp_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workflow_text: str,
) -> None:
    script_path = tmp_path / "scripts" / "ci" / "check_sha_pins.py"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("", encoding="utf-8")

    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(workflow_text, encoding="utf-8")

    monkeypatch.setitem(main.__globals__, "__file__", str(script_path))


def _pinned_refs_from_workflows() -> dict[tuple[str, str, str], str]:
    pins: dict[tuple[str, str, str], str] = {}
    for workflow_path in (PROJECT_ROOT / ".github" / "workflows").glob("*.yml"):
        for line in workflow_path.read_text(encoding="utf-8").splitlines():
            match = PINNED_USES_RE.search(line)
            if match is None:
                continue
            pins[
                (
                    match.group("owner"),
                    match.group("repo"),
                    match.group("ref"),
                )
            ] = match.group("sha")
    return pins


def _fake_ref_api(
    requested_paths: list[str],
    pins: dict[tuple[str, str, str], str],
):
    def fake_gh_api(path: str, token: str) -> dict[str, object]:
        del token
        requested_paths.append(path)
        marker = "/git/refs/"
        if marker not in path:
            raise AssertionError(f"unexpected API path: {path}")

        repo_path, ref_path = path.split(marker, maxsplit=1)
        _, repos_marker, owner, repo = repo_path.split("/")
        assert repos_marker == "repos"
        ref_kind, ref_name = ref_path.split("/", maxsplit=1)
        if ref_kind == "tags":
            key = (owner, repo, ref_name)
        elif ref_kind == "heads":
            key = (owner, repo, ref_name)
        else:
            raise AssertionError(f"unexpected ref kind: {ref_kind}")

        try:
            sha = pins[key]
        except KeyError as exc:
            raise AssertionError(f"missing fake SHA for {key}") from exc
        return {"object": {"type": "commit", "sha": sha}}

    return fake_gh_api


def test_current_release_publish_pins_are_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    requested_paths: list[str] = []
    pins = _pinned_refs_from_workflows()
    monkeypatch.setitem(
        main.__globals__,
        "_gh_api",
        _fake_ref_api(requested_paths, pins),
    )

    assert main() == 0

    assert any(
        path.endswith("/repos/actions/checkout/git/refs/tags/v6") for path in requested_paths
    )
    assert any(
        path.endswith("/repos/github/codeql-action/git/refs/tags/v4") for path in requested_paths
    )
    assert any(
        path.endswith("/repos/pypa/gh-action-pypi-publish/git/refs/heads/release/v1")
        for path in requested_paths
    )


def test_missing_ref_comment_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _make_temp_repo(
        monkeypatch,
        tmp_path,
        "jobs:\n"
        "  check:\n"
        "    steps:\n"
        "      - uses: example/action@1111111111111111111111111111111111111111\n",
    )
    monkeypatch.setenv("GH_TOKEN", "fake-token")

    assert main() == 1

    captured = capsys.readouterr()
    assert "missing upstream ref comment" in captured.err


def test_unsupported_ref_comment_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _make_temp_repo(
        monkeypatch,
        tmp_path,
        "jobs:\n"
        "  check:\n"
        "    steps:\n"
        "      - uses: example/action@1111111111111111111111111111111111111111 # main\n",
    )
    monkeypatch.setenv("GH_TOKEN", "fake-token")

    assert main() == 1

    captured = capsys.readouterr()
    assert "unsupported upstream ref comment 'main'" in captured.err
