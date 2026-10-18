"""Fields the solver does not model: boring values, nested models and providers."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import pytest
from annotated_types import Interval
from pydantic import (
    UUID1,
    UUID4,
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    FutureDate,
    FutureDatetime,
    HttpUrl,
    PastDate,
    PastDatetime,
    conlist,
    field_validator,
    model_validator,
)

from modelfill import (
    GenerationExhausted,
    ModelFiller,
    UnsatisfiableConstraints,
    UnsupportedConstraint,
    fill,
)

NIL4 = UUID("00000000-0000-4000-8000-000000000000")


class Customer(BaseModel):
    id: UUID
    name: str
    age: int = Field(ge=18)


class Order(BaseModel):
    id: UUID
    customer: Customer
    total: float = Field(ge=0)
    tags: list[str]
    quantity: int = Field(ge=1)


def test_a_typical_model_fills_without_arguments():
    order = fill(Order)
    assert order.model_dump() == {
        "id": NIL4,
        "customer": {"id": NIL4, "name": "name", "age": 18},
        "total": 0.0,
        "tags": [],
        "quantity": 1,
    }
    assert fill(Order) == order  # Boring values are as deterministic as canonical ones.


class Everything(BaseModel):
    id1: UUID1
    id4: UUID4
    maybe_customer: Customer | None
    positive: float = Field(gt=0)
    price: Decimal = Field(ge=0)
    required_tags: list[str] = Field(min_length=1)
    pair: tuple[int, str]
    counts: dict[str, int]
    unique: frozenset[int]
    blob: bytes
    anything: Any
    either: int | str
    at: time
    stamp: AwareDatetime
    site: HttpUrl
    email: EmailStr


class Dated(BaseModel):
    born: PastDate
    due: FutureDate
    seen: PastDatetime
    expires: FutureDatetime


def test_past_and_future_dates_get_fixed_values():
    # These depend on the clock and once got no value at all.
    assert fill(Dated) == Dated(
        born=date(2000, 1, 1),
        due=date(2100, 1, 1),
        seen=datetime(2000, 1, 1),
        expires=datetime(2100, 1, 1),
    )


class Sized(BaseModel):
    tags: set[str] = Field(min_length=2)
    codes: frozenset[int] = Field(min_length=3)
    flags: set[bool] = Field(min_length=2)
    scores: dict[str, int] = Field(min_length=1)


def test_sets_and_dicts_get_distinct_boring_entries():
    # Distinct elements or keys once had no boring value beyond one.
    sized = fill(Sized)
    assert (sized.tags, sized.codes, sized.flags) == ({"", "1"}, {0, 1, 2}, {False, True})
    assert sized.scores == {"": 0}


def test_boring_values_per_type():
    assert fill(Everything).model_dump() == {
        "id1": UUID("00000000-0000-1000-8000-000000000000"),
        "id4": NIL4,
        "maybe_customer": None,
        "positive": 1.0,
        "price": Decimal(0),
        "required_tags": [""],
        "pair": (0, ""),
        "counts": {},
        "unique": frozenset(),
        "blob": b"",
        "anything": None,
        "either": 0,
        "at": time(0, 0),
        "stamp": datetime(2025, 1, 1, tzinfo=UTC),
        "site": HttpUrl("https://example.com/"),
        "email": "user@example.com",
    }


def test_a_dict_fixes_fields_of_a_nested_model():
    assert fill(Order, customer={"age": 30}).customer == Customer(id=NIL4, name="name", age=30)
    given = Customer(id=uuid4(), name="Ada", age=40)
    assert fill(Order, customer=given).customer is given  # An instance is used as it is.
    with pytest.raises(UnsatisfiableConstraints, match="customer.age=10 conflict with"):
        fill(Order, customer={"age": 10})


class Shipment(BaseModel):
    order: Order
    backup: Order | None


def test_nested_models_nest_and_optional_ones_stay_none():
    shipment = fill(Shipment, order={"customer": {"name": "Ada"}}, backup={"quantity": 2})
    assert shipment.order.customer.name == "Ada"
    assert shipment.backup is not None and shipment.backup.quantity == 2
    assert fill(Shipment).backup is None


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


def test_dataclasses_nest_too():
    assert fill(Segment, end={"x": 5}) == Segment(Point(0, 0), Point(5, 0))


class Keyed(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    customer: Customer
    note: str = "keep"


def test_providers_beat_defaults_and_a_path_beats_a_type():
    seven, nine = UUID(int=7), UUID(int=9)
    filler = ModelFiller(Keyed, providers={UUID: lambda: seven, "customer.id": lambda: nine})
    keyed = filler.fill()
    assert (keyed.id, keyed.customer.id, keyed.note) == (seven, nine, "keep")
    assert filler.fill(id=uuid4()).id != seven  # Your own value still wins.
    sources = {name: field["default"] for name, field in filler.explain()["fields"].items()}
    assert sources == {
        "id": "provider",
        "customer": "nested model",
        "note": "preference",
        "customer.name": "generated",  # Solved with the outer model, by path.
        "customer.age": "generated",
    }


@pytest.mark.parametrize(
    ("providers", "error", "message"),
    [
        ({"id": UUID(int=1)}, TypeError, "callable"),
        ({int: lambda: 1}, TypeError, "solves such fields"),
        ({"customer.name": lambda: "Ada"}, TypeError, "'customer.name': modelfill solves"),
        ({"customer.nope": lambda: 1}, ValueError, "Customer has no field 'nope'"),
        ({"id.hex": lambda: 1}, ValueError, "'id' is not a nested model"),
    ],
)
def test_bad_providers_fail_when_the_filler_is_created(providers, error, message):
    with pytest.raises(error, match=message):
        ModelFiller(Keyed, providers=providers)


class Token:
    """A class modelfill knows nothing about."""


class Session(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    token: Token
    user: int


def test_fields_without_a_boring_value_need_a_value_or_a_provider():
    with pytest.raises(UnsupportedConstraint, match="cannot generate token; pass values or add"):
        fill(Session)
    assert isinstance(ModelFiller(Session, providers={Token: Token}).fill().token, Token)
    assert ModelFiller(Session).explain()["fields"]["token"]["default"] == "required input"


class Narrow(BaseModel):  # Without decimal_places, a Decimal is external.
    price: Decimal = Field(max_digits=2, ge=Decimal("0.5"), le=Decimal("0.75"))


def test_each_batch_member_gets_its_own_provider_values():
    ids = iter(range(100))
    orders = ModelFiller(Order, providers={UUID: lambda: UUID(int=next(ids))}).batch(3)
    assert [(order.id.int, order.customer.id.int) for order in orders] == [(0, 1), (2, 3), (4, 5)]


class Grouped(BaseModel):
    tags: conlist(int, min_length=2)
    ratio: Annotated[float, Interval(ge=5, le=9)]


def test_boring_values_meet_grouped_constraints():
    assert fill(Grouped) == Grouped(tags=[0, 0], ratio=5.0)


class Unbounded(BaseModel):
    low: float = Field(ge=float("-inf"))
    high: float = Field(le=float("inf"))


def test_infinite_bounds_leave_zero():
    assert fill(Unbounded) == Unbounded(low=0.0, high=0.0)
    assert ModelFiller(Unbounded).explain()["fields"]["low"]["default"] == "boring value"


def in_order(tags):
    return sorted(tags)


class Sorted(BaseModel):
    tags: Annotated[list[str], AfterValidator(in_order)]


def test_code_in_an_external_field_metadata_is_unsupported():
    # Analyzed as a field validator, it reads a field the solver does not model.
    with pytest.raises(UnsupportedConstraint, match="in_order.*no known target field"):
        ModelFiller(Sorted, mode="strict")
    assert fill(Sorted).tags == []


class FlooredNumber(BaseModel):  # The rule reads the provided floor, so it falls back.
    number: int = Field(ge=0, le=2)
    floor: float

    @model_validator(mode="after")
    def check(self):
        if self.number < self.floor:
            raise ValueError("the number is below the floor")
        return self


class ShiftedNumber(BaseModel):
    shift: float
    number: int = Field(ge=0, le=0)

    @field_validator("number")
    @classmethod
    def add_shift(cls, number, info):  # Unsupported: it changes the value.
        return number + int(info.data["shift"])


def test_a_batch_member_retries_candidates_under_its_own_provider_values():
    # Under the first member's values number=0 failed, or became 2, and it stayed
    # excluded, though the second member's values make it fit.
    floors, shifts = iter([2.0, 0.0]), iter([2.0, 0.0])
    floored = ModelFiller(FlooredNumber, providers={"floor": lambda: next(floors)}, max_attempts=3)
    assert [(each.number, each.floor) for each in floored.batch(2)] == [(2, 2.0), (0, 0.0)]
    shifted = ModelFiller(ShiftedNumber, providers={"shift": lambda: next(shifts)}, max_attempts=3)
    assert [each.number for each in shifted.batch(2)] == [2, 0]


def broken():
    raise ValueError("factory failure")


class BrokenDefault(BaseModel):
    number: int = Field(ge=0, le=2)
    id: UUID = Field(default_factory=broken)


@dataclass
class BrokenDataclass:
    number: int
    tags: list[str] = field(default_factory=broken)


@pytest.mark.parametrize("model", [BrokenDefault, BrokenDataclass])
def test_a_failing_default_factory_propagates(model):
    # Its error once counted as a rejection, retried until GenerationExhausted.
    with pytest.raises(ValueError, match="factory failure"):
        ModelFiller(model, max_attempts=3).fill()


class Line(BaseModel):
    sku: str
    quantity: int = Field(ge=1)
    id: UUID


class Basket(BaseModel):
    lines: list[Line] = Field(min_length=2)
    pair: tuple[Line, Line]
    counted: tuple[Line, ...] = Field(min_length=1)
    by_code: dict[str, Line] = Field(min_length=1)


class Courier(BaseModel):
    kind: Literal["courier"]
    address: str


class Pickup(BaseModel):
    kind: Literal["pickup"]
    store: str


class Never(BaseModel):  # No value fits, so a union passes over it.
    n: int = Field(ge=1, le=0)


class Dispatch(BaseModel):
    delivery: Courier | Pickup = Field(discriminator="kind")
    fallback: Never | Pickup


class Tree(BaseModel):
    children: list["Tree"] = Field(min_length=1)


def test_collections_and_unions_of_models_are_filled():
    # Such fields once had no value: a model has no boring value of its own.
    basket = fill(Basket)
    line = Line(sku="sku", quantity=1, id=NIL4)
    assert basket.lines == [line, line] and basket.lines[0] is not basket.lines[1]
    assert (basket.pair, basket.counted, basket.by_code) == ((line, line), (line,), {"": line})
    dispatch = fill(Dispatch)  # A branch's tag is the one value its Literal allows.
    assert dispatch.delivery == Courier(kind="courier", address="address")
    assert dispatch.fallback == Pickup(kind="pickup", store="store")
    assert ModelFiller(Basket).explain()["fields"]["lines"]["default"] == "nested models"
    with pytest.raises(UnsupportedConstraint, match="children: Tree contains itself"):
        fill(Tree)


def test_dicts_inside_given_collections_and_unions_are_completed():
    # Each dict once had to be a whole model.
    basket = fill(Basket, lines=[{"quantity": 3}, {}], by_code={"a": {"sku": "x"}})
    assert [line.quantity for line in basket.lines] == [3, 1]
    assert (basket.by_code["a"].sku, basket.by_code["a"].quantity) == ("x", 1)
    dispatch = fill(Dispatch, delivery={"kind": "pickup"})  # The tag picks the branch.
    assert dispatch.delivery == Pickup(kind="pickup", store="store")
    with pytest.raises(GenerationExhausted):  # A given list keeps its shape: too short.
        fill(Basket, lines=[{}])


class Van(BaseModel):
    kind: Literal["van"]
    plate: UUID


class Bike(BaseModel):
    kind: Literal["bike"]


class Fleet(BaseModel):
    vehicle: Van | Bike = Field(discriminator="kind")


def test_providers_reach_into_collections_and_unions():
    ids = iter(range(100))
    providers = {"lines.id": lambda: UUID(int=next(ids)), "by_code.id": lambda: UUID(int=99)}
    basket = ModelFiller(Basket, providers=providers).fill()
    assert [line.id.int for line in basket.lines] == [0, 1]  # Each element its own value.
    assert basket.by_code[""].id == UUID(int=99)
    # A path into a union applies to the members with that field; Bike has no plate.
    fleet = ModelFiller(Fleet, providers={"vehicle.plate": lambda: UUID(int=7)})
    assert fleet.fill().vehicle == Van(kind="van", plate=UUID(int=7))
    assert fleet.fill(vehicle={"kind": "bike"}).vehicle == Bike(kind="bike")


def test_a_rejected_boring_value_is_named_in_the_error():
    with pytest.raises(GenerationExhausted, match="value chosen for price; pass a value"):
        fill(Narrow)
    assert fill(Narrow, price=Decimal("0.75")).price == Decimal("0.75")


class CountAndItems(BaseModel):
    count: int = Field(ge=0, le=1)
    items: list[int]

    @field_validator("items")
    @classmethod
    def check(cls, items, info):  # Reported at items, but count decides it.
        if info.data["count"] != len(items) + 1:
            raise ValueError("count must be one more than the number of items")
        return items


def test_a_rejection_at_a_chosen_value_does_not_end_the_search():
    assert fill(CountAndItems) == CountAndItems(count=1, items=[])


class Priced(BaseModel):
    quantity: int
    total: float


def test_pydantic_may_convert_values_of_fields_the_solver_does_not_model():
    assert fill(Priced, total=5).total == 5.0


class Node(BaseModel):
    child: "Node"


def test_a_model_that_contains_itself_needs_a_value():
    with pytest.raises(UnsupportedConstraint, match="contains itself"):
        fill(Node)
