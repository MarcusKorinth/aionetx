"""
Validate that every SHA-pinned action in .github/workflows/*.yml matches its ref comment.

For each line of the form:
    uses: owner/repo@<40-char-sha>  # vX.Y.Z
    uses: owner/repo@<40-char-sha>  # release/vX

the script calls the GitHub API to resolve the comment's upstream ref to a
commit SHA and compares it against the pinned SHA.

Exit codes:
    0 - all pins match (or GH_TOKEN is not set, in which case checks are skipped)
    1 - one or more pins do not match the expected upstream ref SHA
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

# Regex to capture every SHA-pinned GitHub action use, with an optional ref comment.
_PINNED_USES_RE = re.compile(
    r"uses:\s+(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?P<path>(?:/[A-Za-z0-9_.-]+)*)@(?P<sha>[0-9a-fA-F]{40})"
    r"(?:\s+#\s*(?P<ref>\S+))?"
)
_TAG_REF_RE = re.compile(r"^v[0-9A-Za-z][0-9A-Za-z._-]*$")
_RELEASE_HEAD_REF_RE = re.compile(r"^release/v[0-9A-Za-z][0-9A-Za-z._-]*$")

RefKind = Literal["tags", "heads"]


@dataclass(frozen=True)
class ActionPin:
    action: str
    owner: str
    repo: str
    pinned_sha: str
    ref: str
    ref_kind: RefKind


def _ref_kind_for_comment(ref: str) -> RefKind | None:
    if _TAG_REF_RE.fullmatch(ref):
        return "tags"
    if _RELEASE_HEAD_REF_RE.fullmatch(ref):
        return "heads"
    return None


def _gh_api(path: str, token: str) -> dict[str, object]:
    """Call `gh api <path>` and return parsed JSON."""
    result = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        text=True,
        env={**os.environ, "GH_TOKEN": token},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api {path!r} failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"gh api {path!r} returned unexpected JSON shape.")
    return cast(dict[str, object], payload)


def _resolve_ref_commit_sha(
    owner: str,
    repo: str,
    ref: str,
    ref_kind: RefKind,
    token: str,
) -> str | None:
    """Return the commit SHA that *ref* points to, or None if the ref is not found."""
    path = f"/repos/{owner}/{repo}/git/refs/{ref_kind}/{ref}"
    try:
        ref_data = _gh_api(path, token)
    except RuntimeError as exc:
        # 404 means the intended upstream ref is absent; report it as a pin failure.
        if "404" in str(exc) or "Not Found" in str(exc):
            return None
        raise

    obj = ref_data.get("object", {})
    if not isinstance(obj, dict):
        return None
    obj_type: str = obj.get("type", "")
    obj_sha: str = obj.get("sha", "")

    if obj_type == "commit":
        # Lightweight tag or branch head: the ref object is the commit.
        return obj_sha

    if obj_type == "tag":
        # Annotated tag: dereference the tag object to get the commit SHA.
        tag_obj = _gh_api(f"/repos/{owner}/{repo}/git/tags/{obj_sha}", token)
        tagged = tag_obj.get("object", {})
        if not isinstance(tagged, dict):
            return None
        tagged_type: str = tagged.get("type", "")
        tagged_sha: str = tagged.get("sha", "")
        if tagged_type == "commit":
            return tagged_sha

        # Edge case: tag points to another tag (rare); follow one more level.
        tag_obj2 = _gh_api(f"/repos/{owner}/{repo}/git/tags/{tagged_sha}", token)
        inner_obj = tag_obj2.get("object", {})
        if not isinstance(inner_obj, dict):
            return None
        inner: str = inner_obj.get("sha", "")
        return inner

    return obj_sha


def _parse_action_pin(line: str) -> ActionPin | str | None:
    match = _PINNED_USES_RE.search(line)
    if match is None:
        return None

    owner = match.group("owner")
    repo = match.group("repo")
    action = f"{owner}/{repo}{match.group('path')}"
    pinned_sha = match.group("sha").lower()
    ref = match.group("ref")

    if ref is None:
        return (
            f"missing upstream ref comment for {action}@{pinned_sha[:12]} "
            "(expected '# v...' or '# release/v...')"
        )

    ref_kind = _ref_kind_for_comment(ref)
    if ref_kind is None:
        return (
            f"unsupported upstream ref comment {ref!r} for {action}@{pinned_sha[:12]} "
            "(expected '# v...' or '# release/v...')"
        )

    return ActionPin(
        action=action,
        owner=owner,
        repo=repo,
        pinned_sha=pinned_sha,
        ref=ref,
        ref_kind=ref_kind,
    )


def main() -> int:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print(
            "GH_TOKEN is not set - skipping SHA pin validation "
            "(set GH_TOKEN to enable checks in local runs).",
            file=sys.stderr,
        )
        return 0

    repo_root = Path(__file__).parent.parent.parent
    workflow_dir = repo_root / ".github" / "workflows"
    workflow_files = sorted(workflow_dir.glob("*.yml"))

    if not workflow_files:
        print(f"No workflow files found under {workflow_dir}", file=sys.stderr)
        return 0

    failures: list[str] = []
    checked_count = 0

    for wf_file in workflow_files:
        content = wf_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), start=1):
            action_pin = _parse_action_pin(line)
            if action_pin is None:
                continue
            if isinstance(action_pin, str):
                failures.append(f"{wf_file.name}:{lineno}: {action_pin}")
                continue

            print(
                f"  checking {action_pin.action}@{action_pin.pinned_sha[:12]}... ({action_pin.ref})"
            )

            try:
                resolved_sha = _resolve_ref_commit_sha(
                    action_pin.owner,
                    action_pin.repo,
                    action_pin.ref,
                    action_pin.ref_kind,
                    token,
                )
            except RuntimeError as exc:
                failures.append(
                    f"{wf_file.name}:{lineno}: ERROR resolving "
                    f"{action_pin.owner}/{action_pin.repo} {action_pin.ref}: {exc}"
                )
                continue

            if resolved_sha is None:
                failures.append(
                    f"{wf_file.name}:{lineno}: ref {action_pin.ref!r} not found for "
                    f"{action_pin.owner}/{action_pin.repo}"
                )
                continue

            if resolved_sha != action_pin.pinned_sha:
                failures.append(
                    f"{wf_file.name}:{lineno}: SHA mismatch for "
                    f"{action_pin.owner}/{action_pin.repo} {action_pin.ref}\n"
                    f"    pinned:   {action_pin.pinned_sha}\n"
                    f"    resolved: {resolved_sha}"
                )
            else:
                checked_count += 1
                print(f"    OK ({action_pin.pinned_sha[:12]} == {action_pin.ref})")

    if failures:
        print("\nSHA pin validation FAILED:", file=sys.stderr)
        for msg in failures:
            print(f"  {msg}", file=sys.stderr)
        return 1

    print(
        f"\nAll {checked_count} SHA pins validated successfully across "
        f"{len(workflow_files)} workflow file(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
