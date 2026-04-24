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


def _iter_open_alerts(*, repository: str, ref: str, token: str) -> list[dict[str, object]]:
    encoded_ref = urllib.parse.quote(ref, safe="")
    url = (
        f"https://api.github.com/repos/{repository}/code-scanning/alerts"
        f"?state=open&ref={encoded_ref}&per_page=100"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aionetx-release-codeql-gate",
    }

    alerts: list[dict[str, object]] = []
    while url:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, list):
                raise RuntimeError("Unexpected code scanning alerts response shape.")
            alerts.extend(item for item in payload if isinstance(item, dict))
            url = _parse_next_link(response.headers.get("Link"))
    return alerts


def _severity_rank(severity: str) -> int:
    return {"critical": 3, "high": 2, "medium": 1, "low": 0}.get(severity, -1)


def main() -> int:
    repository = _require_env("GITHUB_REPOSITORY")
    ref = _require_env("GITHUB_REF")
    token = _require_env("GITHUB_TOKEN")

    open_alerts = _iter_open_alerts(repository=repository, ref=ref, token=token)
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

    print(f"CodeQL release gate inspected {len(open_alerts)} open alert(s) for {ref}.")
    if not blocking_alerts:
        print("No open high/critical CodeQL alerts block this release ref.")
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
