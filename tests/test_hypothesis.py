"""The Hypothesis strategy: Hypothesis draws some fields, modelfill solves the rest."""

from datetime import datetime, timedelta
from uuid import UUID

from hypothesis import find, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, model_validator

from modelfill import ModelFiller
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
