# aionetx - Roadmap

This document collects possible future directions. It is not a commitment;
items may be reprioritized, postponed, or removed.

---

## Near-term transport hardening

### TLS support for TCP (`ssl=` parameter)

`asyncio.open_connection()` and `asyncio.start_server()` already support
transport-level TLS. The main open work is exposing that cleanly through
`TcpClientSettings` / `TcpServerSettings` without compromising lifecycle,
reconnect, or transport-boundary clarity.

Open questions:

- expose `ssl.SSLContext` directly or wrap it in a dedicated settings object
- certificate reload strategy for long-lived servers
- typing shape for optional TLS settings

### UDP sender auto-rebind evaluation

`AsyncioUdpSender` is intentionally lifecycle-light today. There is still room
to evaluate an explicit opt-in recovery/rebind policy for deployments where a
sender socket may need to be recreated after transport-level errors.

### Ergonomics: `wait_until_stopped()` on `ManagedTransportProtocol`

The library already exposes `wait_until_connected()` and
`wait_until_running()` helpers for managed TCP roles. A symmetric
`wait_until_stopped()` helper may improve test ergonomics and shutdown
coordination without expanding the library beyond transport concerns.

---

## Performance and observability

### Evaluate `asyncio.DatagramProtocol` for UDP receive throughput

The current UDP receiver uses an explicit socket receive loop. It may be worth
evaluating `loop.create_datagram_endpoint()` / `asyncio.DatagramProtocol` for
higher datagram throughput and simpler event-loop integration on some
platforms.

### Publish richer overload metrics

The dispatcher already tracks drop counters and emits rate-limited warnings.
Additional runtime metrics or a dedicated observability event for backpressure
may make overload diagnosis easier without relying on log parsing.

### Large-scale connection profiling

The TCP server now exposes explicit admission and timeout controls, but
high-density workloads still need profiling and stress characterization across
platforms before stronger scalability claims would be justified.

---

## Explicit scope boundaries

These ideas are intentionally *not* current roadmap goals:

- moving framing or protocol codecs into the core library
- promoting `TypedEventRouter` into the curated root API without a broader
  API-boundary change
- adding protocol-specific helpers (HTTP, MQTT, Modbus, WebSocket,
  serialization)

Those concerns remain outside the transport-only boundary.

---

## Technical debt and hygiene

- revisit `asyncio_mode = "auto"` versus explicit `@pytest.mark.asyncio`
  usage in tests
- clean up overly narrow Hypothesis bounds in settings/property tests
- reduce environment-sensitive `multicast` marking where a test does not
  actually require multicast networking
- periodically reassess supported Python-version bounds as the ecosystem moves

---

## Rejected or deferred ideas

| Idea | Rationale |
| --- | --- |
| WebSocket transport | Too far from the raw-byte transport focus; a separate library makes more sense |
| Thread-safe API (`asyncio.run_coroutine_threadsafe`) | Conflicts with the deliberate single-loop-owner model |
| Automatic object serialization (JSON, Protobuf, etc.) | Serialization remains the application's responsibility |
