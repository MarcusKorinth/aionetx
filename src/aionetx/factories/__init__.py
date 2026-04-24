"""
Factory entry points for concrete backend implementations.

The public package root re-exports :class:`AsyncioNetworkFactory`, but this
subpackage keeps the actual factory implementation in one focused place.
"""

from aionetx.factories.asyncio_network_factory import AsyncioNetworkFactory

__all__ = ["AsyncioNetworkFactory"]
