"""
Validate that every SHA-pinned action in .github/workflows/*.yml matches its tag comment.

For each line of the form:
    uses: owner/repo@<40-char-sha>  # vX.Y.Z

the script calls the GitHub API to resolve the tag to a commit SHA and compares it
against the pinned SHA.

Exit codes:
    0 — all pins match (or GH_TOKEN is not set, in which case checks are skipped)
    1 — one or more pins do not match the expected tag SHA
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Regex to capture:  owner/repo@<sha>  # vX.Y.Z
_PIN_RE = re.compile(
    r"uses:\s+(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"@(?P<sha>[0-9a-f]{40})\s+#\s+(?P<tag>v\S+)"
)


def _gh_api(path: str, token: str) -> dict:  # type: ignore[type-arg]
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
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def _resolve_tag_commit_sha(owner: str, repo: str, tag: str, token: str) -> str | None:
    """Return the commit SHA that *tag* points to, or None if the tag is not found."""
    path = f"/repos/{owner}/{repo}/git/refs/tags/{tag}"
    try:
        ref_data = _gh_api(path, token)
    except RuntimeError as exc:
        # 404 → tag not found; surface the error but keep going
        if "404" in str(exc) or "Not Found" in str(exc):
            return None
        raise

    obj = ref_data.get("object", {})
    obj_type: str = obj.get("type", "")
    obj_sha: str = obj.get("sha", "")

    if obj_type == "commit":
        # Lightweight tag: the ref object IS the commit
        return obj_sha

    if obj_type == "tag":
        # Annotated tag: dereference the tag object to get the commit SHA
        tag_obj = _gh_api(f"/repos/{owner}/{repo}/git/tags/{obj_sha}", token)
        tagged = tag_obj.get("object", {})
        tagged_type: str = tagged.get("type", "")
        tagged_sha: str = tagged.get("sha", "")
        if tagged_type == "commit":
            return tagged_sha
        # Edge case: tag points to another tag (rare); follow one more level
        tag_obj2 = _gh_api(f"/repos/{owner}/{repo}/git/tags/{tagged_sha}", token)
        inner: str = tag_obj2.get("object", {}).get("sha", "")
        return inner

    return obj_sha


def main() -> int:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print(
            "GH_TOKEN is not set — skipping SHA pin validation "
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

    for wf_file in workflow_files:
        content = wf_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), start=1):
            m = _PIN_RE.search(line)
            if not m:
                continue

            owner = m.group("owner")
            repo = m.group("repo")
            pinned_sha = m.group("sha")
            tag = m.group("tag")

            print(f"  checking {owner}/{repo}@{pinned_sha[:12]}… ({tag})")

            try:
                resolved_sha = _resolve_tag_commit_sha(owner, repo, tag, token)
            except RuntimeError as exc:
                failures.append(
                    f"{wf_file.name}:{lineno}: ERROR resolving {owner}/{repo} {tag}: {exc}"
                )
                continue

            if resolved_sha is None:
                failures.append(
                    f"{wf_file.name}:{lineno}: tag {tag!r} not found for {owner}/{repo}"
                )
                continue

            if resolved_sha != pinned_sha:
                failures.append(
                    f"{wf_file.name}:{lineno}: SHA mismatch for {owner}/{repo} {tag}\n"
                    f"    pinned:   {pinned_sha}\n"
                    f"    resolved: {resolved_sha}"
                )
            else:
                print(f"    OK ({pinned_sha[:12]} == {tag})")

    if failures:
        print("\nSHA pin validation FAILED:", file=sys.stderr)
        for msg in failures:
            print(f"  {msg}", file=sys.stderr)
        return 1

    print(f"\nAll SHA pins validated successfully across {len(workflow_files)} workflow file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
