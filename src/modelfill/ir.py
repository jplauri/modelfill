"""Small immutable, solver-independent constraint representation."""

from __future__ import annotations

import operator
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, EnumType, Flag, IntEnum, ReprEnum, StrEnum
from fractions import Fraction
from typing import Any, Literal

Kind = Literal[
    "int", "bool", "str", "enum", "none", "date", "datetime", "time", "timedelta", "decimal"
]


@dataclass(frozen=True)
class Domain:
    kind: Kind
    choices: tuple[Any, ...] | None = None
    nullable: bool = False  # Also accepts None, which the solver tracks as a flag.
    # A decimal's values are whole numbers of 10**-scale, with at most digits digits
    # when that is set, as max_digits sets it.
    scale: int = 0
    digits: int | None = None

    def accepts(self, value: Any) -> bool:
        if value is None:
            return self.nullable
        if self.kind == "enum":
            return self.choices is not None and any(value is x for x in self.choices)
        if self.kind == "decimal":  # Finite, on the grid; trailing zeros do not matter.
            return (
                type(value) is Decimal
                and value.is_finite()
                and (Fraction(value) * 10**self.scale).denominator == 1
            )
        primitive = PRIMITIVES.get(type(value))
        return (
            primitive is not None
            and primitive.kind == self.kind
            and getattr(value, "tzinfo", None) is None  # Only naive datetimes are encoded.
            and (self.choices is None or value in self.choices)
        )


INT = Domain("int")
BOOL = Domain("bool")
STR = Domain("str")
NONE = Domain("none", (None,))  # For None constants; fields use nullable domains.
TIME_TYPES: dict[Kind, type[date] | type[time] | type[timedelta]] = {
    "date": date,
    "datetime": datetime,
    "time": time,
    "timedelta": timedelta,
}
PRIMITIVES = {int: INT, bool: BOOL, str: STR} | {t: Domain(k) for k, t in TIME_TYPES.items()}

# Dates count days; naive datetimes and timedeltas count microseconds, and naive
# times those since midnight. All preserve order and turn Python's date
# arithmetic into integer arithmetic; times have none. A decimal counts units of
# its scale: 12.34 is 1234 hundredths.
DAY = timedelta(days=1) // timedelta(microseconds=1)


def encode(value: int | date | datetime | time | timedelta | Decimal, scale: int = 0) -> int:
    if isinstance(value, Decimal):  # On its grid, so exact, beyond any precision too.
        return int(Fraction(value) * 10**scale)
    if isinstance(value, timedelta):
        return value // timedelta(microseconds=1)
    if isinstance(value, datetime):  # Before date: every datetime is also a date.
        return (value - datetime.min) // timedelta(microseconds=1)
    if isinstance(value, date):
        return value.toordinal()
    if isinstance(value, time):
        return (value.hour * 3600 + value.minute * 60 + value.second) * 10**6 + value.microsecond
    return value


def decode(
    kind: str, number: int, scale: int = 0
) -> int | date | datetime | time | timedelta | Decimal:
    if kind == "int":
        return number
    if kind == "decimal":
        return Decimal(f"{number}e{-scale}")  # Exact, with scale places: 1.50.
    if kind == "timedelta":
        return timedelta(microseconds=number)
    if kind == "datetime":
        return datetime.min + timedelta(microseconds=number)
    if kind == "time":
        return (datetime.min + timedelta(microseconds=number)).time()
    return date.fromordinal(number)


BOUNDS = {kind: (encode(type_.min), encode(type_.max)) for kind, type_ in TIME_TYPES.items()}


def encodable(value: Any) -> bool:
    """Z3 strings cover code points only up to U+2FFFF."""
    return type(value) is not str or all(ord(char) <= 0x2FFFF for char in value)


# The Enum encoding relies on these bases' standard comparison and truth behavior.
_STANDARD_ENUM_BASES = {Enum, IntEnum, StrEnum, ReprEnum, int, str, object}
_SEMANTIC_HOOKS = {
    "__eq__",
    "__ne__",
    "__lt__",
    "__le__",
    "__gt__",
    "__ge__",
    "__bool__",
    "__len__",
    "__int__",
    "__index__",
    "__getattribute__",
    "__getattr__",
    "_missing_",
}


def plain_enum(enum_type: type[Enum]) -> bool:
    """Reject Flags, custom metaclasses and redefined comparison/truth/attribute hooks."""
    return (
        type(enum_type) is EnumType
        and not issubclass(enum_type, Flag)
        and not any(
            _SEMANTIC_HOOKS.intersection(parent.__dict__)
            for parent in enum_type.__mro__
            if parent not in _STANDARD_ENUM_BASES
        )
    )


@dataclass(frozen=True)
class Expr:
    op: str
    sort: Domain
    args: tuple[Expr, ...] = ()
    value: Any = None


def var(name: str, domain: Domain) -> Expr:
    return Expr("var", domain, value=name)


def const(value: Any) -> Expr:
    if isinstance(value, Enum):
        domain = Domain("enum", tuple(type(value)))
    elif isinstance(value, Decimal):  # On its own grid: Decimal("1.50") counts hundredths.
        scale = max(0, -int(value.as_tuple().exponent))
        domain = Domain("decimal", scale=scale, digits=len(str(abs(encode(value, scale)))))
    else:
        domain = NONE if value is None else PRIMITIVES[type(value)]
    return Expr("const", domain, value=value)


def node(op: str, *args: Expr, sort: Domain = BOOL) -> Expr:
    return Expr(op, sort, args)


def prefixed(expr: Expr, prefix: str) -> Expr:
    """The same expression over a nested model's fields: age becomes customer.age."""
    if expr.op == "var":
        return replace(expr, value=prefix + expr.value)
    return replace(expr, args=tuple(prefixed(arg, prefix) for arg in expr.args))


# IR operations that the operator module lacks.
_FUNCTIONS = {
    "not": operator.not_,
    "len": len,
    "startswith": str.startswith,
    "endswith": str.endswith,
    "weekday": date.weekday,  # A datetime's too.
}


def evaluate(expr: Expr, values: Mapping[str, Any]) -> Any:
    """Python's own meaning of an expression for concrete values, lazy like and/or."""
    if expr.op == "const":
        return expr.value
    if expr.op == "var":
        return values[expr.value]
    if expr.op == "isnone":
        return values[expr.args[0].value] is None
    if expr.op == "and":
        return all(evaluate(arg, values) for arg in expr.args)
    if expr.op == "or":
        return any(evaluate(arg, values) for arg in expr.args)
    if expr.op == "ite":
        condition, yes, no = expr.args
        return evaluate(yes if evaluate(condition, values) else no, values)
    args = [evaluate(arg, values) for arg in expr.args]
    function = _FUNCTIONS.get(expr.op) or getattr(operator, expr.op)
    return function(*args)
