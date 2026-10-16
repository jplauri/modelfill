"""Fields the solver does not model: boring values, nested models and providers."""

from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import (
    UUID1,
    UUID4,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    field_validator,
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


class Narrow(BaseModel):
    price: Decimal = Field(max_digits=2, decimal_places=2, ge=Decimal("0.5"))


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
