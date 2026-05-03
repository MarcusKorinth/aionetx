from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def _parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for segment in link_header.split(","):
        candidate = segment.strip()
        if 'rel="next"' not in candidate:
            continue
        start = candidate.find("<")
        end = candidate.find(">", start + 1)
        if start != -1 and end != -1:
            return candidate[start + 1 : end]
    return None


def _github_api_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aionetx-release-codeql-gate",
    }


def _fetch_paginated_list(*, url: str, token: str, response_name: str) -> list[dict[str, object]]:
    headers = _github_api_headers(token)

    items: list[dict[str, object]] = []
    while url:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, list):
                raise RuntimeError(f"Unexpected {response_name} response shape.")
            items.extend(item for item in payload if isinstance(item, dict))
            url = _parse_next_link(response.headers.get("Link"))
    return items


def _fetch_json_object(*, url: str, token: str, response_name: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers=_github_api_headers(token))
    with urllib.request.urlopen(request) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected {response_name} response shape.")
    return payload


def _iter_codeql_analyses(*, repository: str, token: str) -> list[dict[str, object]]:
    url = f"https://api.github.com/repos/{repository}/code-scanning/analyses?per_page=100"
    return _fetch_paginated_list(url=url, token=token, response_name="code scanning analyses")


def _iter_open_alerts(*, repository: str, ref: str, token: str) -> list[dict[str, object]]:
    encoded_ref = urllib.parse.quote(ref, safe="")
    url = (
        f"https://api.github.com/repos/{repository}/code-scanning/alerts"
        f"?state=open&ref={encoded_ref}&per_page=100"
    )
    return _fetch_paginated_list(url=url, token=token, response_name="code scanning alerts")


def _tool_name(analysis: dict[str, object]) -> str:
    tool = analysis.get("tool")
    if not isinstance(tool, dict):
        return ""
    name = tool.get("name")
    if not isinstance(name, str):
        return ""
    return name


def _analysis_ref(analysis: dict[str, object]) -> str:
    ref = analysis.get("ref")
    if not isinstance(ref, str):
        return ""
    return ref.strip()


def _is_branch_ref(ref: str) -> bool:
    return ref.startswith("refs/heads/")


def _git_ref_api_path(ref: str) -> str:
    if not ref.startswith("refs/"):
        raise RuntimeError(f"CodeQL analysis returned an unsupported ref: {ref!r}")
    ref_path = ref.removeprefix("refs/").strip()
    if not ref_path:
        raise RuntimeError("CodeQL analysis returned an empty analyzed ref.")
    return urllib.parse.quote(ref_path, safe="/")


def _current_ref_sha(*, repository: str, ref: str, token: str) -> str:
    ref_path = _git_ref_api_path(ref)
    url = f"https://api.github.com/repos/{repository}/git/ref/{ref_path}"
    payload = _fetch_json_object(url=url, token=token, response_name="git ref")
    object_value = payload.get("object")
    if not isinstance(object_value, dict):
        raise RuntimeError(f"Unexpected git ref response shape for {ref}.")
    sha = object_value.get("sha")
    if not isinstance(sha, str) or not sha.strip():
        raise RuntimeError(f"Git ref response for {ref} did not include an object SHA.")
    return sha.strip()


def _is_successful_codeql_analysis_for_commit(
    analysis: dict[str, object],
    *,
    release_sha: str,
) -> bool:
    if analysis.get("commit_sha") != release_sha:
        return False
    if _tool_name(analysis).casefold() != "codeql":
        return False
    ref = _analysis_ref(analysis)
    if not ref or not _is_branch_ref(ref):
        return False
    error = analysis.get("error")
    return not isinstance(error, str) or not error.strip()


def _find_release_codeql_analysis(
    analyses: list[dict[str, object]],
    *,
    release_sha: str,
) -> dict[str, object] | None:
    for analysis in analyses:
        if _is_successful_codeql_analysis_for_commit(analysis, release_sha=release_sha):
            return analysis
    return None


def _severity_rank(severity: str) -> int:
    return {"critical": 3, "high": 2, "medium": 1, "low": 0}.get(severity, -1)


def main() -> int:
    repository = _require_env("GITHUB_REPOSITORY")
    release_ref = _require_env("GITHUB_REF")
    release_sha = _require_env("GITHUB_SHA")
    token = _require_env("GITHUB_TOKEN")

    analyses = _iter_codeql_analyses(repository=repository, token=token)
    release_analysis = _find_release_codeql_analysis(analyses, release_sha=release_sha)
    if release_analysis is None:
        print(
            f"No successful CodeQL analysis was found for release commit {release_sha}. "
            f"Failing closed for {release_ref}.",
            file=sys.stderr,
        )
        return 1

    analyzed_ref = _analysis_ref(release_analysis)
    try:
        current_ref_sha = _current_ref_sha(
            repository=repository,
            ref=analyzed_ref,
            token=token,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if current_ref_sha != release_sha:
        print(
            f"Analyzed CodeQL ref {analyzed_ref} does not currently point at release "
            f"commit {release_sha} (current SHA: {current_ref_sha}). "
            f"Failing closed for {release_ref}.",
            file=sys.stderr,
        )
        return 1

    open_alerts = _iter_open_alerts(repository=repository, ref=analyzed_ref, token=token)
    blocking_alerts = []
    for alert in open_alerts:
        rule = alert.get("rule")
        severity = ""
        if isinstance(rule, dict):
            severity_value = rule.get("security_severity_level")
            if isinstance(severity_value, str):
                severity = severity_value.lower()
        if _severity_rank(severity) >= _severity_rank("high"):
            blocking_alerts.append(alert)

    print(
        f"CodeQL release gate inspected {len(open_alerts)} open alert(s) "
        f"for analyzed ref {analyzed_ref} at release commit {release_sha} "
        f"(release ref {release_ref})."
    )
    if not blocking_alerts:
        print("No open high/critical CodeQL alerts block this release commit.")
        return 0

    print(
        f"Found {len(blocking_alerts)} blocking CodeQL alert(s) with severity high/critical:",
        file=sys.stderr,
    )
    for alert in blocking_alerts:
        number = alert.get("number", "?")
        html_url = str(alert.get("html_url", ""))
        rule = alert.get("rule")
        rule_id = "<unknown>"
        severity = "<unknown>"
        if isinstance(rule, dict):
            rule_id = str(rule.get("id", "<unknown>"))
            severity = str(rule.get("security_severity_level", "<unknown>"))
        print(
            f"- alert #{number}: rule={rule_id} severity={severity} {html_url}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
