"""Regression check for the optional Hypothesis test group."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.hypothesis


def test_hypothesis_extra_unavailable_is_reported_as_optional_skip() -> None:
    """Keep ``pytest -m hypothesis`` green when the optional extra is absent."""
    pytest.importorskip(
        "hypothesis",
        reason="hypothesis not installed; skipping property-based tests",
    )
