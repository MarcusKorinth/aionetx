from __future__ import annotations

import json
from json import JSONDecodeError
import os
import sys
import urllib.error
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
        "User-Agent": "aionetx-release-dependency-gate",
    }


def _fetch_paginated_list(*, url: str, token: str, response_name: str) -> list[dict[str, object]]:
    headers = _github_api_headers(token)

    items: list[dict[str, object]] = []
    next_url: str | None = url
    while next_url:
        request = urllib.request.Request(next_url, headers=headers)
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, list):
                raise RuntimeError(f"Unexpected {response_name} response shape.")
            items.extend(item for item in payload if isinstance(item, dict))
            next_url = _parse_next_link(response.headers.get("Link"))
    return items


def _iter_open_blocking_alerts(*, repository: str, token: str) -> list[dict[str, object]]:
    url = (
        f"https://api.github.com/repos/{repository}/dependabot/alerts"
        "?state=open,dismissed,auto_dismissed&severity=high,critical&per_page=100"
    )
    return _fetch_paginated_list(url=url, token=token, response_name="Dependabot alerts")


def _alert_package(alert: dict[str, object]) -> str:
    dependency = alert.get("dependency")
    if not isinstance(dependency, dict):
        return "<unknown>"
    package = dependency.get("package")
    if not isinstance(package, dict):
        return "<unknown>"
    ecosystem = package.get("ecosystem")
    name = package.get("name")
    if not isinstance(name, str) or not name.strip():
        return "<unknown>"
    if isinstance(ecosystem, str) and ecosystem.strip():
        return f"{ecosystem}/{name}"
    return name


def _alert_severity(alert: dict[str, object]) -> str:
    vulnerability = alert.get("security_vulnerability")
    if not isinstance(vulnerability, dict):
        return "<unknown>"
    severity = vulnerability.get("severity")
    return severity if isinstance(severity, str) and severity.strip() else "<unknown>"


def _alert_advisory_id(alert: dict[str, object]) -> str:
    advisory = alert.get("security_advisory")
    if not isinstance(advisory, dict):
        return "<unknown>"
    ghsa_id = advisory.get("ghsa_id")
    return ghsa_id if isinstance(ghsa_id, str) and ghsa_id.strip() else "<unknown>"


def _alert_version_range(alert: dict[str, object]) -> str:
    vulnerability = alert.get("security_vulnerability")
    if not isinstance(vulnerability, dict):
        return "<unknown>"
    version_range = vulnerability.get("vulnerable_version_range")
    return (
        version_range if isinstance(version_range, str) and version_range.strip() else "<unknown>"
    )


def _alert_state(alert: dict[str, object]) -> str:
    state = alert.get("state")
    return state if isinstance(state, str) and state.strip() else "<unknown>"


def _dismissal_has_non_exploitability_rationale(alert: dict[str, object]) -> bool:
    reason = alert.get("dismissed_reason")
    comment = alert.get("dismissed_comment")
    return (
        reason in {"not_used", "inaccurate"} and isinstance(comment, str) and bool(comment.strip())
    )


def _is_blocking_alert(alert: dict[str, object]) -> bool:
    state = _alert_state(alert)
    if state == "open":
        return True
    if state in {"dismissed", "auto_dismissed"}:
        return not _dismissal_has_non_exploitability_rationale(alert)
    return True


def main() -> int:
    try:
        repository = _require_env("GITHUB_REPOSITORY")
        release_ref = _require_env("GITHUB_REF")
        token = _require_env("GITHUB_TOKEN")
        inspected_alerts = _iter_open_blocking_alerts(repository=repository, token=token)
    except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    blocking_alerts = [alert for alert in inspected_alerts if _is_blocking_alert(alert)]
    print(
        f"Dependency release gate inspected {len(inspected_alerts)} "
        f"high/critical Dependabot alert(s) for {repository} at {release_ref}."
    )
    if not blocking_alerts:
        print("No high/critical Dependabot alerts without accepted rationale block this release.")
        return 0

    print(
        f"Found {len(blocking_alerts)} blocking dependency alert(s) with severity high/critical:",
        file=sys.stderr,
    )
    for alert in blocking_alerts:
        number = alert.get("number", "?")
        html_url = str(alert.get("html_url", ""))
        rationale_note = ""
        if _alert_state(alert) in {"dismissed", "auto_dismissed"}:
            rationale_note = " missing an explicit non-exploitability rationale"
        print(
            f"- alert #{number}: package={_alert_package(alert)} "
            f"severity={_alert_severity(alert)} advisory={_alert_advisory_id(alert)} "
            f"range={_alert_version_range(alert)} state={_alert_state(alert)}"
            f"{rationale_note} {html_url}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
