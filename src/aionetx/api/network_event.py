"""
Type-level union of every event dataclass delivered to ``on_event``.

Open-union policy
-----------------
``NetworkEvent`` is an *open* union. New event dataclasses may be added in
minor releases as the library's observability surface grows. In practice
that means:

- Existing event dataclasses, their field names, and their field types are
  stable: fields are never renamed, retyped, or removed without a
  deprecation cycle and a CHANGELOG entry.
- New event types may appear in any minor release; additions are always
  called out under **Added** in the CHANGELOG.
- Removing an event type from the union is treated as a breaking change
  and reserved for major releases (or, during the pre-1.0 phase, for
  minor bumps with an explicit **Removed** CHANGELOG entry).

Consequences for handler code
-----------------------------
User code that branches on event type must keep a catch-all fallback so a
future union extension does not silently break:

.. code-block:: python

    async def on_event(self, event: NetworkEvent) -> None:
        if isinstance(event, BytesReceivedEvent):
            ...
        elif isinstance(event, ConnectionClosedEvent):
            ...
        else:
            # Unknown event type from a newer aionetx release: ignore,
            # log, or forward — but do not assert exhaustiveness.
            pass

``match``/``case`` users should always include a ``case _:`` arm.
``typing.assert_never`` is intentionally **not** safe against this union
because it assumes a closed taxonomy.

``BaseNetworkEventHandler`` handles this correctly out-of-the-box: its
typed hooks are optional overrides, and unknown event types fall back to
its default no-op ``on_event`` dispatch.
"""

from __future__ import annotations

from typing import TypeAlias

from aionetx.api._event_registry import NetworkEvent as _NetworkEvent

NetworkEvent: TypeAlias = _NetworkEvent
