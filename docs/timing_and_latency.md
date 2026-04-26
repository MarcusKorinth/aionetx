# Timing & Latency

aionetx is a best-effort asyncio transport library. It targets low overhead
and predictable lifecycle behavior, but it is **not** a real-time system and
does not provide hard timing guarantees. This document describes the actual
timing envelope users should expect and, equally important, what is **not**
guaranteed.

---

## Scope of guarantees

aionetx guarantees:

- **Lifecycle ordering.** State transitions
  (`STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED`) and event ordering
  invariants hold deterministically. See [`architecture.md`](architecture.md).
- **Event-loop ownership.** Public methods are checked against the owning
  event loop; cross-loop misuse fails fast rather than silently corrupting
  state.
- **No silent drops of exceptions.** Errors surface as events or explicit
  exceptions; there are no fire-and-forget exception swallows on the hot path.

aionetx does **not** guarantee:

- upper bounds on per-operation latency
- deterministic scheduling (jitter-free cadence)
- safety-rated behavior under the definitions of DO-178C, IEC 61508, or
  ISO 26262

If your use case requires any of the above, aionetx should be treated as
instrumentation or a test-bench component, not as a safety-relevant element
of the system under test.

---

## Observed envelopes

### TCP send flushes

TCP `ConnectionProtocol.send()` completes after the underlying `asyncio.StreamWriter.drain()` completes. TCP client and server connections default to `connection_send_timeout_seconds=30.0`, so a slow or non-reading peer cannot stall a send forever unless that timeout is explicitly disabled.

If the send timeout expires, direct `send()` raises `asyncio.TimeoutError`. Managed heartbeat sends emit a `NetworkErrorEvent` and stop the heartbeat sender; server broadcast emits a `NetworkErrorEvent` and closes the failed recipient connection.

Setting `connection_send_timeout_seconds=None` disables this connection-level enforcement and allows `drain()` to wait indefinitely under OS/socket backpressure. For TCP servers, `broadcast_send_timeout_seconds` is a separate outer per-recipient broadcast wrapper; disabling it does not disable the accepted connection's own send timeout.

The configured timeout is a best-effort asyncio deadline, not a hard real-time guarantee. Actual wake-up timing still depends on event-loop scheduling and system load.

### TCP reconnect backoff

Source: `ReconnectBackoff` in
`src/aionetx/implementations/asyncio_impl/runtime_utils.py`.

Per attempt the delay is computed as `current_delay`, then advanced to
`min(current_delay * backoff_factor, max_delay_seconds)`. Jitter is applied
from `TcpReconnectSettings.jitter`:

| Jitter mode | Returned delay |
| --- | --- |
| `NONE` | `current_delay` |
| `FULL` | uniform `[0, current_delay]` |
| `EQUAL` | `current_delay / 2 + uniform[0, current_delay / 2]` |

The randomness uses `random.random()` (not cryptographic). Reconnect timing
therefore has a known *distribution* given the configured settings, but the
absolute wake-up time additionally depends on event-loop scheduling.

### Heartbeat cadence

Heartbeat cadence is driven by `asyncio.sleep()` between emissions. Under
normal event-loop conditions jitter is bounded by the loop's scheduler
resolution (typically sub-millisecond on Linux/macOS selector loops). Under
load the heartbeat task competes with all other tasks on the same loop, so
cadence drift grows with overall loop utilization.

Heartbeats are advisory for liveness detection. They are not a time source
and must not be used as one.

### UDP receive/send

- **Normal path (Selector loop):** A single receive or send completes in a
  handful of microseconds of event-loop overhead on top of the kernel's
  socket call. This is the overhead aionetx adds; absolute latency is
  dominated by the OS network stack and the wire.
- **Fallback path (Windows ProactorEventLoop):** When `sock_recvfrom()` /
  `sock_sendto()` are not exposed by the loop, aionetx polls the
  non-blocking socket and awaits `asyncio.sleep()` on each `BlockingIOError`
  with exponential backoff (0.5 ms initial, 20 ms cap). Per-operation
  worst-case wake latency approaches the 20 ms cap on a quiet socket. See
  [`platform_notes.md`](platform_notes.md).

If latency matters for your use case, measure on the target platform with
the event-loop policy you will deploy in production. Do not assume numbers
from Linux selector-loop runs transfer to Windows Proactor-loop runs.

---

## Suitability statement

aionetx is suitable for:

- test harnesses, simulators, HIL/SIL tooling, bench automation, and
  integration tooling in regulated or certification-adjacent environments
  where aionetx carries data, logs, and control messages but is not itself
  the safety-rated or certified component
- ground-support and lab-support tooling, protocol replay, and CI
  integration tests
- timing-sensitive test rigs where best-effort asyncio behavior is acceptable
  and measured on the target platform

aionetx does not provide certification evidence, qualification artifacts, a
safety case, or bounded worst-case execution time guarantees.

aionetx is **not** suitable, without additional integration analysis, for:

- airborne software requiring DO-178C certification evidence
- automotive ECUs certified against ISO 26262 as ASIL-rated components
- industrial functional-safety systems under IEC 61508 as safety-rated
  components
- any context requiring bounded worst-case execution time (WCET)

Users integrating aionetx into larger certified or safety-related systems are
responsible for their own system-level qualification and compliance evidence.
