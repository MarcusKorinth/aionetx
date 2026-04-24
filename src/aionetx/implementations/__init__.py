"""
Internal implementation namespace.

This module intentionally does not re-export concrete runtime classes.
Stable imports are curated at package root (``aionetx``) and
``aionetx.api``. Importing from ``aionetx.implementations`` is
supported only for tests and advanced internal integrations and may change
between releases.
"""

__all__: list[str] = []


def __getattr__(name: str) -> object:
    raise AttributeError(
        f"module 'aionetx.implementations' has no attribute {name!r}. "
        "This module is internal; import from 'aionetx' instead."
    )
