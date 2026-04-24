# aionetx examples

Five runnable scripts that cover the shapes you are most likely to build on
top of `aionetx`. Each file is self-contained and exits `0` on success.
CI runs them as an advisory smoke lane via `scripts/ci/examples_smoke.py`.

## Recommended reading order

1. **[`tcp_echo_server_and_client.py`](tcp_echo_server_and_client.py)** -
   the smallest possible roundtrip. Introduces `AsyncioNetworkFactory`,
   `BaseNetworkEventHandler`, `TcpServerSettings` / `TcpClientSettings`,
   `wait_until_running` / `wait_until_connected`, and how to send bytes
   over a `ConnectionProtocol`.
2. **[`udp_round_trip.py`](udp_round_trip.py)** - the same shape for
   UDP. Shows the asymmetry between `UdpReceiverSettings` (managed
   lifecycle, emits events) and `UdpSenderSettings` (lifecycle-light,
   fire-and-forget).
3. **[`tcp_framing_length_prefix.py`](tcp_framing_length_prefix.py)** -
   realistic TCP application shape: a fixed-size `CommonMessageHeader`
   that carries payload length, five typed message variants, a central
   `MessageDispatcher` that routes complete frames to per-type handlers,
   and a `FramingHandler` that turns the raw byte stream into complete
   frames. This is the pattern you would lift into production code.
4. **[`tcp_reconnect_with_heartbeat.py`](tcp_reconnect_with_heartbeat.py)** -
   reconnect supervision. A client starts before the server is listening,
   observes one or more `ReconnectAttemptFailedEvent`s, then the server
   comes up and the client reconnects. A minimal heartbeat provider is
   wired up to show the keep-alive hook.
5. **[`backpressure_policies.py`](backpressure_policies.py)** -
   side-by-side comparison of `BLOCK`, `DROP_OLDEST`, and `DROP_NEWEST`
   against an intentionally slow handler. Prints which datagrams each
   policy actually delivered to the handler.

## Running

Any example can be run directly:

```text
python examples/tcp_echo_server_and_client.py
```

To run them all back-to-back with the same guardrails CI uses:

```text
python scripts/ci/examples_smoke.py
```

## Scope and caveats

- The examples pick an ephemeral loopback port with a short-lived
  `socket.bind(("127.0.0.1", 0))` trick. There is a small TOCTOU window
  between closing that socket and the server binding. It is acceptable
  for examples; production code should pass a fixed, pre-allocated port.
- Payloads in `tcp_framing_length_prefix.py` are JSON for readability.
  Production systems typically swap JSON for a binary schema (protobuf,
  MessagePack, CBOR, custom struct). The framing + dispatcher shape does
  not change.
- Every example terminates on a deterministic condition (counted events
  or `asyncio.Event`) instead of using a blind "sleep and hope" shutdown,
  so CI smoke-runs remain fast and stable.
