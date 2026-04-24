<!--
Thanks for contributing to aionetx.

Before opening the PR, please:
  - Run `ruff check .` and `mypy src` locally.
  - Run the relevant test slice (at minimum: `pytest -q -m "not multicast and not slow and not integration"`).
  - Update `CHANGELOG.md` under `[Unreleased]` if the change is user-visible.
  - If this changes a documented public contract, update `README.md` and the relevant docs in the same PR.

Keep the PR focused on one concern. Refactoring-only and behavior-change PRs are easier to review when they are not mixed.
-->

## Summary

<!-- One or two sentences describing *what* changes and *why*. The why is the important part. -->

## Changes

<!-- Bullet list of user-visible or behavior-visible changes. Skip implementation minutiae. -->

-

## Related issue

<!-- "Closes #123" auto-closes the issue on merge. "Relates to #123" links without closing. Leave blank if none. -->

## Checklist

- [ ] Tests added or updated for new or changed behavior (or explicitly explain why none are needed).
- [ ] `CHANGELOG.md` `[Unreleased]` section updated if the change is user-visible.
- [ ] If this changes a documented public contract, the relevant docs/README sections were updated in the same PR.
- [ ] `ruff check .` passes locally.
- [ ] `mypy src` passes locally.
- [ ] Public API changes are reflected in `README.md` and `docs/architecture.md` where appropriate.
