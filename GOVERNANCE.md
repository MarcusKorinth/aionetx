# Governance

`aionetx` is currently governed as a single-maintainer open source
project. The governance model is intentionally lightweight: decisions
are made in public where practical, changes go through pull requests,
and release/security authority remains with the maintainer until
additional maintainers are explicitly added.

## Decision model

Project decisions are made by the maintainer after considering public
issues, pull request discussion, CI results, security impact, and the
project scope documented in `README.md`, `docs/architecture.md`, and
`SECURITY.md`.

The maintainer is responsible for:

- accepting or rejecting pull requests
- deciding whether a change fits the transport-only scope
- deciding release timing and release contents
- maintaining branch protection and required checks
- triaging security reports and coordinating vulnerability disclosure
- granting or removing elevated repository, release, or security access

For routine changes, a passing CI result and a focused pull request are
expected before merge. For behavior, lifecycle, API, or security changes,
the maintainer may require additional tests, documentation, or design
discussion before accepting the change.

## Current roles

| Role | Current holder | Responsibilities |
| --- | --- | --- |
| Maintainer | Marcus Korinth | Project direction, releases, repository settings, security triage, merge decisions |
| Contributor | Anyone submitting issues or pull requests | Propose changes, follow `CONTRIBUTING.md`, sign commits with DCO |
| Security reporter | Anyone reporting a vulnerability privately | Report suspected vulnerabilities through GitHub private vulnerability reporting |
| CI/release automation | GitHub Actions | Run required checks, build artifacts, generate attestations, publish releases when a release tag is pushed |

## Adding maintainers or elevated access

Additional maintainers may be added after sustained, high-quality
contributions and an explicit trust review. Before elevated access is
granted, the maintainer reviews the access need, intended duration,
least-privilege role, multi-factor authentication status, and recent
project activity.

Elevated access must be removed when it is no longer needed. Public code
changes continue to go through pull requests, required checks, and DCO
sign-off even when made by collaborators with repository access.

## Scope and conflict resolution

The project scope is raw asyncio transport primitives for TCP, UDP, and
multicast. Protocol parsing, application-level authentication,
authorization, encryption, business logic, framing, and serialization
remain outside the core library.

If there is disagreement about a change, the maintainer uses the
documented project scope, lifecycle semantics, security policy, and
test evidence as the decision basis. If a decision would materially
change public API, lifecycle behavior, or security expectations, it
should be documented in the pull request and reflected in user-facing
documentation when accepted.

## Current continuity limits

`aionetx` is currently a single-maintainer project. The project does not
yet claim a bus factor of two or a guaranteed one-week continuity plan
if the maintainer becomes unavailable. The intended path to improve this
is to add at least one trusted maintainer once the project has sustained
external contribution and review history.
