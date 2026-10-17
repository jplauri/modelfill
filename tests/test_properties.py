"""Generated inputs, checked against oracles independent of modelfill.

The oracles are Pydantic, plain Python execution and brute force. Example counts
and randomness come from the Hypothesis profiles in conftest.py.
"""

import itertools
import linecache
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Annotated

import pytest
from annotated_types import Ge, Gt, Le, Lt, MaxLen, MinLen, MultipleOf
from hypothesis import assume, given
from hypothesis import strategies as st
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, create_model, model_validator

from examples.meeting import Meeting
from modelfill import ModelFiller, UnsatisfiableConstraints, UnsupportedConstraint, fill
from modelfill.backend import Solver
from modelfill.extract import extract_validator
from modelfill.ir import INT, decode, encode
from modelfill.patterns import Pattern

# Z3 strings cover U+0000 to U+2FFFF; surrogates are not text.
TEXT = st.text(
    st.characters(max_codepoint=0x2FFFF).filter(lambda char: not 0xD800 <= ord(char) <= 0xDFFF),
    max_size=12,
)


@given(
    st.one_of(
        st.tuples(st.dates(), st.dates()),
        st.tuples(st.datetimes(), st.datetimes()),
        st.tuples(st.times(), st.times()),
        st.tuples(st.timedeltas(), st.timedeltas()),
    )
)
def test_time_encodings_round_trip_and_keep_order(pair):
    first, second = pair
    kind = {date: "date", datetime: "datetime", time: "time", timedelta: "timedelta"}[type(first)]
    assert decode(kind, encode(first)) == first
    assert (encode(first) < encode(second)) == (first < second)


class Measured(BaseModel):
    text: str
    size: int

    @model_validator(mode="after")
    def check(self):
        if len(self.text) != self.size:
            raise ValueError("size")
        return self


@given(TEXT)
def test_solver_string_lengths_match_python(text):
    assert fill(Measured, text=text).size == len(text)


class Mirror(BaseModel):
    original: str
    duplicate: str

    @model_validator(mode="after")
    def check(self):
        if self.duplicate != self.original:
            raise ValueError("duplicate")
        return self


@given(TEXT, st.integers(0, 2**32 - 1))
def test_the_sampler_decodes_solver_strings_exactly(text, seed):
    # from_model's sampler reads every value back from the solver's model.
    mirror = ModelFiller(Mirror)._complete({"original": text}, 1, random.Random(seed))[0]
    assert mirror.duplicate == text


SMALL = st.integers(-8, 8)
LENGTHS = st.integers(0, 6)
METADATA = {  # Annotated items, Field() arguments and values to try, per base type.
    int: (
        st.one_of(
            SMALL.map(Ge),
            SMALL.map(Gt),
            SMALL.map(Le),
            SMALL.map(Lt),
            st.integers(1, 5).map(MultipleOf),
        ),
        st.dictionaries(st.sampled_from(["ge", "gt", "le", "lt"]), SMALL, max_size=2),
        st.integers(-10, 10),
    ),
    str: (
        st.one_of(LENGTHS.map(MinLen), LENGTHS.map(MaxLen)),
        st.dictionaries(st.sampled_from(["min_length", "max_length"]), LENGTHS, max_size=2),
        st.text("ab", max_size=8),
    ),
}


@given(st.data())
def test_metadata_combines_as_in_pydantic(data):
    base = data.draw(st.sampled_from([int, str]))
    annotated, field, values = METADATA[base]
    items = data.draw(st.lists(annotated, min_size=1, max_size=4))
    model = create_model("Combined", value=(Annotated[base, *items], Field(**data.draw(field))))
    value = data.draw(values)
    filler = ModelFiller(model, mode="strict")
    try:
        expected = model(value=value)
    except ValidationError:
        with pytest.raises(UnsatisfiableConstraints):
            filler.fill(value=value)
    else:
        assert filler.fill(value=value) == expected


NUMBERS = st.one_of(  # Bounds as Pydantic takes them: a float reads as its repr.
    st.integers(-5, 5), st.decimals(-5, 5, places=3), st.floats(-5, 5).map(lambda x: round(x, 2))
)


