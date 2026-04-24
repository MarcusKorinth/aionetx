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
