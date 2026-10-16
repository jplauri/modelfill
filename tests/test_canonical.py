"""Canonical completion: boring, valid, deterministic values for fields a test ignores."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Annotated, Literal

import pytest
from pydantic import AfterValidator, BaseModel, Field, model_validator

from examples.meeting import Meeting
from modelfill import GenerationExhausted, ModelFiller, SolverTimeout, SolverUnknown, fill


def rows(instances):
    return [tuple(instance.model_dump().values()) for instance in instances]


def test_meeting_canonical_values():
    # Declaration order: start is nearest 0 (its bound 8), then end and duration follow.
    assert fill(Meeting).model_dump() == {"start": 8, "end": 9, "duration": 1}
    assert fill(Meeting, end=18).model_dump() == {"start": 14, "end": 18, "duration": 4}


class Ordered(BaseModel):
    a: int
    c: int = 5

    @model_validator(mode="after")
    def check(self):
        if self.a < self.c:
            raise ValueError("a below c")
        return self


def test_fixing_a_later_field_keeps_earlier_free_fields_unless_forced():
    assert fill(Meeting, duration=3).model_dump() == {"start": 8, "end": 11, "duration": 3}
    assert fill(Meeting, end=12).start == 8  # 8 still fits, so it stays.
    assert fill(Meeting, end=18).start == 14  # Forced: a meeting lasts at most 4 hours.
    # Defaults are settled before anchors: c's default holds a at 5 until c is fixed.
    assert [fill(Ordered, **fixed).a for fixed in ({}, {"c": 5}, {"c": 0})] == [5, 5, 0]


class Color(Enum):
    RED = "red"
    BLUE = "blue"


class Anchors(BaseModel):
    flag: bool
    color: Color
    size: Literal["small", "large"]
    note: str | None
    count: int
    when: datetime
    day: date
    span: timedelta
    title: str
    level: int = 7


def test_per_type_anchors_and_defaults():
    assert fill(Anchors).model_dump() == {
        "flag": False,
        "color": Color.RED,
        "size": "small",
        "note": None,
        "count": 0,
        "when": datetime(2025, 1, 1),
        "day": date(2025, 1, 1),
        "span": timedelta(0),
        "title": "title",
        "level": 7,
    }


class Forced(BaseModel):
    flag: bool
    color: Color
    maybe: int | None
    level: int = Field(default=7, le=5)  # A default the rules reject is replaced.

    @model_validator(mode="after")
    def check(self):
        if not self.flag or self.color == Color.RED or self.maybe is None:
            raise ValueError("forced away from the anchors")
        return self


def test_anchors_give_way_to_the_rules():
    assert fill(Forced).model_dump() == {"flag": True, "color": Color.BLUE, "maybe": 0, "level": 0}


class Bounded(BaseModel):
    inside: int = Field(ge=-100, le=100)
    above: int = Field(gt=5)
    below: int = Field(lt=-5)
    nonzero: int = Field(ge=-3, le=3)

    @model_validator(mode="after")
    def check(self):
        if self.nonzero == 0:
            raise ValueError("nonzero")
        return self


def test_bounds_appear_only_when_they_are_the_closest_allowed_value():
    # 0 when allowed, not a bound; otherwise the nearest value. A tie goes above the anchor.
    assert rows([fill(Bounded)]) == [(0, 6, -6, 1)]


class Texts(BaseModel):
    title: str
    code: str = Field(min_length=6)
    short: str = Field(max_length=3)


class Confirm(BaseModel):
    password: str
    confirm: str

    @model_validator(mode="after")
    def check(self):
        if self.password != self.confirm:
            raise ValueError("passwords differ")
        return self


def test_strings_are_readable_and_deterministic():
    assert fill(Texts).model_dump() == {"title": "title", "code": "code_1", "short": "sho"}
    assert [text.title for text in ModelFiller(Texts).batch(3)] == ["title", "title", "title"]
    assert [text.short for text in ModelFiller(Texts).batch(3)] == ["sho", "sh1", "sh2"]
    assert fill(Confirm).model_dump() == {"password": "password", "confirm": "password"}
    assert fill(Confirm, password="hunter2").confirm == "hunter2"


def test_same_inputs_give_equal_objects_across_fresh_fillers():
    assert ModelFiller(Anchors).fill() == ModelFiller(Anchors).fill() == fill(Anchors)
    assert ModelFiller(Meeting).batch(5) == ModelFiller(Meeting).batch(5)


def test_results_do_not_depend_on_earlier_calls():
    expected = ModelFiller(Meeting).fill(start=10)
    filler = ModelFiller(Meeting)
    filler.fill(end=18)
    filler.batch(5)
    fill(Anchors)
    filler.fill(duration=2)
    assert filler.fill(start=10) == expected


class ExternalFloor(BaseModel):  # The rule reads an external field, so it falls back.
    floor: float
    value: int

    @model_validator(mode="after")
    def check(self):
        if self.value < self.floor:
            raise ValueError("below the floor")
        return self


def above_value(floor, info):
    if info.data["value"] < floor:
        raise ValueError("below the floor")
    return floor


class HiddenFloor(BaseModel):  # The same rule, on the external field: modelfill sees none.
    value: int
    floor: Annotated[float, AfterValidator(above_value)]


@pytest.mark.parametrize("model", [ExternalFloor, HiddenFloor])
def test_external_values_do_not_leak_into_later_calls(model):
    filler = ModelFiller(model)
    assert [filler.fill(floor=floor).value for floor in (2.0, 0.0, 2.0, 0.0)] == [2, 0, 2, 0]


def test_results_do_not_depend_on_the_thread():
    filler = ModelFiller(Meeting)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: filler.fill(end=15), range(8)))
    assert results == [ModelFiller(Meeting).fill(end=15)] * 8


def test_results_do_not_depend_on_the_time_budget():
    quick, patient = ModelFiller(Anchors, timeout_ms=100), ModelFiller(Anchors, timeout_ms=10**6)
    assert quick.fill() == patient.fill()


def test_the_deterministic_work_limit_raises_rather_than_guessing():
    with pytest.raises(SolverUnknown):  # SolverTimeout, unless an older Z3 omits the reason.
        ModelFiller(Meeting, work_limit=1).fill()
    assert ModelFiller(Meeting, work_limit=10_000).fill() == fill(Meeting)  # Ample here.


def test_a_probe_out_of_budget_raises_instead_of_changing_values(monkeypatch):
    filler = ModelFiller(Meeting)
    solver, checks = filler._solver(), []
    original = solver.check

    def check():
        checks.append(None)
        if len(checks) > 2:  # The proof passes; a later canonical probe runs out.
            raise SolverTimeout("Z3 exhausted its budget: canceled")
        return original()

    monkeypatch.setattr(solver, "check", check)
    with pytest.raises(SolverTimeout):
        filler.fill()


class Switch(BaseModel):
    on: bool


def test_batch_is_distinct_canonical_order_and_raises_when_completions_run_out():
    assert rows(ModelFiller(Meeting).batch(6)) == [
        (8, 9, 1),
        (8, 10, 2),
        (8, 11, 3),
        (8, 12, 4),
        (9, 10, 1),
        (9, 11, 2),
    ]
    assert [meeting.duration for meeting in ModelFiller(Meeting).batch(4, start=10)] == [1, 2, 3, 4]
    assert [switch.on for switch in ModelFiller(Switch).batch(2)] == [False, True]
    with pytest.raises(GenerationExhausted, match="found 2 of 3"):
        ModelFiller(Switch).batch(3)


class EvenOnly(BaseModel):
    value: int = Field(ge=1, le=10)

    @model_validator(mode="after")
    def check(self):
        if pow(self.value, 1, 2):  # A call: unsupported, so auto mode falls back.
            raise ValueError("must be even")
        return self


def test_auto_mode_fallback_is_deterministic():
    assert ModelFiller(EvenOnly).explain()["unsupported"]
    assert fill(EvenOnly).value == 2  # 1 is rejected by validation, blocked, then 2.
    assert [even.value for even in ModelFiller(EvenOnly).batch(3)] == [2, 4, 6]


def test_seed_is_gone_and_explain_names_the_selection():
    with pytest.raises(TypeError):
        ModelFiller(Meeting, seed=1)
    explanation = ModelFiller(Meeting, work_limit=5000).explain()
    assert explanation["selection"] == "canonical"
    assert explanation["work_limit_per_check"] == 5000
    assert "seed" not in explanation