@given(st.data())
def test_decimal_constraints_match_pydantic(data):
    digits = data.draw(st.none() | st.integers(1, 4))
    places = data.draw(st.integers(0, 3))
    steps = st.sampled_from([Decimal("0.05"), Decimal("0.3"), Decimal("1.5"), 0.1, 2])
    items = data.draw(
        st.lists(
            st.one_of(*(NUMBERS.map(kind) for kind in (Ge, Gt, Le, Lt)), steps.map(MultipleOf)),
            min_size=1,
            max_size=3,
        )
    )
    bounds = data.draw(st.dictionaries(st.sampled_from(["ge", "gt", "le", "lt"]), NUMBERS))
    field = Field(max_digits=digits, decimal_places=places, **bounds)
    model = create_model("Priced", value=(Annotated[Decimal, *items], field))
    grid = places if digits is None else min(places, digits)  # Every value Pydantic takes.
    value = data.draw(st.decimals(-12, 12, places=grid))
    filler = ModelFiller(model, mode="strict")
    try:
        expected = model(value=value)
    except ValidationError:
        with pytest.raises(UnsatisfiableConstraints):
            filler.fill(value=value)
    else:
        assert filler.fill(value=value) == expected
    try:  # The canonical value, when there is one, is valid: fill() validates it.
        filler.fill()
    except UnsatisfiableConstraints:
        pass


@given(st.dictionaries(st.sampled_from(["start", "end", "duration"]), st.integers(-1, 20)))
def test_canonical_meetings_are_stable_and_impossibility_is_real(partial):
    try:
        meeting = fill(Meeting, **partial)
    except UnsatisfiableConstraints:
        # Every valid meeting lies in this range, so brute force is a complete check.
        for start, end, duration in itertools.product(range(-1, 21), repeat=3):
            values = {"start": start, "end": end, "duration": duration}
            if values.items() >= partial.items():
                with pytest.raises(ValidationError):
                    Meeting(**values)
        return
    for name, value in meeting.model_dump().items():  # A field fixed to its value changes nothing.
        assert fill(Meeting, **(partial | {name: value})) == meeting
    assert ModelFiller(Meeting).batch(1, **partial) == [meeting]


# Random validators over two integer reads, within the supported grammar.
def validators(first: str, second: str) -> st.SearchStrategy[list[str]]:
    atoms = st.sampled_from([first, second]) | st.integers(-4, 4).map(str)
    terms = st.recursive(
        atoms,
        lambda inner: st.one_of(
            st.tuples(inner, st.sampled_from(["+", "-"]), inner).map(
                lambda t: f"({t[0]} {t[1]} {t[2]})"
            ),
            st.tuples(st.integers(-3, 3), inner).map(lambda t: f"({t[0]} * {t[1]})"),
            st.tuples(inner, st.sampled_from(["%", "//"]), st.integers(1, 4)).map(
                lambda t: f"({t[0]} {t[1]} {t[2]})"
            ),
            inner.map(lambda term: f"(-{term})"),
        ),
        max_leaves=4,
    )
    operators = st.sampled_from(["==", "!=", "<", "<=", ">", ">="])
    conditions = st.recursive(
        st.one_of(
            terms,  # Tested for truth.
            st.tuples(terms, operators, terms).map(" ".join),
            st.tuples(terms, operators, terms, operators, terms).map(" ".join),  # Chained.
        ),
        lambda inner: st.one_of(
            inner.map(lambda condition: f"not ({condition})"),
            st.tuples(inner, st.sampled_from(["and", "or"]), inner).map(
                lambda t: f"({t[0]}) {t[1]} ({t[2]})"
            ),
        ),
        max_leaves=3,
    )
    statements = st.one_of(
        conditions.map(lambda c: f"    if {c}:\n        raise ValueError('rejected')"),
        conditions.map(lambda c: f"    if {c}:\n        return self"),
        conditions.map(lambda c: f"    assert {c}"),
        st.tuples(conditions, conditions).map(
            lambda t: (
                f"    if {t[0]}:\n        if {t[1]}:\n            raise ValueError('rejected')\n"
                f"    elif {t[1]}:\n        return self"
            )
        ),
    )
    return st.lists(statements, min_size=1, max_size=3)


