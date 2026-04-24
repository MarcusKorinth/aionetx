"""Internal validation helpers for public settings dataclasses."""

from __future__ import annotations

import math
from enum import Enum
from numbers import Real
from typing import TypeVar

from aionetx.api.errors import InvalidNetworkConfigurationError

_TEnum = TypeVar("_TEnum", bound=Enum)
_ErrorType = type[Exception]


def require_bool(
    *, field_name: str, value: object, error_type: _ErrorType = InvalidNetworkConfigurationError
) -> bool:
    if type(value) is not bool:
        raise error_type(f"{field_name} must be a bool.")
    return value


def require_non_empty_str(
    *, field_name: str, value: object, error_type: _ErrorType = InvalidNetworkConfigurationError
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{field_name} must not be empty or whitespace-only.")
    return value


def require_optional_non_empty_str(
    *, field_name: str, value: object, error_type: _ErrorType = InvalidNetworkConfigurationError
) -> str | None:
    if value is None:
        return None
    return require_non_empty_str(field_name=field_name, value=value, error_type=error_type)


def require_int_range(
    *,
    field_name: str,
    value: object,
    min_value: int,
    max_value: int,
    error_type: _ErrorType = InvalidNetworkConfigurationError,
) -> int:
    if type(value) is not int or not (min_value <= value <= max_value):
        raise error_type(f"{field_name} must be between {min_value} and {max_value}.")
    return value


def require_positive_int(
    *, field_name: str, value: object, error_type: _ErrorType = InvalidNetworkConfigurationError
) -> int:
    if type(value) is not int or value <= 0:
        raise error_type(f"{field_name} must be > 0.")
    return value


def require_positive_finite_number(
    *, field_name: str, value: object, error_type: _ErrorType = InvalidNetworkConfigurationError
) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise error_type(f"{field_name} must be a finite number > 0.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise error_type(f"{field_name} must be a finite number > 0.")
    return number


def require_optional_positive_finite_number(
    *, field_name: str, value: object, error_type: _ErrorType = InvalidNetworkConfigurationError
) -> float | None:
    if value is None:
        return None
    return require_positive_finite_number(field_name=field_name, value=value, error_type=error_type)


def require_finite_number_at_least(
    *,
    field_name: str,
    value: object,
    minimum: float,
    error_type: _ErrorType = InvalidNetworkConfigurationError,
) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise error_type(f"{field_name} must be a finite number >= {minimum}.")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise error_type(f"{field_name} must be a finite number >= {minimum}.")
    return number


def require_enum_member(
    *,
    field_name: str,
    value: object,
    enum_type: type[_TEnum],
    error_type: _ErrorType = InvalidNetworkConfigurationError,
) -> _TEnum:
    if not isinstance(value, enum_type):
        raise error_type(f"{field_name} must be a {enum_type.__name__} value.")
    return value
