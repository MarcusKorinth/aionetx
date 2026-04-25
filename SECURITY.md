# Security Policy

`aionetx` is a raw-byte asyncio transport library. The sections below
describe which versions receive security fixes, how to report a
suspected vulnerability privately, and which classes of report fall
outside the project's security scope by design.

## Supported Versions

Until `aionetx` reaches `1.0.0`, only the **latest published `0.y.x`
release** receives security fixes. Earlier pre-release versions are
not supported; upgrade to the current release.

After `1.0.0`, security fixes will be backported to the latest minor
line and the previous minor line (`N` and `N-1`). Older lines will be
announced as end-of-life in `CHANGELOG.md` when they stop receiving
fixes.

## Reporting a Vulnerability

**Please do not file public GitHub issues for suspected
vulnerabilities.** Open the
[Security tab of the repository](https://github.com/MarcusKorinth/aionetx/security)
and click **"Report a vulnerability"** so a fix can be prepared before
the details become public. This creates a private advisory draft that
is visible only to the maintainers and the reporter until it is
published.

When reporting, please include:

- A description of the issue and the impact you believe it has.
- The affected `aionetx` version(s) and the Python version.
- A minimal reproduction (code snippet, pcap, or step-by-step) if
  possible.
- Any suggested mitigations you are aware of.

## Response Expectations

This project is maintained by a single developer on a best-effort
basis. Realistic commitments:

- **Acknowledgement:** within 7 calendar days of the report.
- **Triage and initial assessment:** within 14 calendar days.
- **Fix timeline:** depends on severity and complexity; high-severity
  issues are prioritised over other work.
- **Coordinated disclosure window:** typically up to 90 days from the
  report, extendable by mutual agreement if a fix requires deeper
  rework. Credit is given to the reporter in the advisory unless the
  reporter asks to stay anonymous.

## Secrets and Credentials

Project secrets must not be committed to the repository. Required
credentials must be stored only in GitHub Secrets, GitHub Environments,
or the corresponding service provider's protected secret store. Release
publishing should prefer short-lived credentials and OpenID Connect
(OIDC) trusted publishing over long-lived upload tokens.

Secrets and credentials must be scoped to the minimum required
permission. Access must be removed when it is no longer needed. Any
secret that is exposed, suspected to be exposed, or accessible to a
person who no longer needs it must be revoked and rotated before the
next release that depends on it.

## Maintainer and Collaborator Access

Access to sensitive project resources is granted on a least-privilege
basis. Before a collaborator receives elevated access to the repository,
release workflow, package publishing settings, security advisories, or
stored secrets, the maintainer must review:

- the concrete access need and intended duration
- whether a lower-permission role is sufficient
- whether the collaborator uses multi-factor authentication
- recent project activity and trust context

Elevated access must be removed when the need ends. Public code changes
continue to go through pull requests and required checks even when made
by collaborators with repository access.

## Dependency and Static Analysis Policy

Software composition analysis (SCA) findings are handled before release:

- High and critical dependency vulnerability findings block releases
  unless they are documented as non-exploitable for this project.
- License findings must be triaged before release.
- Any non-exploitability or policy suppression must include a written
  rationale that can be reviewed later.

Static application security testing (SAST) findings are handled before
merge or release:

- High and critical CodeQL or equivalent security findings block release.
- Medium findings must be triaged before release.
- False-positive or non-exploitable suppressions require a written
  rationale. Dismissal without a reason is not acceptable.

If a dependency vulnerability is known but does not affect `aionetx`,
the project publishes a VEX-style justification or equivalent advisory
note explaining why the vulnerable component or code path is not
exploitable in this project.

## Threat Model and Attack Surface

Primary actors:

- application developers embedding `aionetx`
- remote TCP, UDP, or multicast peers
- local processes that can interact with sockets or event handlers
- maintainers and collaborators with repository or release access
- CI/CD and package release workflows

Assets that need protection:

- sockets, file descriptors, and asyncio tasks owned by transports
- lifecycle state and event stream integrity
- package artifacts, release provenance, and security advisories
- repository settings, CI credentials, and package publishing authority

Main attack surfaces:

- raw network input delivered to user handlers
- handler exceptions, cancellation, and backpressure behavior
- lifecycle transitions, reconnect loops, heartbeat loops, and cleanup
- CI workflow inputs, dependency updates, and release metadata

Current mitigations include explicit lifecycle state tests, bounded
event queues with configurable backpressure, no payload logging by
default, CodeQL, Dependabot, release provenance checks, OIDC trusted
publishing, artifact attestations, and branch protection rules.

## Out of Scope

The following are **not** treated as security vulnerabilities because
they reflect deliberate architectural choices of a raw-byte transport
library, not defects:

- **No authentication, authorization, or transport-layer encryption.**
  `aionetx` exposes raw TCP/UDP/multicast byte streams. Peer identity,
  integrity, and confidentiality are the responsibility of the
  application layer built on top (for example, TLS via
  `asyncio.start_tls`, DTLS, or application-level signing). Reports of
  the form "an attacker can send bytes to an open port" will be
  closed as out of scope.
- **Denial of service from malformed or flooding peer traffic on
  untrusted transports.** Backpressure, rate limiting, and peer-trust
  decisions are delegated to the user's event handler and higher-layer
  protocol logic. `aionetx`'s backpressure primitives (`BLOCK`,
  `DROP_OLDEST`, `DROP_NEWEST`) are explicitly user-selectable
  policies, not a promise of DoS immunity.
- **Port reuse, interface binding, or privileged-port behavior** on
  platforms where those are OS-level policies. `aionetx` exposes the
  OS socket semantics faithfully; misconfiguration at deployment time
  is a deployment concern.
- **Behavior of third-party dependencies.** `aionetx` has zero runtime
  dependencies outside the Python standard library; if you observe a
  vulnerability in `cpython`'s asyncio or socket stack, please report
  it to the Python Security Response Team instead.

In-scope examples (report these privately):

- Memory-safety or reference-cycle bugs that cause runaway resource
  consumption under normal asyncio usage.
- Logic errors in lifecycle or cleanup that leak sockets, file
  descriptors, or asyncio tasks in ways the user cannot recover from.
- Any path where `aionetx` itself (not the user's handler) crashes the
  event loop or the Python process under well-formed peer input.

## Coordinated Disclosure

Once a fix is available and released, the maintainers will publish a
GitHub Security Advisory referencing the affected versions, the CVE
(if one is assigned), the fixed version, and credits. Reporters are
welcome to publish their own write-up once the advisory is public.
