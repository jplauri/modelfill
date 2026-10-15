"""Public API acceptance and Python-versus-solver regression tests."""

import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from itertools import product
from typing import ClassVar, Literal
from uuid import UUID, uuid4

import pytest
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from examples.meeting import Meeting
from modelfill import (
    GenerationExhausted,
    ModelFiller,
    UnsatisfiableConstraints,
    UnsupportedConstraint,
    fill,
)


@pytest.mark.parametrize(
    ("fixed", "expected"),
    [
        ({"end": 18, "duration": 4}, {"start": 14}),
        ({"start": 10, "end": 13}, {"duration": 3}),
        ({"start": 10, "duration": 2}, {"end": 12}),
    ],
)
def test_bidirectional_completion(fixed, expected):
    meeting = fill(Meeting, **fixed)
    assert meeting.model_dump() == fixed | expected


def test_unconstrained_completion_and_reusable_filler():
    filler = ModelFiller(Meeting, mode="strict")
    for meeting in [fill(Meeting), *filler.batch(15)]:
        assert meeting.end == meeting.start + meeting.duration
        assert Meeting.model_validate(meeting.model_dump()) == meeting
    assert filler.fill(end=18, duration=4).start == 14
    assert filler.fill(start=10, end=13).duration == 3


def test_impossible_partial_assignment_is_proven_and_explained():
    with pytest.raises(UnsatisfiableConstraints) as raised:
        fill(Meeting, start=17, duration=4)
    for part in ("start=17", "duration=4", "end <= 18", "Meeting.check"):
        assert part in str(raised.value)


class Pair(BaseModel):
    a: int
    b: int

    @model_validator(mode="after")
    def check(self):
        if self.a == 1 and self.b not in (2, 4):
            raise ValueError("Invalid")
        return self


def test_conditional_membership():
    filler = ModelFiller(Pair, mode="strict")
    assert fill(Pair, a=1).b == 2
    assert [pair.b for pair in filler.batch(2, a=1)] == [2, 4]


class Coupled(BaseModel):
    a: int = Field(ge=-20, le=20)
    b: int = Field(ge=-20, le=20)
    c: int = Field(ge=-20, le=20)

    @model_validator(mode="after")
    def check(self):
        if self.a + self.b != 11:
            raise ValueError("sum")
        if self.b - self.c != 2:
            raise ValueError("difference")
        if self.a != 2 * self.c:
            raise ValueError("double")
        return self


def test_multiple_coupled_constraints():
    assert fill(Coupled).model_dump() == {"a": 6, "b": 5, "c": 3}


class Contradiction(BaseModel):
    number: int

    @model_validator(mode="after")
    def check(self):
        if self.number < 10:
            raise ValueError("too small")
        if self.number > 9:
            raise ValueError("too big")
        return self


def test_contradictory_validators():
    with pytest.raises(UnsatisfiableConstraints):
        fill(Contradiction)


@dataclass
class DataclassMeeting:
    start: int
    end: int
    duration: int = 2

    def __post_init__(self):
        if not 8 <= self.start <= 17:
            raise ValueError("start")
        if not 9 <= self.end <= 18:
            raise ValueError("end")
        if not 1 <= self.duration <= 4:
            raise ValueError("duration")
        if self.end != self.start + self.duration:
            raise ValueError("relationship")


def test_dataclass_relationship_and_default():
    assert fill(DataclassMeeting, end=18, duration=4) == DataclassMeeting(14, 18, 4)
    assert fill(DataclassMeeting, start=10, end=13).duration == 3
    assert fill(DataclassMeeting, start=10).duration == 2
    with pytest.raises(UnsatisfiableConstraints):
        fill(DataclassMeeting, start=17, duration=4)


class CallValidator(BaseModel):
    value: int = Field(ge=0, le=10)

    @model_validator(mode="after")
    def check(self):
        if pow(self.value, 1, 2):  # Calls are unsupported, so this model falls back.
            raise ValueError("must be even")
        return self


def test_unsupported_validator_strict():
    with pytest.raises(UnsupportedConstraint):
        ModelFiller(CallValidator, mode="strict")


