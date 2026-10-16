"""String patterns: Pydantic decides what matches, and values are spelled from the pattern."""

import random
import re
import sys
from typing import Annotated

import pytest
from annotated_types import Interval
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    conint,
    constr,
    field_validator,
    model_validator,
)

from modelfill import GenerationExhausted, ModelFiller, UnsatisfiableConstraints, fill


class Product(BaseModel):
    code: str = Field(pattern=r"^[A-Z]{3}$")
    sku: str = Field(pattern=r"^[A-Z]{3}-\d{4}$")
    zip: Annotated[str, StringConstraints(pattern=r"^\d{5}$")]
    phone: constr(pattern=r"^\+?[1-9]\d{1,14}$")
    city: str = Field(pattern=r"^[A-Z][a-z]+$")
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


def test_values_start_from_the_field_name_where_the_pattern_allows_else_shortest():
    assert fill(Product).model_dump() == {
        "code": "COD",
        "sku": "SKU-0000",
        "zip": "00000",
        "phone": "10",
        "city": "City",
        "slug": "slug",  # A readable candidate that matches comes first.
        "email": "e@a.a",
        "version": "0.0.0",
    }
    assert ModelFiller(Product, mode="strict").explain()["fields"]["zip"]["pattern"] == r"^\d{5}$"


def test_a_fixed_value_that_breaks_the_pattern_is_impossible():
    expected = "Product: code='usd' conflict with code pattern '^[A-Z]{3}$'"
    with pytest.raises(UnsatisfiableConstraints, match=re.escape(expected)):
        fill(Product, code="usd")
    assert fill(Product, code="USD").code == "USD"


class Currency(BaseModel):
    code: str = Field(pattern=r"^[A-Z]{3}$")


def test_batches_continue_in_canonical_order():
    assert [currency.code for currency in ModelFiller(Currency).batch(3)] == ["COD", "COA", "COB"]


class Price(BaseModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: int = Field(ge=0)

    @model_validator(mode="after")
    def check(self):
        if self.currency != "EUR" and self.amount > 100:
            raise ValueError("only euros above 100")
        return self


class Lowercase(BaseModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def check(self):
        if self.currency != "eur":
            raise ValueError("lowercase euros only")
        return self


def test_rules_that_read_a_patterned_field_stay_exact():
    assert fill(Price).currency == "CUR"
    assert fill(Price, amount=500).currency == "EUR"  # The rule's own string matches.
    with pytest.raises(UnsatisfiableConstraints, match="amount=-1 conflict with amount >= 0"):
        fill(Price, amount=-1)  # Proofs about other fields still hold.
    # The pattern is not part of proofs: a rule it contradicts exhausts the search.
    with pytest.raises(GenerationExhausted, match="found no string for 'currency' that matches"):
        fill(Lowercase)


class Customer(BaseModel):
    country: str = Field(pattern=r"^[A-Z]{2}$")
    nickname: str | None = Field(pattern=r"^[a-z]+$")


class Order(BaseModel):
    customer: Customer
    quantity: int = Field(ge=1)


def test_nested_and_optional_fields():
    assert fill(Order).customer == Customer(country="CO", nickname=None)
    assert fill(Order, customer={"nickname": "bo"}).customer.nickname == "bo"
    with pytest.raises(UnsatisfiableConstraints, match="customer.nickname='Bo' conflict"):
        fill(Order, customer={"nickname": "Bo"})
    unchecked = Customer.model_construct(country="usa", nickname=None)
    assert fill(Order, customer=unchecked).customer is unchecked  # Used as it is.


class Loose(BaseModel):
    model_config = ConfigDict(regex_engine="python-re")
    word: str = Field(pattern=r"^(?=[a-z])\w+$")  # A lookahead, which only python-re has.
    code: str = Field(pattern=re.compile(r"^[a-z]{2}$", re.IGNORECASE))


class Letters(BaseModel):
    name: str = Field(pattern=r"^\p{L}+$")  # Rust syntax that Python's parser rejects.
    initials: str = Field(pattern=r"^\p{Lu}{2}$")


def test_pydantic_decides_for_any_engine_or_pattern_it_accepts():
    assert fill(Loose).model_dump() == {"word": "word", "code": "co"}
    assert ModelFiller(Loose, mode="strict").explain()["unsupported"] == []
    filler = ModelFiller(Letters)
    assert filler.fill(initials="JL").name == "name"  # A readable candidate matches.
    with pytest.raises(GenerationExhausted, match="found no string for 'initials' that matches"):
        filler.fill()  # Nothing to spell from.
    with pytest.raises(UnsatisfiableConstraints, match="initials='jl' conflict"):
        filler.fill(initials="jl")


class Tagged(BaseModel):
    tag: str = Field(default="none", pattern=r"^[A-Z]+$")  # Pydantic would keep it unchecked.
    label: str = Field(default="OK", pattern=r"^[A-Z]+$")


def test_a_default_that_breaks_the_pattern_is_not_preferred():
    assert fill(Tagged).model_dump() == {"tag": "TAG", "label": "OK"}


class Ranged(BaseModel):
    small: conint(ge=1, le=5)
    window: Annotated[int, Interval(gt=0, lt=3)]
    short: constr(min_length=2, max_length=3)


def test_constraint_groups_are_read():
    assert fill(Ranged).model_dump() == {"small": 1, "window": 1, "short": "sho"}
    assert ModelFiller(Ranged, mode="strict").explain()["unsupported"] == []
    with pytest.raises(UnsatisfiableConstraints, match="small=6 conflict with small <= 5"):
        fill(Ranged, small=6)


class Shouty(BaseModel):
    word: str

    @field_validator("word")
    @classmethod
    def shout(cls, word):
        if not word.isupper():  # A string method: unsupported, so the search falls back.
            raise ValueError("shout")
        return word


def test_running_out_of_readable_strings_keeps_the_context():
    with pytest.raises(GenerationExhausted) as raised:
        fill(Shouty)
    message = str(raised.value)
    for part in ("no readable string remains for 'word'", "unsupported logic", "last rejection"):
        assert part in message


class Digest(BaseModel):
    sha512: str = Field(pattern=r"^[0-9a-f]{128}$")


def test_long_fixed_lengths_are_reached():
    assert fill(Digest).sha512 == "a" * 128
    explored = ModelFiller(Digest)._complete({}, 1, random.Random(0))[0]  # As from_model does.
    assert len(explored.sha512) == 128


def needs_count(code, info):
    if info.data["count"] < 0:
        raise ValueError("the count must be nonnegative")
    return code


class Counted(BaseModel):
    count: int
    code: Annotated[str, AfterValidator(needs_count)] = Field(pattern=r"^[a-z]+$")


class Shouted(BaseModel):
    code: Annotated[str, AfterValidator(str.upper)] = Field(pattern=r"^[a-z]+$")


def test_candidates_are_checked_against_the_pattern_alone():
    # The field's other metadata once ran there too: without the model's data, and
    # requiring the uppercased value to equal the input.
    assert fill(Counted) == Counted(count=0, code="code")
    assert fill(Shouted).code == "CODE"


DEEP = sys.getrecursionlimit() + 100  # One character per level once overflowed the stack.


class Blob(BaseModel):
    data: str = Field(pattern=f"^a{{{DEEP}}}$")


def test_values_longer_than_the_recursion_limit_are_spelled():
    assert fill(Blob).data == "a" * DEEP
