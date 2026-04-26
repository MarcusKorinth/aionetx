# Architecture

This document is the canonical architectural contract for `aionetx`.

It replaces scattered ADR history with one current-state record of decisions, boundaries, and limitations.

## 1) Purpose and boundary

`aionetx` is an asyncio-first transport library for reusable TCP/UDP primitives with explicit lifecycle semantics and event-driven integration.

Core principle: **hide plumbing, not semantics**.

### In scope

- TCP client/server transports
- UDP receiver/sender transports
- UDP multicast receiver transport
- managed lifecycle publication
- event delivery and bounded backpressure policies
- explicit reconnect behavior
- explicit heartbeat scheduling behavior
- transport-level failure surfacing and canonical identifiers

### Out of scope

- protocol framing/parsing
- serialization/deserialization
- message schemas
- business/domain logic
- application-level retry orchestration
- hidden recovery/implicit lifecycle transitions

## 2) Lifecycle contract (authoritative)

Managed components expose exactly four public lifecycle states:

- `STOPPED`
- `STARTING`
- `RUNNING`
- `STOPPING`

No public `FAILED` lifecycle state exists.

### Legal transitions

- `STOPPED -> STARTING`
- `STARTING -> RUNNING`
- `STARTING -> STOPPED` (startup rollback)
- `STARTING -> STOPPING` (shutdown requested during startup)
- `RUNNING -> STOPPING`
- `STOPPING -> STOPPED`

State transitions must remain monotonic and understandable. Concurrent lifecycle calls must converge without illegal transitions, duplicate terminal publication, or detached cleanup tasks.

Failures are surfaced through events (network/reconnect/close/error), not via a separate lifecycle enum.

### Lifecycle invariants

The following invariants are part of the public contract and are relied on by
the semantic test suite:

- Once `stop()` returns with state `STOPPED`, no further user callback is
  invoked from the component's internal tasks.
- Startup cancellation must roll back partial resources and leave the component
  in the same terminal `STOPPED` state as other startup failures.
- `STOPPING` is a terminal-in-progress state, not a re-entrant steady state:
  repeated or overlapping stop requests converge on the same shutdown path
  instead of creating new transition branches.
- Lifecycle publication remains monotonic: managed components do not publish
  illegal backward transitions.
- Terminal lifecycle publication happens at most once per shutdown sequence.

## 3) Event model contract

Primary integration contract:

```python
async def on_event(self, event: NetworkEvent) -> None:
    ...
```

`BaseNetworkEventHandler` is a convenience layer over this contract.

### Dispatch guarantees

- Sequential dispatch per connection
- No concurrent event execution inside one connection stream
- Deterministic per-connection ordering
- Cross-connection concurrency is topology/dispatch-mode dependent, not universally guaranteed

### Callback semantics

- `ConnectionOpenedEvent` is published before the first
  `BytesReceivedEvent` for the same connection.
- Connection close callbacks run before the corresponding
  `ConnectionClosedEvent` is emitted.
- Re-entrant shutdown requests from callbacks/handlers must converge safely on
  the same close or stop operation.
- Handler failures remain observable: depending on the configured failure
  policy they either surface as `NetworkErrorEvent`, as
  `HandlerFailurePolicyStopEvent`, or as an inline exception in the caller.

### Event Type Evolution

`NetworkEvent` is an **open** `TypeAlias` union: new event dataclasses may
be added in minor releases as the observability surface grows. Stability
guarantees are:

- Existing event dataclasses and their field shapes are stable - fields
  are not renamed, retyped, or removed without a deprecation cycle and a
  CHANGELOG entry when compatibility expectations change.
- Additions to the union are called out under **Added** in the CHANGELOG.
- Removing an event type is breaking and reserved for major releases unless a
  carefully justified compatibility reset is explicitly documented in the
  changelog and upgrade notes.

Handler code must stay forward-compatible: always keep an `else:` branch
on `isinstance` chains and a `case _:` arm on `match` statements.
`typing.assert_never` is intentionally not safe against this union
because it assumes a closed taxonomy. `BaseNetworkEventHandler` and
`TypedEventRouter` handle the open union correctly out-of-the-box:
`BaseNetworkEventHandler` falls back to its default no-op `on_event` for
unknown types; `TypedEventRouter` returns `False` from `dispatch()` and
does nothing.

## 4) Event delivery and backpressure

Event delivery is explicit, bounded, and configurable.

- bounded queueing only (no hidden unbounded buffering)
- explicit backpressure policy selection
- overload behavior must remain observable

Policies are transport settings concerns, not hidden runtime heuristics.

## 5) Reconnect strategy

Reconnect is explicit and configurable.

- never implicit
- must remain observable through events
- must not mask terminal failures
- policy stays at transport layer; no smart strategy engine

## 6) Heartbeat boundary

Heartbeat is an explicit provider-driven periodic send mechanism for managed TCP roles.

Heartbeat is **not**:

- protocol interpretation
- health/liveness proof
- timeout detector
- implicit reconnect trigger

The library owns scheduling/plumbing; user provider logic owns payload meaning.