def test_fallback_disclosed_and_original_validation_runs():
    filler = ModelFiller(CallValidator, mode="auto")
    assert filler.explain()["unsupported"]
    values = filler.batch(6)  # Odd candidates are rejected by validation and blocked.
    assert [value.value for value in values] == [0, 2, 4, 6, 8, 10]
    for value in values:
        assert CallValidator.model_validate(value.model_dump()) == value


def test_failed_fallback_is_not_unsat():
    with pytest.raises(GenerationExhausted):
        ModelFiller(CallValidator, mode="auto", max_attempts=3).fill(value=3)


class GuidedFallback(BaseModel):
    x: int
    y: int

    @model_validator(mode="after")
    def relation(self):
        if self.y != self.x + 5:
            raise ValueError("relation")
        return self

    @model_validator(mode="after")
    def parity(self):
        if pow(self.x, 1, 2):
            raise ValueError("x must be even")
        return self


def test_supported_validators_guide_fallback_search():
    filler = ModelFiller(GuidedFallback)
    statuses = {item["name"]: item["status"] for item in filler.explain()["validators"]}
    assert statuses == {"relation": "guidance", "parity": "unsupported"}
    assert filler.fill(x=1000).y == 1005  # Far outside the random sampling window.


def test_fixed_inputs_are_canonical_and_never_coerced():
    with pytest.raises(TypeError):
        fill(Meeting, start="10")
    with pytest.raises(TypeError):
        fill(Meeting, start=True)
    with pytest.raises(ValueError):
        fill(Meeting, missing=10)
    assert fill(Meeting, start=10).start == 10


class Branches(BaseModel):
    a: int
    b: int

    @model_validator(mode="after")
    def check(self):
        if self.a == 0:
            return self
        if self.a > 0:
            if self.b not in (2, 4):
                raise ValueError("positive branch")
        elif self.b != -2:
            raise ValueError("negative branch")
        if self.a + self.b == 5:
            raise ValueError("late rejection")
        return self


class BooleanOperands(BaseModel):
    a: int
    b: int
    enabled: bool

    @model_validator(mode="after")
    def check(self):
        if (self.a or self.b) != 2:
            raise ValueError("or returns its operand")
        if (self.a and self.b) != 0:
            raise ValueError("and returns its operand")
        if not self.enabled or self.a < 0 and self.b > 0:
            raise ValueError("precedence")
        return self


class LargeBounds(BaseModel):
    low: int = Field(gt=10**30)
    high: int = Field(lt=-(10**30))

    @model_validator(mode="after")
    def check(self):
        if self.low != -self.high:
            raise ValueError("opposites")
        return self


def test_heuristic_bounds_do_not_create_false_unsat():
    value = fill(LargeBounds)
    assert value.low > 10**30
    assert value.high < -(10**30)
    fixed = 10**100
    assert fill(LargeBounds, low=fixed).high == -fixed


class Color(Enum):
    RED = "red"
    BLUE = "blue"


class BasicTypes(BaseModel):
    active: bool
    color: Color
    state: Literal["on", "off"]
    code: str = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def check(self):
        if self.active and self.state != "on":
            raise ValueError("state")
        if self.color == Color.RED and len(self.code) != 3:
            raise ValueError("code")
        return self


def test_basic_types_and_string_lengths():
    filler = ModelFiller(BasicTypes, mode="strict")
    for value in filler.batch(6, active=True, color=Color.RED):
        assert value.active is True
        assert value.color is Color.RED
        assert value.state == "on"
        assert len(value.code) == 3
    with pytest.raises(TypeError):
        filler.fill(color="red")


class FieldDependency(BaseModel):
    lower: int = Field(ge=1, le=20)
    upper: int = Field(ge=1, le=20)

    @field_validator("upper", mode="after")
    @classmethod
    def upper_exceeds_lower(cls, value: int, info: ValidationInfo):
        if value <= info.data["lower"]:
            raise ValueError("upper must exceed lower")
        return value


def test_field_validator_earlier_field_reads():
    filler = ModelFiller(FieldDependency, mode="strict")
    assert filler.fill(lower=19).upper == 20
    assert filler.fill(upper=2).lower == 1
    with pytest.raises(UnsatisfiableConstraints):
        filler.fill(lower=20)


