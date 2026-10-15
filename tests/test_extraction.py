"""Compare the analyzer's complete acceptance predicates to Python execution."""

from __future__ import annotations

import datetime as dt
import inspect
import itertools
from datetime import date, datetime, timedelta
from enum import Enum, IntEnum
from types import SimpleNamespace

import pytest

from modelfill.backend import Solver
from modelfill.errors import UnsupportedConstraint
from modelfill.extract import extract_validator
from modelfill.ir import BOOL, INT, STR, Domain, evaluate


def assert_matches_python(validator, fields, grid):
    """Python, the extracted predicate and the solver agree on every input."""
    expression = extract_validator(validator, fields)
    solver = Solver(fields, (expression,), timeout_ms=1000, seed=0)
    for combination in itertools.product(*grid.values()):
        values = dict(zip(grid, combination, strict=True))
        try:
            validator(SimpleNamespace(**values))
            accepted = True
        except (ValueError, AssertionError, TypeError):  # TypeError: an operation on None.
            accepted = False
        assert evaluate(expression, values) is accepted, (validator.__name__, values)
        with solver.session(values):
            assert solver.check() is accepted, (validator.__name__, values)


def conditional_membership(self):
    if self.a == 1 and self.b not in (2, 4):
        raise ValueError("invalid")
    return self


def branches(self):
    if self.a > 0:
        if self.b != self.a + 1:
            raise ValueError("bad positive")
    elif self.b != self.a - 2:
        raise ValueError("bad nonpositive")
    return self


def early_returns(self):
    if self.a == 2:
        return self
    if self.b == 4:
        if self.a > 0:
            return self
        raise ValueError("bad b")
    if self.a != self.b:
        raise ValueError("must match")
    return self


def branching_returns(self):
    if self.a:
        if self.b:
            return self
        raise ValueError("b required")
    else:
        if self.b != -1:
            raise ValueError("invalid")
        return self


def precedence(self):
    if not self.a == 2 and self.b or self.a == -1:
        raise ValueError("invalid")
    return self


def operand_values(self):
    if (self.a or self.b) != 2:
        raise ValueError("invalid")
    if (self.a and self.b) > 3:
        raise ValueError("invalid")
    return self


def arithmetic(self):
    if -self.a + +self.b != 2 * self.a - self.b * -3:
        raise ValueError("invalid")
    return self


def chained(self):
    if not -1 <= self.a < self.b <= 4:
        raise ValueError("invalid")
    return self


def overlapping_paths(self):
    if self.a:
        if self.b > 2:
            return self
    if self.b == self.a:
        return self
    raise ValueError("invalid")


def local_variables(self):
    total = self.a + self.b
    gap: int = self.b - self.a
    if total > 4 or gap == 3:
        raise ValueError(f"total {total}, gap {gap}")
    return self


def asserts(self):
    assert self.a < self.b, f"{self.a} must be below {self.b}"
    if self.a == 0:
        raise ValueError("zero")
    return self


LIMIT = 3


def modulo_and_constants(self):
    if self.a % 3 == 1 or self.b // 2 > self.a or self.b > LIMIT:
        raise ValueError("invalid")
    return self


@pytest.mark.parametrize(
    "validator",
    [
        conditional_membership,
        branches,
        early_returns,
        branching_returns,
        precedence,
        operand_values,
        arithmetic,
        chained,
        overlapping_paths,
        local_variables,
        asserts,
        modulo_and_constants,
    ],
)
def test_extraction_and_solver_match_python_exhaustively(validator):
    assert_matches_python(validator, {"a": INT, "b": INT}, {"a": range(-4, 6), "b": range(-4, 6)})


def optional_narrowing(self):
    if self.a is not None and self.a < self.b:
        raise ValueError("a below b")
    return self


def optional_values(self):
    if not self.a or self.a == self.b or self.a in (3, None) or (self.a or 0) > 4:
        raise ValueError("invalid")
    return self


def optional_unguarded(self):
    if self.b > 1 or self.a + self.b > 2:  # With a None, this raises when b <= 1.
        raise ValueError("too big")
    return self


def optional_chain(self):
    if self.b < 0 or -1 <= self.a <= self.b:
        return self
    raise ValueError("outside")


def date_rules(self):
    if self.b - self.a > timedelta(days=2) or self.b < self.a:
        raise ValueError("span")
    if self.a + timedelta(hours=36) == self.b:  # Python adds whole days only.
        raise ValueError("next day")
    return self


def schedule_rules(self):
    end = self.start + self.length
    if end > datetime(2025, 1, 1, 12) or not self.length or -self.length > timedelta(0):
        raise ValueError("invalid")
    return self