## 7) Factory-first construction

Preferred creation path is `AsyncioNetworkFactory`.

Factory responsibilities:

- consistent construction
- explicit validation
- stable user-facing setup path
- explicit conformance to the `aionetx.api.NetworkFactory` contract

Factory must not become implicit orchestration that hides semantics.

## 8) Unified canonical identifier model

Identifiers are part of the observable contract.

Requirements:

- deterministic derivation
- stable structure
- human-readable enough for logs/tests/diagnostics
- consistent model across transport roles

Identifiers support event correlation, lifecycle observability, and semantic testing.

## 9) Testing trust model

Testing is semantic evidence, not only coverage accounting.

Priority areas:

- lifecycle correctness
- event ordering guarantees
- cancellation/shutdown safety
- reconnect behavior
- backpressure behavior

Coverage is necessary but insufficient without behavioral confidence.

## 10) UDP sender asymmetry (intentional)

`UdpSenderProtocol` remains lifecycle-light by design:

- no managed lifecycle stream
- no connection metadata stream
- focused capability: datagram send + idempotent stop

Built-in heartbeat for UDP sender is intentionally out of current scope to avoid turning it into a managed runtime role.

## 11) Compatibility and change policy

Compatibility-breaking redesign is allowed only when it materially improves
long-term API quality.

That flexibility is not permission for arbitrary churn.

Required discipline:

- clear rationale
- semantically beneficial outcome
- explicit migration guidance when user adaptation is required across
  supported upgrade paths
- record durable compatibility notes in `docs/breaking_changes/` when user
  adaptation is required

## 12) Current limitations and explicit non-goals

- No built-in framing/parsing helpers in transport core
- No protocol adapters/codecs in this repository
- No hidden recovery automation beyond explicit settings
- No application-level policy engine (retry/routing/business orchestration)

These are deliberate to preserve transport boundary clarity and predictable behavior.

See [platform notes](./platform_notes.md) for Windows socket option semantics.

## 13) Decision index (consolidated from removed ADR set)

This architecture doc now holds the active knowledge that was previously split across ADR 001-013:

1. per-connection sequential event dispatch
2. explicit reconnect strategy
3. bounded/configurable backpressure
4. four-state lifecycle authority and transition guards
5. factory-first setup model
6. strict transport-only boundary
7. provider-driven heartbeat semantics
8. single event integration contract
9. quality-first compatibility and change policy
10. canonical identifier model
11. semantic testing trust model
12. lifecycle authority consolidation over historical variants
13. preserved UDP sender asymmetry (no built-in heartbeat)


## 14) API boundary and import-path guidance

Public boundary expectations:

- `aionetx` package-root curated exports are the default/common onboarding surface.
- `aionetx.api` curated exports are stable advanced contracts for explicit typing/composition.
- `aionetx.api._*` and `aionetx.implementations.*` are internal by default.

This preserves a small beginner path while keeping explicit advanced contracts available.

Curation invariant for `aionetx.api`: every type reachable from a public
signature (method parameter, return type, dataclass field, or exception
raised by a documented operation) is itself part of `aionetx.api`'s curated
exports. Reaching into non-underscore submodules by name is a public-API
gap, not an abstraction. Capability-mixin protocols whose members are
already exposed through the concrete role-specific protocols
(`ManagedTransportProtocol`, `ByteSenderProtocol`), the `BytesLike` alias,
and advanced helper types remain intentionally out of the package-root list.

Settings validation model: public settings dataclasses fail fast at
construction time via `__post_init__()`. The explicit `.validate()` method
remains part of the contract as an idempotent re-check helper, but invalid
settings objects are not expected to circulate in normal use.

## 15) Dispatch/runtime phase clarifications

For `BACKGROUND` dispatch mode, runtime behavior is intentionally phase-aware:

- before dispatcher worker startup: emission may fall back to inline delivery
- steady-state: dispatcher worker delivers queued events
- during dispatcher shutdown: newly emitted events may be dropped to guarantee deterministic stop completion

This shutdown-phase drop behavior is a deliberate deterministic-shutdown tradeoff and not a hidden overload policy.
Runtime diagnostics should make this distinction explicit by separating:

- overload drops (`DROP_OLDEST`/`DROP_NEWEST`)
- shutdown-phase drops after stop begins
- queue occupancy snapshots (current + peak)

For handler-failure-driven shutdown, the dispatcher uses an explicit
`DispatcherStopPolicy` contract. Under
`handler_failure_policy=STOP_COMPONENT`, it emits
`HandlerFailurePolicyStopEvent` before invoking the configured stop callback.

In concrete asyncio TCP transports, this diagnostic snapshot is exposed via
`dispatcher_runtime_stats` on the client/server objects.

## 16) Verification posture interpretation

Verification signals are interpreted in tiers:

- required CI/release gates are primary trust evidence for runtime semantics and packaging
- non-blocking lanes are secondary/environmental signals
- semantic behavioral confidence (lifecycle/ordering/cancellation/reconnect) is prioritized over raw coverage alone

The architecture goal is trustworthy runtime semantics, not process-heavy governance signaling.

