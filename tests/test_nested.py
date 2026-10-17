"""Rules across nested models: required nested models are solved with their parent."""

from dataclasses import dataclass
from typing import Annotated

import pydantic
import pytest
from annotated_types import GroupedMetadata
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from modelfill import (
    GenerationExhausted,
    ModelFiller,
    UnsatisfiableConstraints,
    UnsupportedConstraint,
    fill,
)


class Person(BaseModel):
    name: str
    age: int = Field(ge=0, le=120)


class Ticket(BaseModel):
    holder: Person
    adult_only: bool

    @model_validator(mode="after")
    def check(self):
        if self.adult_only and self.holder.age < 18:
            raise ValueError("adults only")
        return self


def test_a_rule_reads_a_nested_field_and_solving_crosses_levels():
    assert fill(Ticket).model_dump() == {"holder": {"name": "name", "age": 0}, "adult_only": False}
    assert fill(Ticket, adult_only=True).holder.age == 18  # The parent's rule moves the child.
    assert fill(Ticket, holder={"age": 30}).adult_only is False
    assert ModelFiller(Ticket, mode="strict").explain()["unsupported"] == []


def test_impossibility_is_proven_across_levels_and_explained_by_path():
    with pytest.raises(UnsatisfiableConstraints) as raised:
        fill(Ticket, adult_only=True, holder={"age": 10})
    for part in ("holder.age=10", "adult_only=True", "Ticket.check"):
        assert part in str(raised.value)
    with pytest.raises(UnsatisfiableConstraints, match="age=200 conflict with holder.age <= 120"):
        fill(Ticket, holder={"age": 200})


def test_a_given_instance_is_kept_and_its_values_constrain_the_parent():
    adult = Person(name="Ada", age=30)
    assert fill(Ticket, holder=adult, adult_only=True).holder is adult
    with pytest.raises(UnsatisfiableConstraints, match="holder.age=10"):
        fill(Ticket, holder=Person(name="Kid", age=10), adult_only=True)
    adults = ModelFiller(Ticket, providers={Person: lambda: Person(name="Bo", age=40)})
    assert adults.fill(adult_only=True).holder.name == "Bo"  # A provider's instance, too.


def test_batches_vary_nested_fields_in_canonical_order():
    tickets = ModelFiller(Ticket).batch(3, adult_only=True)
    assert [ticket.holder.age for ticket in tickets] == [18, 19, 20]


class Venue(BaseModel):
    capacity: int = Field(ge=1)


class Event(BaseModel):
    venue: Venue
    sold: int = Field(ge=0)

    @field_validator("venue")
    @classmethod
    def big_enough(cls, venue):
        if venue.capacity < 10:
            raise ValueError("too small")
        return venue

    @model_validator(mode="after")
    def fits(self):
        if self.sold > self.venue.capacity:
            raise ValueError("oversold")
        return self


def test_field_validators_read_a_nested_value_through_its_fields():
    assert fill(Event).venue.capacity == 10
    assert fill(Event, sold=50).venue.capacity == 50
    with pytest.raises(UnsatisfiableConstraints):
        fill(Event, venue={"capacity": 5})


class Street(BaseModel):
    number: int = Field(ge=1)


class Address(BaseModel):
    street: Street


class Parcel(BaseModel):
    address: Address
    floor: int

    @model_validator(mode="after")
    def check(self):
        if self.floor > self.address.street.number:
            raise ValueError("no such floor")
        return self


def test_rules_reach_through_several_levels():
    assert fill(Parcel, floor=7).address.street.number == 7
    assert fill(Parcel, address={"street": {"number": 3}}).floor == 0


@dataclass
class Point:
    x: int
    y: int


@dataclass
class Segment:
    start: Point
    end: Point

    def __post_init__(self):
        if self.end.x < self.start.x:
            raise ValueError("left to right")


def test_dataclasses_solve_across_levels_too():
    assert fill(Segment, start={"x": 5}) == Segment(Point(5, 0), Point(5, 0))
    with pytest.raises(UnsatisfiableConstraints):
        ModelFiller(Segment, mode="strict").fill(start={"x": 5}, end={"x": 4})


class Guest(BaseModel):
    age: int

    @model_validator(mode="after")
    def check(self):
        if pow(self.age, 1, 2):  # A call: unsupported.
            raise ValueError("even ages only")
        return self


class Party(BaseModel):
    guest: Guest
    seats: int

    @model_validator(mode="after")
    def check(self):
        if self.seats < self.guest.age:
            raise ValueError("not enough seats")
        return self


def test_unsupported_logic_in_a_nested_model_makes_the_whole_object_fall_back():
    with pytest.raises(UnsupportedConstraint, match="guest: "):
        ModelFiller(Party, mode="strict")
    party = fill(Party, seats=3)
    assert party.guest.age % 2 == 0 and party.seats >= party.guest.age


