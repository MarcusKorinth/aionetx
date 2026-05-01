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
- tests, examples, typing metadata, and CI/release verification gates

### Changed

- require Python 3.11 or newer for the initial public alpha release

### Fixed

- TCP connections now wait for opened-event handlers before starting reads,
  preserving per-connection event ordering across dispatch modes.
- TCP close and stop paths now defer handler-origin terminal events until the
  active same-connection handler has returned.
- UDP receivers now preserve deferred stop publication when stop originates
  from a handler or when an external stop caller is cancelled.