SHAPES = {  # The two reads, the solver fields they become, and an object Python validates.
    "flat": (validators("self.a", "self.b"), ("a", "b"), lambda a, b: SimpleNamespace(a=a, b=b)),
    "nested": (
        validators("self.inner.a", "self.b"),
        ("inner.a", "b"),
        lambda a, b: SimpleNamespace(inner=SimpleNamespace(a=a), b=b),
    ),
}
NAMES = itertools.count()


def define(source):
    """Define a function whose source inspect can retrieve, as extraction requires."""
    filename = f"<generated validator {next(NAMES)}>"
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    namespace = {}
    exec(compile(source, filename, "exec"), namespace)
    return namespace["check"]


@pytest.mark.parametrize("shape", SHAPES)
@given(data=st.data())
def test_generated_validators_are_extracted_exactly_or_refused(shape, data):
    strategy, names, build = SHAPES[shape]
    statements = data.draw(strategy)
    validator = define("def check(self):\n" + "\n".join(statements) + "\n    return self\n")
    fields = dict.fromkeys(names, INT)
    try:
        expression = extract_validator(validator, fields)
    except UnsupportedConstraint:
        assume(False)  # Refusing is sound; only what is extracted must be exact.
    solver = Solver(fields, (expression,), timeout_ms=1000, work_limit=10**6)
    for a, b in itertools.product(range(-5, 6), repeat=2):
        try:
            validator(build(a, b))
            accepted = True
        except (ValueError, AssertionError):
            accepted = False
        with solver.session(dict(zip(names, (a, b), strict=True))):
            assert solver.check() is accepted, (statements, a, b)


# Pattern pieces with the same meaning in Python's syntax and Rust's, which Pydantic uses.
ATOMS = ["a", "Z", "7", "-", "@", r"\.", ".", "[a-z]", "[A-Z0-9]", "[^a]", "[^@\\s]"]
ATOMS += [r"\d", r"\D", r"\s", r"\w", r"\W", "[a-f0-9_]"]
QUANTIFIERS = ["", "", "*", "+", "?", "{2}", "{1,3}", "{0,2}"]
PIECES = st.tuples(st.sampled_from(ATOMS), st.sampled_from(QUANTIFIERS)).map("".join)


def anchored(inner: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    """Alternatives, each perhaps anchored at either end."""
    alternative = st.tuples(st.sampled_from(["", "^"]), inner, st.sampled_from(["", "$"]))
    return st.lists(alternative.map("".join), min_size=1, max_size=3).map("|".join)


def grouped(inner: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    parts = st.tuples(
        st.sampled_from(["(", "(?:", "(?i:", "(?-i:"]),
        anchored(inner),
        st.sampled_from(QUANTIFIERS),
    )
    return parts.map(lambda part: f"{part[0]}{part[1]}){part[2]}")


BODIES = st.recursive(
    st.lists(PIECES, min_size=1, max_size=4).map("".join),
    lambda inner: st.lists(st.one_of(inner, grouped(inner)), min_size=1, max_size=3).map("".join),
    max_leaves=6,
)
REGEXES = st.tuples(st.sampled_from(["", "(?i)"]), anchored(BODIES)).map("".join)


@given(REGEXES, st.sampled_from(["code", "zip", "e"]), st.integers(0, 2**32 - 1))
def test_spelled_and_sampled_values_match_as_pydantic_says(regex, name, seed):
    metadata = tuple(Field(pattern=regex).metadata)
    oracle = TypeAdapter(Annotated[str, *metadata])
    pattern = Pattern(regex, None)
    assert pattern.automaton is not None  # Every generated construct is read.
    values = list(itertools.islice(pattern.spell(name, lambda size: True, 0), 3))
    values.append(pattern.sample(random.Random(seed)))
    for value in values:
        if value is not None:
            assert oracle.validate_python(value) == value, (regex, value)
