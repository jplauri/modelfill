"""Conservative translation of pure rejection validators into typed expressions.

This module never executes validator code and does not depend on the solver.
Only accepted syntax is traversed; an unsupported reachable statement rejects
the whole validator instead of contributing a partial constraint. Operations
that raise for some inputs (TypeError on None, OverflowError past date limits)
become requirements: a path where one would raise rejects, like a raise.
"""

from __future__ import annotations

import ast
import datetime
import decimal
import inspect
import textwrap
from collections.abc import Callable, Collection, Mapping, Sequence
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic_core import PydanticCustomError

from .errors import UnsupportedConstraint
from .ir import (
    INT,
    PRIMITIVES,
    TIME_TYPES,
    Domain,
    Expr,
    Kind,
    const,
    encodable,
    node,
    plain_enum,
    var,
)

# Falsy values for truth tests; dates and datetimes are always truthy.
_ZERO = {"int": 0, "str": "", "timedelta": datetime.timedelta(0), "decimal": Decimal(0)}

# Types of named constants (module globals or closures) read into validators.
_CONSTANT_TYPES = (int, bool, str, type(None), Decimal, *TIME_TYPES.values())

# A name with no constant value: a parameter, a local or an unknown name. Unlike
# None, which is a real constant, it is never read as a value.
_UNRESOLVED = object()

# What a validator may raise to reject: Pydantic turns each into a ValidationError,
# and a dataclass's __post_init__ rejects with any of them.
_REJECTIONS = (ValueError, AssertionError, PydanticCustomError)

# The integer attributes of dates and times that validators may read, by kind.
_PARTS = {
    "date": ("year",),
    "datetime": ("year", "hour", "minute", "second", "microsecond"),
    "time": ("hour", "minute", "second", "microsecond"),
    "timedelta": ("days", "seconds", "microseconds"),
}

# Kinds that min() and max() compare, one kind at a time.
_ORDERED = ("int", "date", "datetime", "time", "timedelta")

_OPERATORS = {
    ast.Add: "add",
    ast.Sub: "sub",
    ast.Mult: "mul",
    ast.Mod: "mod",
    ast.FloorDiv: "floordiv",
}

# Result kinds of supported arithmetic, following Python's int, Decimal and datetime
# rules. Booleans count as integers. To stay linear, * needs a literal number operand,
# and % and // a positive integer literal divisor (where Python and Z3 agree).
_ARITHMETIC: dict[tuple[str, str, str], Kind] = {
    ("add", "int", "int"): "int",
    ("sub", "int", "int"): "int",
    ("mul", "int", "int"): "int",
    ("mod", "int", "int"): "int",
    ("floordiv", "int", "int"): "int",
    ("add", "timedelta", "timedelta"): "timedelta",
    ("sub", "timedelta", "timedelta"): "timedelta",
    ("mul", "timedelta", "int"): "timedelta",
    ("mul", "int", "timedelta"): "timedelta",
    ("add", "datetime", "timedelta"): "datetime",
    ("add", "timedelta", "datetime"): "datetime",
    ("sub", "datetime", "timedelta"): "datetime",
    ("sub", "datetime", "datetime"): "timedelta",
    ("add", "date", "timedelta"): "date",
    ("add", "timedelta", "date"): "date",
    ("sub", "date", "timedelta"): "date",
    ("sub", "date", "date"): "timedelta",
} | {  # A Decimal takes an int exactly.
    (operation, left, right): "decimal"
    for operation in ("add", "sub", "mul")
    for left, right in (("decimal", "decimal"), ("decimal", "int"), ("int", "decimal"))
}


def _and(*values: Expr) -> Expr:
    if any(value == const(False) for value in values):
        return const(False)
    remaining = tuple(value for value in values if value != const(True))
    return node("and", *remaining) if len(remaining) > 1 else next(iter(remaining), const(True))


def _or(*values: Expr) -> Expr:
    if any(value == const(True) for value in values):
        return const(True)
    remaining = tuple(value for value in values if value != const(False))
    return node("or", *remaining) if len(remaining) > 1 else next(iter(remaining), const(False))


def _not(value: Expr) -> Expr:
    if value.op == "const":
        return const(not value.value)
    return value.args[0] if value.op == "not" else node("not", value)