def test_shared_filler_thread_safety():
    filler = ModelFiller(Meeting)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda start: filler.fill(start=start), range(8, 17)))
    assert [result.start for result in results] == list(range(8, 17))
    assert all(value.end == value.start + value.duration for value in results)


def test_original_model_is_not_modified():
    fields_before = dict(Meeting.model_fields)
    validator_before = Meeting.check
    fill(Meeting)
    assert Meeting.model_fields == fields_before
    assert Meeting.check is validator_before


def test_empty_and_fixed_batch():
    filler = ModelFiller(Meeting)
    assert filler.batch(0) == []
    assert all(value.start == 10 for value in filler.batch(4, start=10))
    with pytest.raises(ValueError):
        filler.batch(-1)


class LoopValidator(BaseModel):
    value: int

    @model_validator(mode="after")
    def check(self):
        for forbidden in (1, 3):
            if self.value == forbidden:
                raise ValueError("forbidden")
        return self


class MethodCallValidator(BaseModel):
    value: str

    @model_validator(mode="after")
    def check(self):
        if not self.value.isalpha():
            raise ValueError("alphabetical only")
        return self


class MutatingValidator(BaseModel):
    value: int

    @model_validator(mode="after")
    def check(self):
        self.value = 10
        return self


class BeforeValidator(BaseModel):
    value: int

    @model_validator(mode="before")
    @classmethod
    def check(cls, data):
        return data


@pytest.mark.parametrize(
    "model", [LoopValidator, MethodCallValidator, MutatingValidator, BeforeValidator]
)
def test_unsupported_operations_are_rejected_and_explained(model):
    with pytest.raises(UnsupportedConstraint):
        ModelFiller(model, mode="strict")
    assert ModelFiller(model, mode="auto").explain()["unsupported"]


def test_mutation_never_silently_changes_a_fixed_value():
    with pytest.raises(GenerationExhausted):
        ModelFiller(MutatingValidator, mode="auto", max_attempts=3).fill(value=5)


def test_source_unavailable_is_explicit():
    class UnavailableSource(BaseModel):
        value: int
        check = model_validator(mode="after")(eval("lambda self: self"))

    with pytest.raises(UnsupportedConstraint):
        ModelFiller(UnavailableSource, mode="strict")
    assert ModelFiller(UnavailableSource, mode="auto").explain()["unsupported"]


class PlainString(BaseModel):
    text: str


# Z3's string range without surrogates; seeded samples complement the escape cases.
CODE_POINTS = [*range(0xD800), *range(0xE000, 0x30000)]
RANDOM_TEXTS = [
    "".join(map(chr, random.Random(seed).sample(CODE_POINTS, 12))) for seed in range(20)
]
ESCAPES = [r"\u{41}", r"\u{1f600}", r"\\u{41}", "back\\slash", "\x00", '"\n\t\x00', "🙂", "a\nβ"]


@pytest.mark.parametrize("text", ["", "ordinary", *ESCAPES, *RANDOM_TEXTS])
def test_solver_strings_round_trip_without_escape_interpretation(text):
    assert ModelFiller(PlainString, mode="strict").fill(text=text).text == text


class IntegerEndpoints(BaseModel):
    lower: int = Field(gt=-3, le=0)
    upper: int = Field(ge=0, lt=3)


class BooleanArithmetic(BaseModel):
    a: bool
    b: bool
    total: int

    @model_validator(mode="after")
    def check(self):
        if self.total != self.a + self.b:
            raise ValueError("Boolean addition uses integers")
        if self.a < self.b:
            raise ValueError("Boolean comparison uses integers")
        if -self.a != -1:
            raise ValueError("Boolean negation uses integers")
        return self


