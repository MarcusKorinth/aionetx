# Platform Notes

This document records platform-specific behaviour that differs from the
cross-platform defaults assumed elsewhere in aionetx.

---

## TCP/UDP bind reuse and exclusivity

### Linux / macOS

Ordinary TCP/UDP listeners use the platform default bind behavior. Multicast
receivers opt into address reuse because joining the same multicast group/port
from multiple sockets is a normal multicast use case.

### Windows

On Windows, `SO_REUSEADDR` has stronger semantics: it allows **any** process
(including a malicious one) to bind to the same port, even if another process
is already listening. This is a known security concern documented in the
[Windows socket documentation](https://learn.microsoft.com/en-us/windows/win32/winsock/using-so-reuseaddr-and-so-exclusiveaddruse).

The safe Windows alternative is `SO_EXCLUSIVEADDRUSE`, which prevents port
hijacking while still allowing `TIME_WAIT` reuse.

**Current state:** aionetx uses safer platform-conditional bind setup through
`configure_listener_bind_socket(...)`:

- ordinary Windows listeners prefer `SO_EXCLUSIVEADDRUSE` when the option is
  available
- multicast receivers pass `allow_address_reuse=True` and use `SO_REUSEADDR`
  because multicast membership needs reuse semantics
- multicast receivers additionally attempt `SO_REUSEPORT` where the platform
  exposes it, suppressing unsupported-kernel failures

**Affected components:**

| File | Class |
|------|-------|
| `src/aionetx/implementations/asyncio_impl/asyncio_multicast_receiver.py` | `AsyncioMulticastReceiver` |
| `src/aionetx/implementations/asyncio_impl/asyncio_udp_receiver.py` | `AsyncioUdpReceiver` |
| `src/aionetx/implementations/asyncio_impl/runtime_utils.py` | `configure_listener_bind_socket` |

---

## Async UDP I/O on Windows (ProactorEventLoop)

aionetx's UDP receive and send paths prefer the event loop's
`sock_recvfrom()` / `sock_sendto()` awaitables, which deliver non-blocking
socket I/O integrated with the selector. These APIs are available on
`asyncio.SelectorEventLoop` (the default on Linux and macOS). On Windows,
Python 3.8+ defaults to `asyncio.ProactorEventLoop`, which does **not**
expose `sock_recvfrom()` / `sock_sendto()`.

### Fallback behaviour

When the running loop does not expose these awaitables, aionetx falls back
to calling `socket.recvfrom()` / `socket.sendto()` directly on the
non-blocking socket and awaits `asyncio.sleep()` on each `BlockingIOError`
with exponential backoff.

Backoff envelope (from
`_WOULD_BLOCK_INITIAL_DELAY_SECONDS` / `_WOULD_BLOCK_MAX_DELAY_SECONDS`
in `_asyncio_datagram_receiver_base.py` and `asyncio_udp_sender.py`):

| Parameter        | Value       |
|------------------|-------------|
| Initial delay    | **0.5 ms**  |
| Maximum delay    | **20 ms**   |
| Growth           | Exponential (×2 per retry, capped at max) |

On a busy socket the first retry is cheap; on an idle socket the cap means
worst-case wake-up latency per operation approaches 20 ms — substantially
higher than the sub-millisecond behaviour of a selector-integrated loop.

### How to detect fallback is active

Both components emit a single `WARNING` log line on first use of the
fallback per instance (rate-limited to once per 5 minutes per
`connection_id` at the module level):

- Receiver:
  `"<name> is using recvfrom() fallback polling because the event loop does not expose sock_recvfrom()."`
- Sender:
  `"AsyncioUdpSender is using sendto() fallback polling because the event loop does not expose sock_sendto()."`

Set the `aionetx.implementations.asyncio_impl` logger (or the project root
logger) to at least `WARNING` to see these messages. See
[`logging.md`](logging.md).

### Mitigation

If your application can tolerate the Selector loop's trade-offs (no overlapped
file I/O, reduced subprocess support on Windows), set the Selector policy
**before** creating your event loop:

```python
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
```

With the Selector policy, `sock_recvfrom()` / `sock_sendto()` are available
and aionetx will not use the polling fallback.

### Implications for timing-sensitive use cases

For low-latency test rigs in timing-sensitive or regulated environments (for
example hardware-in-the-loop setups, CAN/LIN bridging, or protocol
simulators), profile on the exact platform and event-loop policy you will
deploy. See [`timing_and_latency.md`](timing_and_latency.md) for the overall
timing envelope aionetx targets and explicitly does not guarantee.
