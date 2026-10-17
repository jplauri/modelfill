"""Boring values for fields the solver does not model: fixed, readable and valid."""

from __future__ import annotations

import dataclasses
import inspect
import math
import types
import uuid
from collections import abc
from collections.abc import Callable, Sequence
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
    FutureDate,
    FutureDatetime,
    HttpUrl,
    NaiveDatetime,
    PastDate,
    PastDatetime,
    SecretBytes,
    SecretStr,
)
from pydantic.types import UuidVersion

from .adapters import _flatten, _resolve
from .errors import ModelFillError

MISSING = dataclasses.MISSING
Fill = Callable[[type, dict[str, Any]], Any]  # Completes a model from some fields.
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
    # Fixed, so canonical values stay put: past for good, and future until 2100.
    PastDate: date(2000, 1, 1),
    FutureDate: date(2100, 1, 1),
    PastDatetime: datetime(2000, 1, 1),
    FutureDatetime: datetime(2100, 1, 1),
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


def models_in(annotation: Any) -> list[type]:
    """The models a value of this type can hold, at any depth: itself, a collection's
    elements or values, a union's members."""
    annotation = _resolve(annotation)[0]
    if nested_model(annotation) is annotation:
        return [annotation]
    return [model for arg in get_args(annotation) for model in models_in(arg)]


def boring(annotation: Any, metadata: Sequence[Any] = (), fill: Fill | None = None) -> Any:
    """A fixed, readable value of a type the solver does not model, or MISSING.

    A model has no boring value, but with fill one gets fill(model, {}): in its own
    place, as each of a collection's elements, or as a union's first member to fill
    when no member is boring.
    """
    annotation, inherited = _resolve(annotation)  # NewType, type alias and Annotated layers.
    metadata = (*inherited, *metadata)
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation in (Any, object):
        return None
    if fill is not None and nested_model(annotation) is annotation:
        return fill(annotation, {})
    if origin in (Union, types.UnionType):
        if type(None) in args:
            return None
        value = next((value for value in map(boring, args) if value is not MISSING), MISSING)
        return value if value is not MISSING or fill is None else _first(args, fill)
    if origin is Literal:
        return args[0]
    limits = {type(item): item for item in _flatten(metadata)}  # The last of each kind wins.
    size = limits[MinLen].min_length if MinLen in limits else 0
    kind = origin or annotation
    if kind is tuple and args and args[-1] is not Ellipsis:  # A fixed shape: one per position.
        values = [boring(arg, fill=fill) for arg in args]
        return MISSING if any(value is MISSING for value in values) else tuple(values)
    if kind in _LISTS or kind is tuple:  # Empty, or min_length elements, each made anew.
        elements = [boring(args[0], fill=fill) if args else None for _ in range(size)]
        if any(element is MISSING for element in elements):
            return MISSING
        return (tuple if kind is tuple else list)(elements)
    if kind in _SETS:
        elements = _distinct(args[0] if args else Any, size)
        return MISSING if elements is None else (frozenset if kind is frozenset else set)(elements)
    if kind in _MAPPINGS:  # Distinct keys, each with the value type's boring value.
        keys = _distinct(args[0] if args else Any, size)
        if keys is None:
            return MISSING
        values = {key: boring(args[1], fill=fill) if len(args) == 2 else None for key in keys}
        return MISSING if any(value is MISSING for value in values.values()) else values
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


def complete(annotation: Any, given: Any, fill: Fill) -> Any:
    """given, with each dict that stands for a model completed by fill(model, dict): in
    the model's place, as a collection's element or value, or for the first of a
    union's model members that takes it. The shape stays as given."""
    annotation = _resolve(annotation)[0]
    if nested_model(annotation) is annotation:
        return fill(annotation, given) if isinstance(given, dict) else given
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        members = [option for option in args if models_in(option)]
        if not members or not isinstance(given, (dict, list, tuple)):
            return given
        failures: list[Exception] = []
        for option in members:  # A tagged union's tag rules out the other branches.
            try:
                return complete(option, given, fill)
            except (ModelFillError, TypeError, ValueError) as error:
                failures.append(error)
        if len(members) < len([option for option in args if option is not type(None)]):
            return given  # A member without models may take it as it is.
        raise failures[0]
    kind = origin or annotation
    if not args or not isinstance(given, (dict, list, tuple)):
        return given
    if kind is tuple and args[-1] is not Ellipsis:  # A fixed shape: one per position.
        if len(given) != len(args):
            return given  # Pydantic rejects the shape.
        return tuple(complete(arg, value, fill) for arg, value in zip(args, given, strict=True))
    if (kind in _LISTS or kind is tuple) and not isinstance(given, dict):
        values = [complete(args[0], value, fill) for value in given]
        return tuple(values) if kind is tuple else values
    if kind in _MAPPINGS and len(args) == 2 and isinstance(given, dict):
        return {key: complete(args[1], value, fill) for key, value in given.items()}
    return given


def _first(options: Sequence[Any], fill: Fill) -> Any:
    """The first union member that fill can make, in declaration order, or MISSING."""
    failure = None
    for option in options:
        try:
            value = boring(option, fill=fill)
        except ModelFillError as error:  # This member cannot be filled; the next may.
            failure = failure or error
            continue
        if value is not MISSING:
            return value
    if failure is not None:
        raise failure
    return MISSING


def _distinct(annotation: Any, count: int) -> list[Any] | None:
    """count distinct boring values of a type, or None: its boring value, then the next."""
    first = boring(annotation)
    annotation = _resolve(annotation)[0]
    if not count:
        return []
    if get_origin(annotation) is Literal:
        options = list(get_args(annotation))
    elif inspect.isclass(annotation) and issubclass(annotation, Enum):
        options = list(annotation)
    elif annotation is bool:
        options = [False, True]
    elif type(first) in (int, float, Decimal):
        options = [first + step for step in range(count)]
    elif type(first) is str:  # "", "1", "2", ... after any min_length padding.
        options = [first + str(step) if step else first for step in range(count)]
    elif type(first) in (date, datetime):
        options = [first + timedelta(days=step) for step in range(count)]
    else:
        options = [] if first is MISSING else [first]
    return options[:count] if len(options) >= count else None


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