class Maybe(BaseModel):
    holder: Person | None
    holder_with_default: Person = Person(name="Default", age=50)

    @model_validator(mode="after")
    def check(self):
        if self.holder_with_default.age < 18:
            raise ValueError("adults only")
        return self


def test_optional_and_defaulted_nested_models_are_completed_on_their_own():
    assert ModelFiller(Maybe).explain()["unsupported"]  # The rule reads an unsolved model.
    maybe = fill(Maybe)
    assert maybe.holder is None and maybe.holder_with_default.name == "Default"


class Account(BaseModel):
    balance: int = Field(default=-1, ge=0)  # An unchecked default Pydantic would keep.


class Wallet(BaseModel):
    account: Account

    @model_validator(mode="after")
    def check(self):
        if self.account.balance != -1:
            raise ValueError("needs the default")
        return self


def test_unchecked_nested_defaults_still_block_an_impossibility_claim():
    filler = ModelFiller(Wallet, mode="strict")
    assert filler.explain()["unsat_requires_explicit_fields"] == ["account.balance"]
    with pytest.raises(GenerationExhausted):
        filler.fill()
    with pytest.raises(UnsatisfiableConstraints):
        filler.fill(account={"balance": -1})


class Counter(BaseModel):
    count: int = Field(ge=0)


def negative_copy(counter):  # A new object: the input is left alone.
    return counter.model_copy(update={"count": -counter.count - 1})


class Flip(GroupedMetadata):  # Pydantic unpacks a group and runs what is inside.
    def __iter__(self):
        yield AfterValidator(negative_copy)


FLIPS = [AfterValidator(negative_copy), Flip()]
if hasattr(pydantic, "ValidateAs"):  # A Pydantic marker that runs the function it is given.
    FLIPS.append(pydantic.ValidateAs(Counter, negative_copy))


def flipped(flip):
    class Flipped(BaseModel):
        counter: Annotated[Counter, flip]

        @model_validator(mode="after")
        def check(self):  # Reads the copy, not the Counter that met count >= 0.
            if self.counter.count >= 0:
                raise ValueError("the count must be negative after the copy")
            return self

    return Flipped


@pytest.mark.parametrize("flip", FLIPS, ids=lambda flip: type(flip).__name__)
def test_code_in_a_nested_model_field_metadata_is_unsupported(flip):
    # Solving count >= 0 and count < 0 together once claimed a contradiction.
    model = flipped(flip)
    # An AfterValidator is analyzed as a field validator, and this one returns a copy.
    with pytest.raises(UnsupportedConstraint, match="return must preserve|unsupported metadata"):
        ModelFiller(model, mode="strict")
    assert fill(model).counter.count == -1


def positive_count(counter):
    if counter.count <= 0:
        raise ValueError("the count must be positive")
    return counter


class Checked(BaseModel):
    counter: Annotated[Counter, AfterValidator(positive_count)]


def test_a_pure_check_on_a_nested_model_field_is_modeled():
    assert ModelFiller(Checked, mode="strict").fill().counter.count == 1


class Leaf(BaseModel):
    floor: int


class Box(BaseModel):
    leaf: Leaf


class Envelope(BaseModel):
    box: Box
    serial: int

    @model_validator(mode="after")
    def check(self):
        if self.serial < self.box.leaf.floor:
            raise ValueError("the serial is below the floor")
        return self


@pytest.mark.parametrize(
    "path, provide",
    [
        ("box.leaf", lambda floor: Leaf(floor=floor)),
        ("box.leaf", lambda floor: {"floor": floor}),
        ("box", lambda floor: {"leaf": {"floor": floor}}),
    ],
)
def test_a_provided_nested_model_stays_fixed_for_a_whole_batch(path, provide):
    # Running the provider again for the second member once broke the floor that the
    # solver had fixed.
    floors = iter([0, 100])
    filler = ModelFiller(Envelope, providers={path: lambda: provide(next(floors))}, max_attempts=3)
    assert [(env.box.leaf.floor, env.serial) for env in filler.batch(2)] == [(0, 0), (0, 1)]


def test_a_given_instance_must_keep_its_own_rules():
    filler = ModelFiller(Wallet, mode="strict")
    # Pydantic keeps Account() with its unchecked default; modelfill would solve with
    # balance >= 0 and claim a contradiction, so the instance itself is refused.
    with pytest.raises(ValueError, match=r"breaks its own rules.*balance >= 0"):
        filler.fill(account=Account())
    with pytest.raises(UnsatisfiableConstraints):  # A valid instance the parent rejects.
        filler.fill(account=Account(balance=5))
