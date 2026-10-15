"""The Hypothesis strategy: Hypothesis draws some fields, modelfill solves the rest."""

import random
import re
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from hypothesis import find, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, Field, model_validator

from modelfill import ModelFiller, fill
from modelfill.hypothesis import from_model


class Shift(BaseModel):
    id: UUID
    start: datetime
    end: datetime
    break_minutes: int

    @model_validator(mode="after")
    def check(self):
        if not self.start < self.end <= self.start + timedelta(hours=10):
            raise ValueError("shifts last up to ten hours")
        if not 0 <= self.break_minutes <= 60:
            raise ValueError("breaks last up to an hour")
        return self


STARTS = st.datetimes(min_value=datetime(2025, 1, 1), max_value=datetime(2026, 1, 1))


@settings(max_examples=25, deadline=None)
@given(from_model(Shift, start=STARTS, break_minutes=st.integers(-30, 90)))
def test_drawn_fields_are_kept_and_the_rest_is_solved(shift):
    assert isinstance(shift.id, UUID)  # External: drawn with st.from_type.
    assert shift.start < shift.end <= shift.start + timedelta(hours=10)
    assert 0 <= shift.break_minutes <= 60  # Impossible draws were rejected, not failed.


REPLAYABLE = settings(database=None, derandomize=True)


def test_shrinking_reaches_the_boundary_and_replays_identically():
    strategy = from_model(Shift, start=STARTS, break_minutes=st.integers(0, 60))

    def long_break(shift):
        return shift.break_minutes >= 30

    first = find(strategy, long_break, settings=REPLAYABLE)
    assert first.break_minutes == 30
    assert find(strategy, long_break, settings=REPLAYABLE) == first


@settings(max_examples=5, deadline=None)
@given(from_model(ModelFiller(Shift, mode="strict", max_attempts=3), start=STARTS))
def test_a_configured_filler_works_as_the_template(shift):
    assert shift.start < shift.end


class Wide(BaseModel):
    amount: int = Field(ge=1000, le=1_000_000)
    optional: int | None = Field(ge=1000, le=1_000_000)  # Required, so it is explored.


def test_exploration_finds_edges_that_canonical_values_never_take():
    assert fill(Wide).model_dump() == {"amount": 1000, "optional": None}
    found = find(from_model(Wide), lambda wide: wide.amount == 1_000_000, settings=REPLAYABLE)
    assert found.amount == 1_000_000


def test_exploration_spreads_over_the_feasible_range_and_its_edges():
    filler, rng = ModelFiller(Wide), random.Random(0)  # The sampler behind from_model.
    wides = [filler._complete({}, 1, rng)[0] for _ in range(100)]
    for values in ({wide.amount for wide in wides}, {wide.optional for wide in wides} - {None}):
        assert {1000, 1_000_000} <= values
        assert len(values) > 40


class Tagged(BaseModel):
    n: int
    id: UUID = Field(default_factory=uuid4)


@settings(max_examples=5, deadline=None)
@given(from_model(ModelFiller(Tagged, providers={UUID: lambda: UUID(int=7)})))
def test_a_configured_filler_brings_its_providers(tagged):
    assert tagged.id == UUID(int=7)  # The provider beats the model's random default.


class Coded(BaseModel):
    code: str = Field(pattern=r"^[A-Z]{3}-\d{2}$")
    count: int = Field(ge=0)


def test_exploration_draws_values_that_match_patterns():
    filler, rng = ModelFiller(Coded), random.Random(0)  # The sampler behind from_model.
    codes = {filler._complete({}, 1, rng)[0].code for _ in range(50)}
    assert all(re.fullmatch(r"[A-Z]{3}-\d{2}", code) for code in codes)
    assert len(codes) > 40  # Spread out, unlike the canonical COD-00.
