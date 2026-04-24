"""
Shared bytes-like payload alias used across transport APIs.

``BytesLike`` intentionally matches the payload types accepted by Python's
socket and stream APIs: ``bytes``, ``bytearray``, and ``memoryview``.
"""

from __future__ import annotations

from typing import TypeAlias

BytesLike: TypeAlias = bytes | bytearray | memoryview
