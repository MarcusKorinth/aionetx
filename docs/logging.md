# Logging

aionetx uses the standard library's `logging` module. It does **not**
configure any handlers itself; the integrating application is expected to
configure output. All aionetx loggers live under the package namespace
`aionetx.*` so a single configuration entry can target the library.

---

## Logger hierarchy

All module loggers derive from `__name__`, so names match the import path.

| Logger (prefix `aionetx.implementations.asyncio_impl.`) | Component               |
|---------------------------------------------------------|-------------------------|
| `asyncio_tcp_client`                                    | TCP client              |
| `asyncio_tcp_server`                                    | TCP server              |
| `asyncio_tcp_connection`                                | TCP connection wrapper  |
| `asyncio_heartbeat_sender`                              | Heartbeat emission      |
| `asyncio_udp_receiver`                                  | UDP receiver            |
| `asyncio_udp_sender`                                    | UDP sender              |
| `asyncio_multicast_receiver`                            | Multicast receiver      |
| `event_dispatcher`                                      | Event delivery          |
| `_tcp_server_helpers`                                   | Internal TCP server     |
| `_tcp_client_connect`                                   | Internal TCP connect    |
| `_tcp_connection_helpers`                               | Internal TCP connection |
| `_asyncio_datagram_receiver_base`                       | Shared datagram base    |

Setting the level on `aionetx` propagates to all of them.

---

## Structured context

Many runtime components attach a `logging.LoggerAdapter` with context merged
into records via `extra`. The exact keys vary by component. Common keys
include:

```python
{"component": "<component-name>", "connection_id": "<canonical-id>", "host": "...", "port": 1234}
```

Canonical resource-id / connection-id formats (from
`aionetx.implementations.asyncio_impl.identifier_utils`):

| Component           | canonical id format                            |
|---------------------|------------------------------------------------|
| TCP client          | `tcp/client/<host>/<port>`                     |
| TCP server          | `tcp/server/<host>/<port>`                     |
| TCP server conn.    | `tcp/server/<peer_host>/<peer_port>/connection/<seq>` |
| TCP client conn.    | `tcp/client/<host>/<port>/connection`          |
| UDP receiver        | `udp/receiver/<host>/<port>`                   |
| UDP sender          | `udp/sender/<local_host>/<local_port>`         |
| Multicast receiver  | `udp/multicast/<group_ip>/<port>`              |

To surface these keys in the output, a formatter must reference them
explicitly (e.g. `%(connection_id)s`) or a handler must read them from the
`LogRecord` via the standard `extra` mechanism.

---

## Recommended levels

| Environment | `aionetx` level | Notes                                    |
|-------------|-----------------|------------------------------------------|
| Production  | `WARNING`       | Surfaces fallback warnings, handler-failure warnings, teardown warnings, and dropped-event warnings without per-packet noise. |
| Staging     | `WARNING` or `DEBUG` | Use `WARNING` for production-like signal; temporarily use `DEBUG` when validating lifecycle/connect sequencing. |
| Debug       | `DEBUG`         | Adds per-reconnect step, per-event-dispatch detail. Noisy — do not ship. |

---

## Warnings you should alert on

These are the `WARNING`-level events aionetx emits that typically indicate
user-visible degradation or misconfiguration:

- **UDP `sendto()` / `recvfrom()` fallback polling.** Emitted once per
  sender/receiver when the running event loop does not expose
  `sock_sendto()` / `sock_recvfrom()` (typical on Windows
  `ProactorEventLoop`). Implies per-operation latency up to ~20 ms. See
  [`platform_notes.md`](platform_notes.md).
- **TCP client supervision/connect failure.** Emitted when a connect or
  reconnect cycle fails before policy handling decides whether to retry or
  stop.
- **Handler-failure or backpressure warnings.** Emitted when handler-failure
  policy cannot request a component stop, when shutdown drops background
  events, or when a configured backpressure policy drops events.
- **Best-effort teardown warnings.** Emitted when close/shutdown cleanup,
  multicast membership cleanup, read-task shutdown, writer shutdown, or server
  connection teardown reports a recoverable failure.
- **TCP server admission/broadcast warnings.** Emitted when max connection
  limits reject an accepted connection or when a broadcast send to one
  connection fails.

An operational alert on `aionetx.*` `WARNING` records is a reasonable
default.

---

## Minimal configuration

```python
import logging.config

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "aionetx": {
            "format": (
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            ),
        },
    },
    "handlers": {
        "stderr": {
            "class": "logging.StreamHandler",
            "formatter": "aionetx",
        },
    },
    "loggers": {
        "aionetx": {
            "level": "WARNING",
            "handlers": ["stderr"],
            "propagate": False,
        },
    },
})
```

Use a standard formatter as the default copy-paste configuration.

Structured extras such as `component`, `connection_id`, `host`, `port`, and
`role` are attached on many aionetx records via `LoggerAdapter`, but not every
record carries the same keys. If you want to render those extras, use a custom
formatter or filter that supplies defaults for missing fields instead of
referencing them unconditionally in the format string.