class _Analyzer:
    def __init__(
        self,
        function: Callable[..., Any],
        fields: Mapping[str, Domain],
        kind: Literal["model", "dataclass", "field"],
        field_name: str | None,
        earlier_fields: Collection[str],
        constants: Mapping[str, Any],
    ) -> None:
        self.function = function
        self.fields = fields
        self.kind = kind
        self.field_name = field_name
        self.earlier_fields = earlier_fields
        self.constants = constants
        self.receiver: str | None = None
        self.owner: str | None = None  # A field validator's cls parameter.
        self.value_name: str | None = None
        self.info_name: str | None = None
        self.local_names: set[str] = set()
        self.bound: dict[str, Expr] = {}
        self.requires: list[Expr] = []
        try:
            found = inspect.getclosurevars(function)
            self.names = {**found.builtins, **found.globals, **found.nonlocals}
        except (TypeError, ValueError):
            self.names = {}

    def unsupported(self, syntax: ast.AST | None, reason: str) -> UnsupportedConstraint:
        name = getattr(self.function, "__qualname__", repr(self.function))
        location = f" at line {syntax.lineno}" if hasattr(syntax, "lineno") else ""
        return UnsupportedConstraint(f"{name}{location}: {reason}")

    def resolve(self, name: str) -> Any:
        return _UNRESOLVED if name in self.local_names else self.names.get(name, _UNRESOLVED)

    def guarded(self, analyze: Callable[..., Any], *args: Any) -> tuple[Any, Expr]:
        """Analyze in isolation; also return the condition for it not to raise."""
        outer, self.requires = self.requires, []
        result = analyze(*args)
        safe, self.requires = _and(*self.requires), outer
        return result, safe

    def present(self, value: Expr) -> Expr:
        """A use where None raises TypeError, so it requires a value."""
        if value.sort.nullable:
            self.requires.append(_not(node("isnone", value)))
        return value

    def digits(self, value: Expr, scale: int, syntax: ast.AST) -> int:
        """At most how many digits a number has in units of 10**-scale, which decimal
        arithmetic needs to know."""
        digits = _digits(value, scale)
        if digits is None:
            raise self.unsupported(syntax, "decimal arithmetic needs max_digits on its fields")
        return digits

    def exact(self, scale: int, digits: int, syntax: ast.AST) -> Domain:
        """A decimal result's sort, when Python computes it without rounding: Decimal
        arithmetic rounds a result past the context's precision."""
        if digits > decimal.getcontext().prec:
            raise self.unsupported(syntax, "decimal arithmetic could round past the precision")
        return Domain("decimal", scale=scale, digits=digits)

    def in_range(self, value: Expr) -> Expr:
        """Date and time arithmetic past Python's limits raises OverflowError."""
        if value.sort.kind in TIME_TYPES:
            limits = TIME_TYPES[value.sort.kind]
            self.requires.append(
                _and(node("le", const(limits.min), value), node("le", value, const(limits.max)))
            )
        return value

    @staticmethod
    def is_none(value: Expr) -> Expr:
        if value.sort.kind == "none":
            return const(True)
        return node("isnone", value) if value.sort.nullable else const(False)

    @staticmethod
    def reached(truths: list[Expr], conjunction: bool) -> Expr:
        """Whether a short-circuiting and/or evaluates its next operand."""
        return _and(*truths) if conjunction else _not(_or(*truths))

    def analyze(self) -> Expr:
        if getattr(self.function, "__wrapped__", None) is not None:
            raise self.unsupported(None, "wrapped validators cannot be analyzed safely")
        if inspect.iscoroutinefunction(self.function) or inspect.isgeneratorfunction(self.function):
            raise self.unsupported(None, "async and generator validators are unsupported")
        try:
            lines, first_line = inspect.getsourcelines(self.function)
            parsed = ast.parse(textwrap.dedent("".join(lines)))
        except (OSError, TypeError, SyntaxError, IndentationError) as exc:
            raise self.unsupported(None, "Python source is unavailable") from exc
        ast.increment_lineno(parsed, first_line - 1)  # Report lines of the file itself.
        if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.FunctionDef):
            raise self.unsupported(None, "expected one ordinary function definition")
        function = parsed.body[0]
        arguments = function.args
        if arguments.vararg or arguments.kwarg or arguments.kwonlyargs:
            raise self.unsupported(function, "variable or keyword-only validator arguments")
        names = [argument.arg for argument in (*arguments.posonlyargs, *arguments.args)]
        # As in Python, a name assigned anywhere in the function is local throughout it.
        assigned = {
            target.id
            for target in ast.walk(function)
            if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store)
        }
        if assigned & set(names):
            raise self.unsupported(function, "validator parameters cannot be reassigned")
        self.local_names = set(names) | assigned
        if self.kind == "field":
            target = self.field_name or ""
            if target not in self.fields and not any(
                f.startswith(f"{target}.") for f in self.fields
            ):
                raise self.unsupported(function, "field validator has no known target field")
            bound_class = inspect.ismethod(self.function) and isinstance(
                self.function.__self__, type
            )
            if bound_class or (names and names[0] == "cls"):
                self.owner, names = names[0], names[1:]
            if not 1 <= len(names) <= 2:
                raise self.unsupported(
                    function, "expected value and optional ValidationInfo arguments"
                )
            self.value_name = names[0]
            self.info_name = names[1] if len(names) == 2 else None
        else:
            if not 1 <= len(names) <= (2 if self.kind == "model" else 1):
                raise self.unsupported(
                    function, "expected self and optional ValidationInfo arguments"
                )
            if inspect.ismethod(self.function) and isinstance(self.function.__self__, type):
                raise self.unsupported(function, "classmethod model validators are unsupported")
            self.receiver = names[0]
            self.info_name = names[1] if len(names) == 2 else None
        accepted, fallthrough = self.block(function.body, top=True)
        if self.kind == "dataclass":
            return _or(accepted, fallthrough)
        if fallthrough != const(False):
            raise self.unsupported(
                function, "all successful paths must return the original self/value"
            )
        return accepted

    def block(self, statements: list[ast.stmt], top: bool = False) -> tuple[Expr, Expr]:
        """Return predicates for returning successfully and reaching the next block."""
        accepted, active = const(False), const(True)
        for statement in statements:
            if active == const(False):
                break
            if isinstance(statement, ast.If):
                condition, safe = self.guarded(self.truth, statement.test)
                active = _and(active, safe)  # A test that raises rejects.
                yes_return, yes_continue = self.block(statement.body)
                no_return, no_continue = self.block(statement.orelse)
                accepted = _or(
                    accepted,
                    _and(
                        active, _or(_and(condition, yes_return), _and(_not(condition), no_return))
                    ),
                )
                active = _and(
                    active,
                    _or(_and(condition, yes_continue), _and(_not(condition), no_continue)),
                )
            elif isinstance(statement, ast.Return):
                if self.kind == "dataclass":
                    valid = statement.value is None or (
                        isinstance(statement.value, ast.Constant) and statement.value.value is None
                    )
                else:
                    expected = self.value_name if self.kind == "field" else self.receiver
                    valid = isinstance(statement.value, ast.Name) and statement.value.id == expected
                if not valid:
                    raise self.unsupported(
                        statement, "return must preserve the original self/value"
                    )
                accepted, active = _or(accepted, active), const(False)
            elif isinstance(statement, ast.Raise):
                self.check_raise(statement)
                active = const(False)
            elif isinstance(statement, ast.Assert):
                if __debug__:  # A failing assert rejects; `python -O` removes asserts.
                    test, safe = self.guarded(self.truth, statement.test)
                    active = _and(active, safe, test)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value:
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                # A binding made inside a branch would be path-dependent; keep them top-level.
                if not top or len(targets) != 1 or not isinstance(targets[0], ast.Name):
                    raise self.unsupported(
                        statement, "only single-name assignments outside branches are supported"
                    )
                value, safe = self.guarded(self.expression, statement.value)
                active = _and(active, safe)
                self.bound[targets[0].id] = value
            elif isinstance(statement, ast.Pass):
                continue
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                if not isinstance(statement.value.value, str):
                    raise self.unsupported(
                        statement, "only string docstrings are supported statements"
                    )
            else:
                raise self.unsupported(
                    statement, f"unsupported statement {type(statement).__name__}"
                )
        return accepted, active

    def check_raise(self, statement: ast.Raise) -> None:
        # Arguments and a cause run only on this rejecting path; any message is fine.
        exc = statement.exc.func if isinstance(statement.exc, ast.Call) else statement.exc
        if not isinstance(exc, ast.Name) or self.resolve(exc.id) not in _REJECTIONS:
            raise self.unsupported(
                statement,
                "only raising builtin ValueError or AssertionError, or PydanticCustomError,"
                " is supported",
            )

    def truth(self, syntax: ast.expr) -> Expr:
        # A condition needs truth values, whereas Python and/or used as operands
        # must retain their original values (handled by expression() below).
        if isinstance(syntax, ast.BoolOp):
            conjunction = isinstance(syntax.op, ast.And)
            truths: list[Expr] = []
            for operand in syntax.values:
                truth, safe = self.guarded(self.truth, operand)
                self.requires.append(_or(_not(self.reached(truths, conjunction)), safe))
                truths.append(truth)
            return _and(*truths) if conjunction else _or(*truths)
        return self.truth_value(self.expression(syntax), syntax)

    def truth_value(self, value: Expr, syntax: ast.AST) -> Expr:
        if value.op == "const":
            return const(bool(value.value))
        kind = value.sort.kind
        if kind == "bool":
            truth = value
        elif kind in _ZERO:
            truth = node("ne", value, const(_ZERO[kind]))
        elif kind in ("date", "datetime", "time"):  # Midnight is truthy too.
            truth = const(True)
        elif kind == "enum" and value.sort.choices:
            truth = _or(
                *(node("eq", value, const(member)) for member in value.sort.choices if member)
            )
        else:
            raise self.unsupported(syntax, "unsupported truth-value test")
        return _and(_not(self.is_none(value)), truth)  # None is falsy.

    def constant(self, value: Any, syntax: ast.AST) -> Expr:
        """A literal or named constant, when the solver represents it exactly."""
        if isinstance(value, Enum) and not plain_enum(type(value)):
            raise self.unsupported(syntax, "Enum constants must have standard semantics")
        if not encodable(value):
            raise self.unsupported(syntax, "string constants above U+2FFFF are unsupported")
        if getattr(value, "tzinfo", None) is not None:
            raise self.unsupported(syntax, "aware datetime constants are unsupported")
        if isinstance(value, Decimal) and not value.is_finite():
            raise self.unsupported(syntax, "only finite Decimal constants are supported")
        return const(value)

    def expression(self, syntax: ast.expr) -> Expr:
        if isinstance(syntax, ast.Constant) and type(syntax.value) in (int, bool, str, type(None)):
            return self.constant(syntax.value, syntax)
        if isinstance(syntax, ast.Name):
            if syntax.id == self.value_name and self.field_name is not None:
                if self.field_name not in self.fields:
                    raise self.unsupported(syntax, "a nested model is read only through its fields")
                return var(self.field_name, self.fields[self.field_name])
            if syntax.id in self.bound:
                return self.bound[syntax.id]
            # Named constants are read once, like Enum classes: treat them as immutable.
            value = self.resolve(syntax.id)
            if type(value) in _CONSTANT_TYPES or isinstance(value, Enum):
                return self.constant(value, syntax)
            raise self.unsupported(syntax, "names must be fields, assigned locals or constants")
        if isinstance(syntax, ast.Attribute):
            path = self.field_path(syntax)
            if path in self.fields:
                return var(path, self.fields[path])
            # A class constant through self or cls: Pydantic sets no ClassVar on an instance.
            owner = syntax.value
            if isinstance(owner, ast.Name) and owner.id == self.info_name:
                if syntax.attr == "field_name":  # None in a model validator, as in Pydantic.
                    return const(self.field_name)
            if (
                isinstance(owner, ast.Name)
                and owner.id in (self.receiver, self.owner)
                and syntax.attr in self.constants
            ):
                value = self.constants[syntax.attr]
                if type(value) in _CONSTANT_TYPES or isinstance(value, Enum):
                    return self.constant(value, syntax)
            if isinstance(syntax.value, ast.Name):
                enum_type = self.resolve(syntax.value.id)
                if isinstance(enum_type, type) and issubclass(enum_type, Enum):
                    if not plain_enum(enum_type):
                        raise self.unsupported(
                            syntax, "Enum constants must have standard semantics"
                        )
                    if syntax.attr in enum_type.__members__:
                        return const(enum_type.__members__[syntax.attr])
            if syntax.attr in ("value", "name"):  # Of an Enum field or member.
                try:
                    member = self.expression(syntax.value)
                except UnsupportedConstraint:
                    member = None
                if member is not None and member.sort.kind == "enum" and member.sort.choices:
                    return self.member_attribute(member, syntax.attr, syntax)
            if any(syntax.attr in names for names in _PARTS.values()):
                value = self.present(self.expression(syntax.value))  # None has none.
                if syntax.attr in _PARTS.get(value.sort.kind, ()):
                    return node(syntax.attr, value, sort=INT)
            raise self.unsupported(
                syntax, "only model fields and literal Enum members are supported attributes"
            )
        if isinstance(syntax, ast.Subscript):
            target = syntax.value
            key = syntax.slice
            if (
                self.kind == "field"
                and isinstance(target, ast.Attribute)
                and target.attr == "data"
                and isinstance(target.value, ast.Name)
                and target.value.id == self.info_name
                and isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in self.earlier_fields
                and key.value in self.fields
            ):
                return var(key.value, self.fields[key.value])
            raise self.unsupported(
                syntax, "only info.data['earlier_field'] subscriptions are supported"
            )
        if isinstance(syntax, ast.UnaryOp):
            if isinstance(syntax.op, ast.Not):
                return _not(self.truth(syntax.operand))
            operand = self.present(self.expression(syntax.operand))
            kind = "int" if operand.sort.kind == "bool" else operand.sort.kind
            if kind not in ("int", "timedelta", "decimal") or not isinstance(
                syntax.op, (ast.UAdd, ast.USub)
            ):
                raise self.unsupported(syntax, "unary + and - need numbers or timedeltas")
            negate = isinstance(syntax.op, ast.USub)
            if kind == "decimal":  # Both round to the context's precision, as abs() does.
                scale = operand.sort.scale
                sort = self.exact(scale, self.digits(operand, scale, syntax), syntax)
                if operand.op == "const":
                    return const(-operand.value if negate else +operand.value)
                return node("neg", operand, sort=sort) if negate else operand
            if operand.op == "const" and kind == "int":
                return const(-operand.value if negate else +operand.value)
            if negate:
                return self.in_range(node("neg", operand, sort=Domain(kind)))
            # +x keeps a timedelta; for a Boolean it produces an integer.
            return node("add", const(0), operand, sort=INT) if kind == "int" else operand
        if isinstance(syntax, ast.BinOp):
            left = self.present(self.expression(syntax.left))
            right = self.present(self.expression(syntax.right))
            operation = _OPERATORS.get(type(syntax.op))
            literal = any(
                side.op == "const" and type(side.value) in (int, Decimal) for side in (left, right)
            )
            divisor = right.op == "const" and type(right.value) is int and right.value > 0
            allowed = {"mul": literal, "mod": divisor, "floordiv": divisor}.get(operation, True)
            kinds = (
                "int" if side.sort.kind == "bool" else side.sort.kind for side in (left, right)
            )
            result = _ARITHMETIC.get((operation, *kinds)) if operation and allowed else None
            if operation is None or result is None:
                raise self.unsupported(
                    syntax, "only +, -, * by a literal number and % or // by a positive integer"
                )
            sort = Domain(result)
            if result == "decimal":
                if operation == "mul":  # The units multiply, and so do the grids.
                    scale = left.sort.scale + right.sort.scale
                    digits = sum(
                        self.digits(side, side.sort.scale, syntax) for side in (left, right)
                    )
                else:  # On the finer grid, with at most one digit more.
                    scale = max(left.sort.scale, right.sort.scale)
                    digits = max(self.digits(side, scale, syntax) for side in (left, right)) + 1
                sort = self.exact(scale, digits, syntax)
            return self.in_range(node(operation, left, right, sort=sort))
        if isinstance(syntax, ast.BoolOp):
            conjunction = isinstance(syntax.op, ast.And)
            values: list[Expr] = []
            truths: list[Expr] = []
            final = len(syntax.values) - 1
            for index, operand in enumerate(syntax.values):
                value, safe = self.guarded(self.expression, operand)
                self.requires.append(_or(_not(self.reached(truths, conjunction)), safe))
                # A None that becomes the result has no encoding; `x or default` is fine.
                if value.sort.nullable and (conjunction or index == final):
                    raise self.unsupported(
                        syntax, "only `x or default` may use a possibly-None value"
                    )
                values.append(value)
                if index < final:
                    truths.append(self.truth_value(value, syntax))
            result = values[-1]
            for value, condition in zip(values[-2::-1], truths[::-1], strict=True):
                yes, no = (result, value) if conjunction else (value, result)
                result = self.choice(condition, yes, no, syntax)
            return result
        if isinstance(syntax, ast.IfExp):
            condition, safe = self.guarded(self.truth, syntax.test)
            yes, yes_safe = self.guarded(self.expression, syntax.body)
            no, no_safe = self.guarded(self.expression, syntax.orelse)
            if yes.sort.nullable or no.sort.nullable or "none" in (yes.sort.kind, no.sort.kind):
                raise self.unsupported(syntax, "a conditional value that may be None")
            # Only the branch taken runs, so only its requirements apply.
            self.requires += [safe, _or(_not(condition), yes_safe), _or(condition, no_safe)]
            return self.choice(condition, yes, no, syntax)
        if isinstance(syntax, ast.Compare):
            left = self.expression(syntax.left)
            links: list[Expr] = []
            for operation, right_syntax in zip(syntax.ops, syntax.comparators, strict=True):
                (link, left), safe = self.guarded(self.link, left, operation, right_syntax, syntax)
                # A link of a chain only runs when the earlier links held.
                self.requires.append(_or(_not(_and(*links)), safe))
                links.append(link)
            return _and(*links)
        if isinstance(syntax, ast.Call):
            method = syntax.func
            if (
                isinstance(method, ast.Attribute)
                and method.attr in ("startswith", "endswith")
                and len(syntax.args) == 1
                and not syntax.keywords
            ):
                return self.affix(method.attr, method.value, syntax.args[0], syntax)
            if (
                isinstance(method, ast.Attribute)
                and method.attr in ("weekday", "isoweekday")
                and not syntax.args
                and not syntax.keywords
            ):
                day = self.present(self.expression(method.value))  # None has no weekday.
                if day.sort.kind in ("date", "datetime"):
                    weekday = node("weekday", day, sort=INT)  # Monday is 0, and 1 in ISO.
                    if method.attr == "isoweekday":
                        return node("add", weekday, const(1), sort=INT)
                    return weekday
            function = self.callee(syntax.func)
            if function is len and len(syntax.args) == 1 and not syntax.keywords:
                operand = self.present(self.expression(syntax.args[0]))
                if operand.sort.kind == "str":
                    return node("len", operand, sort=INT)
            elif any(function is type_ for type_ in TIME_TYPES.values()):
                return self.time_constant(function, syntax)
            elif function is Decimal and len(syntax.args) == 1 and not syntax.keywords:
                argument = self.expression(syntax.args[0])  # Read exactly, as Python does.
                if argument.op == "const" and type(argument.value) in (int, str):
                    try:
                        return self.constant(Decimal(argument.value), syntax)
                    except decimal.InvalidOperation as exc:
                        raise self.unsupported(syntax, "invalid Decimal() constant") from exc
            elif function is abs and len(syntax.args) == 1 and not syntax.keywords:
                value = self.present(self.expression(syntax.args[0]))
                if value.sort.kind in ("int", "decimal"):
                    sort = INT
                    if value.sort.kind == "decimal":
                        scale = value.sort.scale
                        sort = self.exact(scale, self.digits(value, scale, syntax), syntax)
                    negated = node("neg", value, sort=sort)
                    return node("ite", node("ge", value, const(0)), value, negated, sort=sort)
            elif function in (min, max) and len(syntax.args) > 1 and not syntax.keywords:
                values = [self.present(self.expression(argument)) for argument in syntax.args]
                kind = values[0].sort.kind
                sort = _numbers(values)
                if sort is None and kind in _ORDERED:
                    if all(value.sort.kind == kind for value in values):
                        sort = Domain(kind)
                if sort is not None:
                    result = values[0]
                    for value in values[1:]:  # The first of equal values wins, as in Python.
                        better = node("lt" if function is min else "gt", value, result)
                        result = node("ite", better, value, result, sort=sort)
                    return result
        raise self.unsupported(syntax, f"unsupported expression {type(syntax).__name__}")

    def affix(self, method: str, text: ast.expr, argument: ast.expr, syntax: ast.AST) -> Expr:
        """text.startswith(argument) or text.endswith(argument), where the argument is a
        string or a literal tuple of them, any of which will do."""
        value = self.present(self.expression(text))  # None has no such method.
        options = argument.elts if isinstance(argument, ast.Tuple) else [argument]
        affixes = [self.present(self.expression(option)) for option in options]
        if any(part.sort.kind != "str" for part in (value, *affixes)):
            raise self.unsupported(syntax, f"{method}() needs strings")
        return _or(*(node(method, value, affix) for affix in affixes))

    def choice(self, condition: Expr, yes: Expr, no: Expr, syntax: ast.AST) -> Expr:
        """yes where condition holds, else no: two values of one supported type, or
        numbers that include a decimal."""
        mixed = _numbers((yes, no))
        if mixed is not None:
            return node("ite", condition, yes, no, sort=mixed)
        if yes.sort.kind != no.sort.kind or (
            yes.sort.kind == "enum" and self.enum_type(yes) is not self.enum_type(no)
        ):
            raise self.unsupported(syntax, "values chosen between must have the same type")
        if yes.sort.kind == "enum":
            enum_type = self.enum_type(yes)
            assert enum_type is not None
            sort = Domain("enum", tuple(enum_type))
        else:
            sort = Domain(yes.sort.kind)
        return node("ite", condition, yes, no, sort=sort)

    def member_attribute(self, member: Expr, attribute: str, syntax: ast.AST) -> Expr:
        """An Enum member's value or name, chosen among the members it can be."""
        choices = member.sort.choices or ()
        items = [getattr(choice, attribute) for choice in choices]
        sort = PRIMITIVES.get(type(items[0]))
        if sort is None or any(type(item) is not type(items[0]) for item in items):
            raise self.unsupported(syntax, f"Enum {attribute}s must share one supported type")
        if not all(encodable(item) for item in items):
            raise self.unsupported(syntax, "string constants above U+2FFFF are unsupported")
        if member.op == "const":
            return const(getattr(member.value, attribute))
        member = self.present(member)  # None has neither: Python raises AttributeError.
        result = const(items[-1])
        for choice, item in zip(choices[-2::-1], items[-2::-1], strict=True):
            result = node("ite", node("eq", member, const(choice)), const(item), result, sort=sort)
        return result

    def field_path(self, syntax: ast.expr) -> str:
        """The field an attribute chain reads: self.customer.age reads "customer.age"."""
        names = []
        while isinstance(syntax, ast.Attribute):
            names.append(syntax.attr)
            syntax = syntax.value
        if isinstance(syntax, ast.Name) and syntax.id == self.receiver:
            return ".".join(reversed(names))
        if isinstance(syntax, ast.Name) and syntax.id == self.value_name and self.field_name:
            return ".".join([self.field_name, *reversed(names)])  # A nested field's value.
        return ""

    def link(
        self, left: Expr, operation: ast.cmpop, right_syntax: ast.expr, syntax: ast.Compare
    ) -> tuple[Expr, Expr]:
        """One comparison of a chain: its test and the next left operand."""
        if not isinstance(operation, (ast.In, ast.NotIn)):
            right = self.expression(right_syntax)
            return self.compare(left, right, operation, syntax), right
        if len(syntax.ops) > 1:
            raise self.unsupported(syntax, "membership cannot participate in a chained comparison")
        if not isinstance(right_syntax, (ast.Tuple, ast.List, ast.Set)):  # A substring?
            right = self.present(self.expression(right_syntax))
            if left.sort.kind != "str" or right.sort.kind != "str":
                raise self.unsupported(
                    syntax, "membership needs a literal tuple/list/set or a string"
                )
            test = node("contains", right, self.present(left))
            return (_not(test) if isinstance(operation, ast.NotIn) else test), left
        members = [self.expression(member) for member in right_syntax.elts]
        if any(member.op != "const" for member in members):
            raise self.unsupported(
                syntax, "membership collections must contain only literal values"
            )
        test = _or(*(self.compare(left, member, ast.Eq(), syntax) for member in members))
        return (_not(test) if isinstance(operation, ast.NotIn) else test), left

    def callee(self, function: ast.expr) -> Any:
        """A call's target: a resolved name, or an attribute of the datetime or decimal
        module."""
        if isinstance(function, ast.Name):
            return self.resolve(function.id)
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            module = self.resolve(function.value.id)
            if module is datetime or module is decimal:
                return getattr(module, function.attr, None)
        return None

    def time_constant(self, constructor: type, syntax: ast.Call) -> Expr:
        """date(...), datetime(...) or timedelta(...) with integer literal arguments."""
        if any(keyword.arg is None for keyword in syntax.keywords):
            raise self.unsupported(syntax, f"{constructor.__name__}(**...) is unsupported")
        arguments = [self.expression(argument) for argument in syntax.args]
        keywords = {
            keyword.arg: self.expression(keyword.value)
            for keyword in syntax.keywords
            if keyword.arg is not None
        }
        values = (*arguments, *keywords.values())
        if any(value.op != "const" or type(value.value) is not int for value in values):
            raise self.unsupported(
                syntax, f"{constructor.__name__}() needs integer literal arguments"
            )
        try:
            value = constructor(
                *(argument.value for argument in arguments),
                **{name: argument.value for name, argument in keywords.items()},
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise self.unsupported(syntax, f"invalid {constructor.__name__}() constant") from exc
        return const(value)

    def compare(self, left: Expr, right: Expr, operation: ast.cmpop, syntax: ast.AST) -> Expr:
        if isinstance(operation, (ast.Is, ast.IsNot)):
            if "none" not in (left.sort.kind, right.sort.kind):
                raise self.unsupported(syntax, "identity comparisons are only supported with None")
            result = _and(self.is_none(left), self.is_none(right))
            return _not(result) if isinstance(operation, ast.IsNot) else result
        operators = {
            ast.Eq: "eq",
            ast.NotEq: "ne",
            ast.Lt: "lt",
            ast.LtE: "le",
            ast.Gt: "gt",
            ast.GtE: "ge",
        }
        opname = operators.get(type(operation))
        if opname is None:
            raise self.unsupported(syntax, "unsupported comparison")
        if opname in ("eq", "ne"):
            equal = self.equality(left, right, syntax)
            return _not(equal) if opname == "ne" else equal
        # Ordering raises TypeError for None, so it requires values.
        left, right = self.present(left), self.present(right)
        kinds = {left.sort.kind, right.sort.kind}
        if not kinds <= {"int", "bool", "decimal"} and kinds not in (
            {"date"},
            {"datetime"},
            {"time"},
            {"timedelta"},
        ):
            raise self.unsupported(
                syntax, "ordering needs numbers or two values of one date/time type"
            )
        return node(opname, left, right)

    def equality(self, left: Expr, right: Expr, syntax: ast.AST) -> Expr:
        left_none, right_none = self.is_none(left), self.is_none(right)
        kinds = {left.sort.kind, right.sort.kind}
        if "none" in kinds:
            return _and(left_none, right_none)
        if "enum" in kinds:
            if self.enum_type(left) is not self.enum_type(right):
                raise self.unsupported(
                    syntax, "Enum comparisons require equality with the same Enum type"
                )
            values_equal = node("eq", left, right)
        elif len(kinds) == 1 or kinds <= {"int", "bool", "decimal"}:  # Numbers by value.
            values_equal = node("eq", left, right)
        else:
            values_equal = const(False)  # Python's == across these types is always False.
        # None equals only None; otherwise the values decide.
        return _or(
            _and(left_none, right_none), _and(_not(left_none), _not(right_none), values_equal)
        )

    @staticmethod
    def enum_type(value: Expr) -> type[Enum] | None:
        if value.sort.kind == "enum" and value.sort.choices:
            return type(value.sort.choices[0])
        return None


def _numbers(values: Sequence[Expr]) -> Domain | None:
    """The sort of a value that is one of these, when they are decimals and integers:
    Python compares them by value, on the finest grid among them."""
    kinds = {value.sort.kind for value in values}
    if "decimal" in kinds and kinds <= {"int", "decimal"}:
        scale = max(value.sort.scale for value in values)
        digits = [_digits(value, scale) for value in values]
        known = [count for count in digits if count is not None]
        return Domain("decimal", scale=scale, digits=max(known) if known == digits else None)
    return None


def _digits(value: Expr, scale: int) -> int | None:
    """At most how many digits a number has in units of 10**-scale, or None when it is
    unbounded, as an int field is."""
    if value.sort.kind == "bool":
        return scale + 1
    if value.sort.kind == "int":
        return len(str(abs(value.value))) + scale if value.op == "const" else None
    return None if value.sort.digits is None else value.sort.digits + scale - value.sort.scale


def extract_validator(
    function: Callable[..., Any],
    fields: Mapping[str, Domain],
    *,
    kind: Literal["model", "dataclass", "field"] = "model",
    field_name: str | None = None,
    earlier_fields: Collection[str] = (),
    constants: Mapping[str, Any] | None = None,
) -> Expr:
    """Return the complete acceptance predicate or reject the entire validator.

    ``fields`` contains validated, canonical Python value types. ``earlier_fields``
    must follow Pydantic's declaration order for ``ValidationInfo.data`` reads.
    ``constants`` are the class's ClassVars, which ``self.NAME`` or ``cls.NAME`` read.
    """
    analyzer = _Analyzer(function, fields, kind, field_name, earlier_fields, constants or {})
    return analyzer.analyze()
