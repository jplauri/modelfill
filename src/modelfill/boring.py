"""Boring values for fields the solver does not model: fixed, readable and valid."""

from __future__ import annotations

import dataclasses
import inspect
import math
import types
import uuid
from collections import abc
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from annotated_types import Ge, Gt, Le, Lt, MinLen, MultipleOf
from pydantic import (
    AnyHttpUrl,
    AnyUrl,
    AwareDatetime,
    BaseModel,
    EmailStr,
    HttpUrl,
    NaiveDatetime,
    SecretBytes,
    SecretStr,
)
from pydantic.types import UuidVersion

from .adapters import _flatten, _resolve

MISSING = dataclasses.MISSING
_START = datetime(2025, 1, 1)  # The anchor that canonical dates and times use, too.
_SCALARS: dict[Any, Any] = {
    type(None): None,
    bool: False,
    date: _START.date(),
    datetime: _START,
    NaiveDatetime: _START,
    AwareDatetime: _START.replace(tzinfo=UTC),
    timedelta: timedelta(0),
    time: time(0, 0),
    EmailStr: "user@example.com",
    SecretStr: SecretStr(""),
    SecretBytes: SecretBytes(b""),
}
_URLS = (AnyUrl, AnyHttpUrl, HttpUrl)
_LISTS = (list, abc.Sequence, abc.MutableSequence, abc.Collection, abc.Iterable)
_SETS = (set, frozenset, abc.Set, abc.MutableSet)
_MAPPINGS = (dict, abc.Mapping, abc.MutableMapping)
_CHECKS = {
    Gt: lambda value, item: value > item.gt,
    Ge: lambda value, item: value >= item.ge,
    Lt: lambda value, item: value < item.lt,
    Le: lambda value, item: value <= item.le,
    MultipleOf: lambda value, item: math.isclose(
        value / item.multiple_of, round(value / item.multiple_of)
    ),
}


def nested_model(annotation: Any) -> type | None:
    """The model class behind ``M`` or ``M | None``, which modelfill fills recursively."""
    if get_origin(annotation) in (Union, types.UnionType):
        options = [option for option in get_args(annotation) if option is not type(None)]
        annotation = options[0] if len(options) == 1 else None
    if inspect.isclass(annotation) and (
        issubclass(annotation, BaseModel) or dataclasses.is_dataclass(annotation)
    ):
        return annotation
    return None


def boring(annotation: Any, metadata: Sequence[Any] = ()) -> Any:
    """A fixed, readable value of a type the solver does not model, or MISSING."""
    annotation, inherited = _resolve(annotation)  # NewType, type alias and Annotated layers.
    metadata = (*inherited, *metadata)
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation in (Any, object):
        return None
    if origin in (Union, types.UnionType):
        if type(None) in args:
            return None
        return next((value for value in map(boring, args) if value is not MISSING), MISSING)
    if origin is Literal:
        return args[0]
    limits = {type(item): item for item in _flatten(metadata)}  # The last of each kind wins.
    size = limits[MinLen].min_length if MinLen in limits else 0
    kind = origin or annotation
    if kind is tuple and args and args[-1] is not Ellipsis:  # A fixed shape: one per position.
        values = [boring(arg) for arg in args]
        return MISSING if any(value is MISSING for value in values) else tuple(values)
    if kind in _LISTS or kind is tuple:  # Empty, or min_length copies of the element's value.
        element = boring(args[0]) if args else None
        if size and element is MISSING:
            return MISSING
        return (tuple if kind is tuple else list)([element] * size)
    if kind in _SETS:  # Distinct elements have no boring value beyond one.
        element = boring(args[0]) if args else None
        if size > 1 or (size and element is MISSING):
            return MISSING
        return (frozenset if kind is frozenset else set)([element] * size)
    if kind in _MAPPINGS:
        return MISSING if size else {}
    if kind is str:
        return "_" * size
    if kind is bytes:
        return bytes(size)
    if kind in (int, float, Decimal):
        number = _number(limits)
        if number is MISSING or (kind is int and number != int(number)):
            return MISSING
        return Decimal(str(number)) if kind is Decimal else kind(number)
    if kind is uuid.UUID:  # Zeros, with the version bits the annotation requires.
        version = limits[UuidVersion].uuid_version if UuidVersion in limits else 4
        return uuid.UUID(int=version << 76 | 1 << 63)
    if kind in _SCALARS:
        return _SCALARS[kind]
    if kind in _URLS:
        return kind("https://example.com/")
    if inspect.isclass(kind) and issubclass(kind, Enum):
        return next(iter(kind), MISSING)
    return MISSING


def _number(limits: dict[type, Any]) -> Any:
    """0, else the whole number (or multiple) nearest 0 within the bounds, else their middle."""
    step = limits[MultipleOf].multiple_of if MultipleOf in limits else 1

    def fits(value: Any) -> bool:
        return all(check(value, limits[kind]) for kind, check in _CHECKS.items() if kind in limits)

    bounds = [
        getattr(limits[kind], kind.__name__.lower()) for kind in (Gt, Ge, Lt, Le) if kind in limits
    ]
    # The multiples just around each bound include the nearest allowed one on its side;
    # an infinite bound has none.
    candidates = [0] + [
        (math.floor(bound / step) + offset) * step
        for bound in bounds
        if math.isfinite(bound)
        for offset in (-1, 0, 1, 2)
    ]
    allowed = [value for value in candidates if fits(value)]
    if allowed:
        return min(allowed, key=lambda value: (abs(value), value < 0))  # A tie goes up.
    lower = [getattr(limits[kind], kind.__name__.lower()) for kind in (Gt, Ge) if kind in limits]
    upper = [getattr(limits[kind], kind.__name__.lower()) for kind in (Lt, Le) if kind in limits]
    if lower and upper and fits(middle := (max(lower) + min(upper)) / 2):
        return middle
    return MISSING