def module_constants(self):
    if not self.a or self.a < dt.date(2025, 1, 2) or self.b - self.a > dt.timedelta(days=1):
        raise ValueError("invalid")
    return self


def boolean_numbers(self):
    if self.a == self.b and self.a + self.b > 1:
        raise ValueError("invalid")
    return self


def text_conditions(self):
    if not self.a or len(self.a) < 2 or (self.a or self.b) != "ok":
        raise ValueError("invalid")
    return self


class Color(Enum):
    RED = "red"
    BLUE = "blue"


class Number(IntEnum):
    ZERO = 0
    ONE = 1


def enum_validation(self):
    if self.a not in (Color.RED, Color.BLUE):
        raise ValueError("invalid")
    if not self.b:
        raise ValueError("invalid number")
    return self


def literal_subsets(self):
    if self.a != Color.BLUE or self.b != self.a:
        raise ValueError("invalid")
    return self


def enum_operands(self):
    if ((self.a or self.b) and self.c) != Number.ZERO:
        raise ValueError("invalid")
    return self


def signed_membership(self):
    if self.a not in (-3, +2, None, "2"):
        raise ValueError("invalid")
    return self


# With int fields, `self.a is not None` always holds, so every input is rejected.
def none_validation(self):
    if self.a is not None or self.b is None:
        raise ValueError("invalid")
    return self


def always_reject(self):
    raise ValueError


def unreachable_code(self):
    return self
    print("cannot run")  # pragma: no cover


MAYBE = {"a": Domain("int", nullable=True), "b": INT}
MAYBE_GRID = {"a": [None, *range(-2, 5)], "b": range(-2, 5)}
DAYS = [date(2025, 1, 1) + timedelta(days=offset) for offset in range(4)]
SCHEDULE = {"start": Domain("datetime"), "length": Domain("timedelta")}
SCHEDULE_GRID = {
    "start": [datetime(2025, 1, 1, hour) for hour in (9, 10, 11)],
    "length": [timedelta(minutes=minutes) for minutes in (-30, 0, 30, 90)],
}
TEXTS = ("", "x", "ok", "no", "😀")
COLORS = Domain("enum", tuple(Color))
NUMBERS = Domain("enum", tuple(Number))
# Literal subsets must still encode members by their position in the whole Enum.
BLUE_ONLY = {"a": Domain("enum", (Color.BLUE,)), "b": COLORS}
ZERO_FIRST = {"a": Domain("enum", (Number.ZERO,)), "b": NUMBERS, "c": NUMBERS}


@pytest.mark.parametrize(
    ("validator", "fields", "grid"),
    [
        (optional_narrowing, MAYBE, MAYBE_GRID),
        (optional_values, MAYBE, MAYBE_GRID),
        (optional_unguarded, MAYBE, MAYBE_GRID),
        (optional_chain, MAYBE, MAYBE_GRID),
        (date_rules, {"a": Domain("date"), "b": Domain("date")}, {"a": DAYS, "b": DAYS}),
        (module_constants, {"a": Domain("date"), "b": Domain("date")}, {"a": DAYS, "b": DAYS}),
        (schedule_rules, SCHEDULE, SCHEDULE_GRID),
        (boolean_numbers, {"a": BOOL, "b": INT}, {"a": (False, True), "b": range(-1, 3)}),
        (text_conditions, {"a": STR, "b": STR}, {"a": TEXTS, "b": TEXTS}),
        (enum_validation, {"a": COLORS, "b": NUMBERS}, {"a": Color, "b": Number}),
        (literal_subsets, BLUE_ONLY, {"a": (Color.BLUE,), "b": Color}),
        (enum_operands, ZERO_FIRST, {"a": (Number.ZERO,), "b": Number, "c": Number}),
        (signed_membership, {"a": INT}, {"a": range(-4, 4)}),
        (none_validation, {"a": INT, "b": INT}, {"a": range(2), "b": range(2)}),
        (always_reject, {}, {}),
        (unreachable_code, {}, {}),
    ],
)
def test_typed_fields_match_python_exhaustively(validator, fields, grid):
    assert_matches_python(validator, fields, grid)


def dataclass_validation(self):
    if self.a:
        return
    if self.b != 2:
        raise ValueError("invalid")


def test_dataclass_implicit_and_explicit_returns():
    expression = extract_validator(dataclass_validation, {"a": INT, "b": INT}, kind="dataclass")
    for a, b in itertools.product(range(-1, 3), repeat=2):
        assert evaluate(expression, {"a": a, "b": b}) == (bool(a) or b == 2)


class FieldValidators:
    @classmethod
    def earlier(cls, value, info):
        if value <= info.data["a"]:
            raise ValueError("invalid")
        return value


