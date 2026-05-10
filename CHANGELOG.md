# Changelog

All notable user-visible changes to `aionetx` are documented in this file.

## [Unreleased]

Planned contents for the initial public alpha release (`0.1.0`).

### Added

- asyncio-first TCP client and server transport primitives
- UDP sender, UDP receiver, and IPv4 multicast receiver primitives
- explicit lifecycle events, event delivery settings, and backpressure policies
- bounded TCP send flushes with configurable per-connection send timeouts
- optional TCP client reconnect and heartbeat support
- `DispatcherRuntimeStats` as the public diagnostics snapshot type for managed
  TCP, UDP, and multicast receiver dispatcher counters
- `docs/lifecycle.md` as the public stop-during-state and caller-origin
  lifecycle reference
- tests, examples, typing metadata, and CI/release verification gates

### Changed

- require Python 3.11 or newer for the initial public alpha release
- Documentation now distinguishes possible future narrow TCP `ssl=` /
  `ssl.SSLContext` transport wiring from higher-layer authentication,
  certificate-management, DTLS, service-mesh, and application-security scope.

### Fixed

- README and reconnect heartbeat example wording now describe heartbeat bytes
  as advisory application-level probes, not built-in peer-health detection.
- Dispatcher documentation now describes the `emit_and_wait()` stop/drop
  `RuntimeError` path for completion-barrier callers.
- TCP connections now wait for opened-event handlers before starting reads,
  preserving per-connection event ordering across dispatch modes.
- TCP close and stop paths now defer handler-origin terminal events until the
  active same-connection handler has returned.
- TCP client/server stop paths and connection close paths now preserve deferred
  terminal publication when an external caller is cancelled while an inline
  bytes handler is still active.
- UDP receivers now preserve deferred stop publication when stop originates
  from a handler or when an external stop caller is cancelled.
- UDP and multicast receivers now abort startup when a STARTING lifecycle
  handler stops the receiver, preventing stale socket or running state.
- TCP clients now suppress stale RUNNING lifecycle publication when a
  STARTING lifecycle handler stops the client.