@pytest.mark.parametrize(
    ("model", "grid"),
    [
        (Branches, {"a": range(-2, 4), "b": range(-3, 6)}),
        (BooleanOperands, {"a": range(-1, 4), "b": range(-1, 4), "enabled": (False, True)}),
        (IntegerEndpoints, {"lower": range(-4, 2), "upper": range(-1, 5)}),
        (BooleanArithmetic, {"a": (False, True), "b": (False, True), "total": range(3)}),
    ],
)
def test_fill_matches_original_constructor_exhaustively(model, grid):
    filler = ModelFiller(model, mode="strict")
    for values in product(*grid.values()):
        inputs = dict(zip(grid, values, strict=True))
        try:
            expected = model(**inputs)
        except ValidationError:
            with pytest.raises(UnsatisfiableConstraints):
                filler.fill(**inputs)
        else:
            assert filler.fill(**inputs) == expected


class RestrictedEnum(BaseModel):
    color: Literal[Color.BLUE]

    @model_validator(mode="after")
    def check(self):
        if self.color != Color.BLUE:
            raise ValueError("blue")
        return self


def test_literal_enum_uses_the_same_encoding_as_enum_constants():
    assert ModelFiller(RestrictedEnum, mode="strict").fill().color is Color.BLUE
    assert fill(RestrictedEnum, color=Color.BLUE).color is Color.BLUE


class UncheckedDefault(BaseModel):
    value: int = Field(default=-1, ge=0)

    @model_validator(mode="after")
    def require_default(self):
        if self.value != -1:
            raise ValueError("requires the unchecked default")
        return self


class CheckedDefault(BaseModel):
    value: int = Field(default=-1, ge=0, validate_default=True)

    @model_validator(mode="after")
    def require_default(self):
        if self.value != -1:
            raise ValueError("requires the default")
        return self


def test_unchecked_default_does_not_produce_a_false_global_unsat_claim():
    assert UncheckedDefault().value == -1
    filler = ModelFiller(UncheckedDefault, mode="strict")
    assert filler.explain()["unsat_requires_explicit_fields"] == ["value"]
    with pytest.raises(GenerationExhausted):
        filler.fill()
    with pytest.raises(UnsatisfiableConstraints):
        filler.fill(value=-1)
    with pytest.raises(UnsatisfiableConstraints):
        filler.fill(value=0)


def test_checked_default_allows_proven_unsatisfiability():
    with pytest.raises(ValidationError):
        CheckedDefault()
    with pytest.raises(UnsatisfiableConstraints):
        ModelFiller(CheckedDefault, mode="strict").fill()


class DefaultBypassesFieldValidator(BaseModel):
    value: int = Field(default=0, ge=0)

    @field_validator("value", mode="after")
    @classmethod
    def require_negative(cls, value):
        if value >= 0:
            raise ValueError("negative only")
        return value


def test_unchecked_default_can_also_bypass_field_validators():
    assert DefaultBypassesFieldValidator().value == 0
    filler = ModelFiller(DefaultBypassesFieldValidator, mode="strict")
    with pytest.raises(GenerationExhausted):
        filler.fill()
    with pytest.raises(UnsatisfiableConstraints):
        filler.fill(value=0)


def test_original_pydantic_validation_runs_for_every_returned_instance():
    class Observed(BaseModel):
        value: int
        calls: ClassVar[int] = 0

        @model_validator(mode="after")
        def check(self):
            type(self).calls += 1
            return self

    # The counter is test instrumentation. Its mutation is explicitly diagnosed
    # as unsupported, and only ordinary validation is allowed to execute it.
    filler = ModelFiller(Observed, mode="auto")
    assert Observed.calls == 0
    assert filler.explain()["unsupported"]
    results = filler.batch(5)
    assert len(results) == Observed.calls == 5


def test_original_dataclass_post_init_runs_for_every_returned_instance():
    @dataclass
    class Observed:
        value: int
        calls: ClassVar[int] = 0

        def __post_init__(self):
            type(self).calls += 1

    filler = ModelFiller(Observed, mode="auto")
    assert Observed.calls == 0
    assert filler.explain()["unsupported"]
    results = filler.batch(5)
    assert len(results) == Observed.calls == 5


class Booking(BaseModel):
    start: datetime
    end: datetime
    length: timedelta
    note: str | None = None

    @model_validator(mode="after")
    def check(self):
        if self.end != self.start + self.length:
            raise ValueError("end must equal start + length")
        if not timedelta(minutes=30) <= self.length <= timedelta(hours=4):
            raise ValueError("bookings last from 30 minutes to 4 hours")
        return self