def test_field_validator_reads_only_previous_declared_fields():
    expression = extract_validator(
        FieldValidators.earlier,
        {"a": INT, "b": INT},
        kind="field",
        field_name="b",
        earlier_fields=("a",),
    )
    for a, b in itertools.product(range(-1, 3), repeat=2):
        assert evaluate(expression, {"a": a, "b": b}) == (b > a)
    with pytest.raises(UnsupportedConstraint, match="earlier_field"):
        extract_validator(
            FieldValidators.earlier, {"a": INT, "b": INT}, kind="field", field_name="b"
        )


def rebinds_self(self):
    self = None
    return self


def branch_assignment(self):
    total = self.a
    if self.b:
        total = self.b
    if total > 2:
        raise ValueError("invalid")
    return self


def mutation(self):
    self.a += 1
    return self


def loop(self):
    for item in [self.a]:
        if item:
            raise ValueError("invalid")
    return self


def call(self):
    if abs(self.a) > 2:
        raise ValueError("invalid")
    return self


def dynamic_multiplication(self):
    if self.a * self.b > 2:
        raise ValueError("invalid")
    return self


def variable_modulo(self):
    if self.a % self.b:
        raise ValueError("invalid")
    return self


def negative_modulo(self):  # Python and Z3 disagree for negative divisors.
    if self.a % -3:
        raise ValueError("invalid")
    return self


def unsafe_return(self):
    return 4


def missing_return(self):
    if self.a:
        return self


def identity_number(self):
    if self.a is self.b:
        raise ValueError("invalid")
    return self


def nonliteral_membership(self):
    if self.a in (self.b, 4):
        raise ValueError("invalid")
    return self


def mixed_boolean_values(self):
    if (self.a or "x") == self.b:
        raise ValueError("invalid")
    return self


def try_statement(self):
    try:
        return self
    except ValueError:
        raise


def unsupported_unicode(self):
    if self.a == "\U0010ffff":
        raise ValueError("invalid")
    return self


@pytest.mark.parametrize(
    "validator",
    [
        rebinds_self,
        branch_assignment,
        mutation,
        loop,
        call,
        dynamic_multiplication,
        variable_modulo,
        negative_modulo,
        unsafe_return,
        missing_return,
        identity_number,
        nonliteral_membership,
        mixed_boolean_values,
        try_statement,
        unsupported_unicode,
    ],
)
def test_entire_unsupported_validator_is_rejected(validator):
    with pytest.raises(UnsupportedConstraint):
        extract_validator(validator, {"a": INT, "b": INT})


def test_source_unavailable_is_reported(monkeypatch):
    def unavailable(_):
        raise OSError("no source")

    monkeypatch.setattr(inspect, "getsourcelines", unavailable)
    with pytest.raises(UnsupportedConstraint, match="source is unavailable"):
        extract_validator(conditional_membership, {"a": INT, "b": INT})


def test_unsupported_reasons_point_at_file_lines():
    if_line = inspect.getsourcelines(call)[1] + 1
    with pytest.raises(UnsupportedConstraint, match=f"call at line {if_line}: "):
        extract_validator(call, {"a": INT, "b": INT})


def test_shadowed_len_is_unsupported():
    len = lambda value: 100  # noqa: E731

    def validator(self):
        if len(self.a) > 4:
            raise ValueError("invalid")
        return self

    with pytest.raises(UnsupportedConstraint, match="Call"):
        extract_validator(validator, {"a": STR})


def test_shadowed_value_error_is_unsupported():
    ValueError = RuntimeError

    def validator(self):
        if self.a:
            raise ValueError("invalid")
        return self

    with pytest.raises(UnsupportedConstraint, match="builtin ValueError"):
        extract_validator(validator, {"a": INT})


def test_function_parameters_shadow_global_builtins():
    def validator(self, len):
        if len(self.a) > 4:
            raise ValueError("invalid")
        return self

    with pytest.raises(UnsupportedConstraint, match="Call"):
        extract_validator(validator, {"a": STR})


def test_unrelated_int_enums_are_not_conflated():
    class OtherNumber(IntEnum):
        ZERO = 0
        ONE = 1

    def validator(self):
        if self.a != OtherNumber.ONE:
            raise ValueError("invalid")
        return self

    with pytest.raises(UnsupportedConstraint, match="same Enum type"):
        extract_validator(validator, {"a": NUMBERS})


def test_external_enum_constants_cannot_run_custom_semantics():
    class Dynamic(Enum):
        MEMBER = 1

        def __bool__(self):
            pytest.fail("Enum custom semantics executed during analysis")

    def validator(self):
        if Dynamic.MEMBER:
            raise ValueError("invalid")
        return self

    with pytest.raises(UnsupportedConstraint, match="standard semantics"):
        extract_validator(validator, {})
