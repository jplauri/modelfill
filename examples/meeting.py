"""Run with ``uv run python examples/meeting.py``."""

from typing import Self

from pydantic import BaseModel, Field, model_validator

from modelfill import ModelFiller, UnsatisfiableConstraints, fill


class Meeting(BaseModel):
    start: int = Field(ge=8, le=17)
    end: int = Field(ge=9, le=18)
    duration: int = Field(ge=1, le=4)

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.end != self.start + self.duration:
            raise ValueError("Invalid meeting")
        return self


def main() -> None:
    assert fill(Meeting, end=18, duration=4).start == 14
    assert fill(Meeting, start=10, end=13).duration == 3
    assert fill(Meeting, start=10, duration=2).end == 12
    print(fill(Meeting))  # Canonical: the boring valid meeting, the same every time.

    filler = ModelFiller(Meeting, mode="strict")
    print(filler.batch(5))  # Five distinct meetings, in canonical order.
    print(filler.explain())
    try:
        fill(Meeting, start=17, duration=4)
    except UnsatisfiableConstraints as error:
        print(f"Proven impossible: {error}")
    else:
        raise AssertionError("The impossible meeting unexpectedly succeeded")


if __name__ == "__main__":
    main()