def test_datetime_relationships_solve_in_both_directions():
    filler = ModelFiller(Booking, mode="strict")
    six = datetime(2025, 3, 1, 18)
    assert filler.fill(end=six, length=timedelta(hours=2)).start == datetime(2025, 3, 1, 16)
    booking = filler.fill(start=six)
    assert booking.end - booking.start == booking.length
    with pytest.raises(UnsatisfiableConstraints):  # The None default of note cannot help.
        filler.fill(start=six, end=six)
    with pytest.raises(TypeError):  # Only naive datetimes have an integer encoding.
        filler.fill(start=six.replace(tzinfo=UTC))


class Window(BaseModel):
    opens: int = Field(ge=0, le=23)
    closes: int | None = Field(ge=0, le=23)

    @model_validator(mode="after")
    def check(self):
        if self.closes is not None and self.closes <= self.opens:
            raise ValueError("a window closes after it opens")
        return self


def test_optional_fields_take_none_or_satisfying_values():
    filler = ModelFiller(Window, mode="strict")
    assert filler.fill(opens=22, closes=None).closes is None
    assert filler.fill(opens=22).closes is None
    assert filler.fill(opens=22, closes=23).closes == 23
    with pytest.raises(UnsatisfiableConstraints):
        filler.fill(opens=23, closes=5)
    assert [window.closes for window in filler.batch(3)] == [None, 1, 2]


class Ticket(BaseModel):
    id: UUID
    tags: list[str] = []
    seats: int = Field(ge=1, le=8)
    price: int

    @model_validator(mode="after")
    def check(self):
        if self.price != 25 * self.seats:
            raise ValueError("tickets cost 25 per seat")
        return self


def test_external_fields_pass_through_while_the_rest_is_solved():
    filler = ModelFiller(Ticket, mode="strict")
    ticket_id = uuid4()
    ticket = filler.fill(id=ticket_id, price=100)
    assert (ticket.id, ticket.tags, ticket.seats) == (ticket_id, [], 4)
    assert filler.explain()["fields"]["id"] == {
        "type": "external",
        "choices": None,
        "default": "required input",
    }
    with pytest.raises(UnsupportedConstraint, match="cannot generate id"):
        filler.fill(price=100)


class Packs(BaseModel):
    size: int = Field(ge=0, le=20, multiple_of=5)


def test_multiple_of_is_modeled():
    filler = ModelFiller(Packs, mode="strict")
    assert [pack.size for pack in filler.batch(5)] == [0, 5, 10, 15, 20]
    with pytest.raises(UnsatisfiableConstraints, match="size % 5 == 0"):
        filler.fill(size=7)


class Invoice(BaseModel):
    issued: date
    due: date

    @model_validator(mode="after")
    def check(self):
        if not self.issued < self.due <= self.issued + timedelta(days=30):
            raise ValueError("due within 30 days of issue")
        return self


def test_date_fields_are_generated():
    filler = ModelFiller(Invoice, mode="strict")
    for invoice in filler.batch(10):
        assert type(invoice.due) is date
        assert 0 < (invoice.due - invoice.issued).days <= 30
    assert filler.fill(due=date(2025, 1, 1)).issued == date(2024, 12, 31)


class Preferences(BaseModel):
    theme: Literal["light", "dark", None]
    color: Color | None

    @model_validator(mode="after")
    def check(self):
        if self.color == Color.RED and self.theme != "dark":
            raise ValueError("red needs the dark theme")
        return self


def test_optional_literal_and_enum_fields_are_generated():
    filler = ModelFiller(Preferences, mode="strict")
    assert filler.fill(color=Color.RED).theme == "dark"
    # None first, then the declared choices; red needs the dark theme. 7 of 3 x 3 are valid.
    assert [(p.theme, p.color) for p in filler.batch(7)] == [
        (None, None),
        (None, Color.BLUE),
        ("light", None),
        ("light", Color.BLUE),
        ("dark", None),
        ("dark", Color.RED),
        ("dark", Color.BLUE),
    ]
    with pytest.raises(GenerationExhausted, match="found 7 of 8"):
        filler.batch(8)
