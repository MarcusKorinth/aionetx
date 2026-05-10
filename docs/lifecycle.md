# Lifecycle Reference

This page is the focused shutdown reference for managed `aionetx`
components. It expands the lifecycle contract in
[`docs/architecture.md`](./architecture.md) without replacing it.

The managed component model applies to:

- TCP clients
- TCP servers
- UDP receivers
- IPv4 multicast receivers

`UdpSenderProtocol` is intentionally lifecycle-light. It supports idempotent
`stop()` for cleanup, but it does not publish managed lifecycle or connection
metadata events.

## State Model

Managed components expose exactly four public states:

- `STOPPED`
- `STARTING`
- `RUNNING`
- `STOPPING`

Legal transitions are:

| Transition | Meaning |
| --- | --- |
| `STOPPED -> STARTING` | Startup has begun. |
| `STARTING -> RUNNING` | Startup committed and the component owns runtime resources. |
| `STARTING -> STOPPED` | Startup rolled back after failure or cancellation. |
| `STARTING -> STOPPING` | Stop won while startup was in progress. |
| `RUNNING -> STOPPING` | Shutdown has begun. |
| `STOPPING -> STOPPED` | Shutdown completed and owned runtime resources are released. |

There is no public `FAILED` state. Runtime failures are surfaced through
events, exceptions from direct API calls, or failed waiters.

## Caller Origins

Shutdown behavior depends on where the stop or close request came from.

| Origin | Definition | Why it matters |
| --- | --- | --- |
| External caller | Application code outside an active event handler for the same component or connection. | The caller can wait for the normal shutdown path. |
| Handler-origin caller | The task currently running an event handler calls `stop()` or `close()` for the same component or connection stream. | Terminal events must not re-enter that handler. |
| Spawned child from a handler | A task created while a handler is still active calls `stop()` or `close()`. | The child keeps handler-origin shutdown context while the parent handler is active. |
| Internal shutdown path | Library-owned runtime logic starts shutdown, for example after handler-failure policy, peer close on a connection, or startup rollback. | Cleanup must converge with public callers and publish the same terminal contract. |

## Stop-During-State Matrix

This matrix describes the expected behavior for managed component `stop()`.
Direct TCP connection `close()` follows the same waiter and terminal-event
principles for `ConnectionClosedEvent`.

| Current state | External caller | Handler-origin caller | Spawned child from handler | Internal shutdown path | Terminal publication | Final state |
| --- | --- | --- | --- | --- | --- | --- |
| `STOPPED` | Returns without starting new work. | Same. | Same once the component is already stopped. | No shutdown work remains. | No new terminal event is published for the no-op call. | `STOPPED` |
| `STARTING` | Requests shutdown for the in-progress start. If a concurrent startup task owns resources that `stop()` cannot yet see, that startup task closes them before it can publish `RUNNING`. | Stop requested from a `STARTING` handler suppresses stale `RUNNING` publication. TCP clients and datagram receivers avoid continuing to connection or socket setup; TCP servers may create a listener after the handler returns, but close it before publishing `RUNNING` when stop already won. | Treated as handler-origin while the parent handler is active. | Startup failure or cancellation rolls back partial resources. | If stop wins, publish `STOPPING` then `STOPPED` at most once. Startup rollback always returns state to `STOPPED`; observable rollback publication happens only when rollback reaches its normal event-emission path. | `STOPPED` |
| `RUNNING` | First caller owns the shutdown path. Overlapping callers wait on the same stop waiter. | The component starts shutdown, but terminal callbacks are deferred until the active handler unwinds. | The child can own the stop path, but same-stream terminal callbacks remain deferred until the parent handler unwinds. | Handler-failure policy or supervisor logic starts the same stop path. | Publish `STOPPING` then `STOPPED` at most once. Publish connection close events before stop is considered complete, except for handler-origin deferral. | `STOPPED` |
| `STOPPING` | Joins the existing shutdown waiter. Caller cancellation must not cancel the shared shutdown. | Does not create a second shutdown path or self-wait. | Joins or preserves the existing handler-origin stop path. | Continues the existing cleanup and publication sequence. | No duplicate `STOPPING`, `STOPPED`, or successful close event should be emitted. | `STOPPED` |

