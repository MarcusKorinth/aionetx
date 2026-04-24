"""
Asyncio implementation namespace (internal and unstable).

Concrete runtime modules/classes in this package are implementation details.
Prefer construction via ``aionetx.AsyncioNetworkFactory`` and protocol
imports from ``aionetx`` / ``aionetx.api``.
"""

__all__: list[str] = []


def __getattr__(name: str) -> object:
    raise AttributeError(
        f"aionetx.implementations.asyncio_impl.{name} is internal and unstable; "
        "construct transports via AsyncioNetworkFactory instead."
    )