## Dispatch-Mode Differences

`INLINE` and `BACKGROUND` modes share the same public lifecycle states and
terminal ordering rules, but the handler execution path differs.

| Dispatch mode | Handler execution | Shutdown consequence |
| --- | --- | --- |
| `INLINE` | The handler runs in the caller/emitter path. | A stop or close request made while an inline handler is active must not publish terminal lifecycle or close events back into that handler. External stop callers wait for the active inline handler before terminal publication completes. |
| `BACKGROUND` | Events are queued to the dispatcher worker after it starts. Before worker startup, emission can fall back to inline delivery. | Handler-origin stop cannot self-wait on the worker that is running the handler. Terminal publication is deferred until the active handler-origin context expires. |

After dispatcher stop begins in `BACKGROUND` mode, newly emitted best-effort
events may be dropped so shutdown can finish deterministically. Completion
barrier emission is stricter: if a barrier event is dropped during stop, its
waiter fails instead of being treated as handled.

## Waiter and Cancellation Rules

Repeated and overlapping stop or close callers converge on one owner path.

- A caller that finds an existing stop/close owner waits for the shared waiter.
- The owner never waits on its own waiter.
- Cancelling one waiting caller must not cancel the shared cleanup or terminal
  publication path.
- If a cancellation happens while an active inline handler is blocking terminal
  publication, the cancelled caller receives `CancelledError` only after the
  shared cleanup reaches a safe completion point.
- Other overlapping callers can still complete successfully when cleanup
  finishes.

These rules keep cancellation local to the caller that was cancelled. They do
not turn caller cancellation into component-wide shutdown cancellation.

## Terminal Event Rules

Terminal events are the observable end of shutdown:

- managed components publish `ComponentLifecycleChangedEvent` for `STOPPING`
  and `STOPPED` at most once per shutdown sequence
- TCP connections publish at most one successfully completed
  `ConnectionClosedEvent` for a close sequence; if publication fails or is
  cancelled before success, later close reconciliation can retry publication
- UDP and multicast receivers publish their receiver close event when the
  managed receive endpoint is torn down
- server stop preserves close publication for all accepted connections, not
  only the connection that triggered shutdown
- terminal events for a connection stream must not overlap an active handler
  for that same stream

The general invariant is: once `stop()` returns, component-owned background
tasks should not invoke user callbacks.

There is one handler-origin exception. If `stop()` or `close()` is awaited from
the currently running handler for the same connection or component event
stream, the terminal events needed to close that stream can be published after
that handler unwinds. This avoids self-deadlock while preserving same-stream
non-overlap.

## Startup Rollback and Shutdown Completion

Startup rollback and normal shutdown both end in `STOPPED`, but they reach it
through different paths:

- startup failure or startup cancellation may roll back `STARTING` directly to
  `STOPPED`
- a stop request that wins during startup moves through `STOPPING` before
  `STOPPED`
- a `STARTING` handler-origin stop prevents stale `RUNNING` lifecycle
  publication
- rollback after failed or cancelled `STARTING` publication applies `STOPPED`
  state without relying on a second handler callback
- pending connection waiters are unblocked when startup is stopped or rolled
  back before a connection becomes available
- after shutdown completion, listeners, sockets, receive tasks, heartbeat
  tasks, supervisor tasks, connection registries, and dispatcher ownership are
  released for the component

These outcomes are the test oracle for lifecycle and shutdown work. Changes to
TCP, UDP, multicast, dispatcher, reconnect, heartbeat, or connection teardown
should preserve this matrix unless the public contract is intentionally changed
and documented in the same pull request.
