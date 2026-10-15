from dataclasses import InitVar, dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum, Flag, IntEnum, StrEnum, auto
from typing import Annotated, ClassVar, Literal, Optional

import pytest
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from modelfill.adapters import MISSING, introspect
from modelfill.errors import UnsupportedConstraint
from modelfill.ir import Domain


class Color(Enum):
    RED = "red"
    BLUE = "blue"


# Enums whose semantics the solver cannot trust: custom equality or length, or Flags.
class AlwaysEqual(Enum):
    FIRST = 1
    SECOND = 2

    def __eq__(self, other):
        return True


class FakeLength(StrEnum):
    EMPTY = ""

    def __len__(self):
        return 1


class Permissions(Flag):
    READ = auto()
    WRITE = auto()


class Fields(BaseModel):
    number: int = Field(ge=-2, lt=5)
    flag: bool
    color: Color
    choice: Literal["a", "b"]
    text: str = Field(min_length=1, max_length=4)
    default: int = 7


def test_public_field_discovery_and_normal_constructor():
    adapter = introspect(Fields)
    assert [f.name for f in adapter.fields] == list(Fields.model_fields)
    assert adapter.unsupported == ()
    assert len(adapter.constraints) == 4
    assert adapter.fields[-1].preferred() == 7
    assert adapter.fields[0].preferred() is MISSING
    obj = adapter.construct(
        dict(number=2, flag=True, color=Color.RED, choice="a", text="x", default=7)
    )
    assert obj.number == 2
    with pytest.raises(ValueError):
        adapter.construct(
            dict(number=5, flag=True, color=Color.RED, choice="a", text="x", default=7)
        )


@pytest.mark.parametrize(
    "values",
    [
        {"number": True},
        {"number": "3"},
        {"flag": 1},
        {"color": "red"},
        {"choice": "c"},
    ],
)
def test_fixed_inputs_are_canonical(values):
    with pytest.raises(TypeError):
        introspect(Fields).check_fixed(values)


def test_unknown_field_and_valid_partial_input():
    adapter = introspect(Fields)
    adapter.check_fixed({"number": 3, "color": Color.RED})
    with pytest.raises(ValueError, match="Unknown field"):
        adapter.check_fixed({"missing": 3})


class Validators(BaseModel):
    first: int
    second: int

    @field_validator("second", mode="after")
    @classmethod
    def check_field(cls, value, info):
        if value < info.data["first"]:
            raise ValueError("order")
        return value

    @model_validator(mode="after")
    def check_model(self):
        if self.second == 0:
            raise ValueError("zero")
        return self


def test_original_validator_discovery():
    adapter = introspect(Validators)
    assert adapter.unsupported == ()
    model, field_spec = adapter.validators
    assert (model.kind, model.mode, model.name) == ("model", "after", "check_model")
    assert field_spec.field_name == "second"
    assert field_spec.earlier_fields == ("first",)
    assert field_spec.function.__self__ is Validators
    with pytest.raises(ValueError):
        adapter.construct(dict(first=4, second=3))


class InheritedValidators(Validators):
    third: int = 2


def test_inherited_validator_discovery():
    adapter = introspect(InheritedValidators)
    assert len(adapter.validators) == 2
    assert adapter.validators[1].function.__self__ is InheritedValidators


def test_alias_construction_uses_canonical_public_field_names():
    class Aliased(BaseModel):
        count: int = Field(alias="Count")

    adapter = introspect(Aliased)
    adapter.check_fixed({"count": 4})
    assert adapter.construct({"count": 4}).count == 4
    with pytest.raises(ValueError):
        adapter.check_fixed({"Count": 4})


def test_alias_configuration_across_pydantic_versions():
    class Aliased(BaseModel):
        # Before v2.11 these keys are ignored by Pydantic, so aliases still apply.
        model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
        count: int = Field(alias="Count")

    assert introspect(Aliased).construct({"count": 4}).count == 4


def test_complex_aliases_rejected():
    class Aliased(BaseModel):
        count: int = Field(validation_alias=AliasChoices("Count", "count"))

    with pytest.raises(UnsupportedConstraint, match="complex validation aliases"):
        introspect(Aliased)


def test_duplicate_aliases_rejected():
    class Aliased(BaseModel):
        first: int = Field(alias="value")
        second: int = Field(alias="value")

    with pytest.raises(UnsupportedConstraint, match="share the same"):
        introspect(Aliased)


def test_default_factory_is_not_evaluated_during_discovery():
    calls = []

    def factory():
        calls.append(True)
        return 4

    class Defaults(BaseModel):
        count: int = Field(default_factory=factory)

    adapter = introspect(Defaults)
    assert calls == []
    assert adapter.fields[0].preferred() == 4
    assert calls == [True]


def test_data_dependent_factory_diagnostic():
    class Defaults(BaseModel):
        first: int
        second: int = Field(default_factory=lambda data: data["first"])

    adapter = introspect(Defaults)
    assert "data-dependent default factory" in " ".join(adapter.unsupported)
    assert adapter.fields[1].preferred() is MISSING


def test_unchecked_defaults_are_only_those_that_could_skip_a_failing_check():
    class Defaults(BaseModel):
        required: int
        harmless: int = 0
        maybe: int | None = Field(default=None, ge=0)  # Pydantic skips ge for None.
        failing: int = Field(default=-1, ge=0)
        checked: int = Field(default=-1, ge=0, validate_default=True)
        factory: int = Field(default_factory=lambda: 0)  # Unknown until called.
        validated: int = 0

        @field_validator("validated")
        @classmethod
        def positive(cls, value):
            if value <= 0:
                raise ValueError("positive")
            return value

    flags = {field.name: field.unchecked_default for field in introspect(Defaults).fields}
    assert flags == {
        "required": False,
        "harmless": False,
        "maybe": False,
        "failing": True,
        "checked": False,
        "factory": True,
        "validated": True,
    }

    class CheckedDefaults(BaseModel):
        model_config = ConfigDict(validate_default=True)
        checked: int = Field(default=-1, ge=0)
        unchecked: int = Field(default=-1, ge=0, validate_default=False)

    flags = {field.name: field.unchecked_default for field in introspect(CheckedDefaults).fields}
    assert flags == {"checked": False, "unchecked": True}
    assert all(not field.unchecked_default for field in introspect(Data).fields)


def test_unknown_validation_metadata_diagnostic():
    class Custom(BaseModel):
        value: Annotated[int, BeforeValidator(lambda x: x + 1)]

    assert "BeforeValidator" in " ".join(introspect(Custom).unsupported)


def test_global_string_lengths_do_not_apply_to_literals():
    class Lengths(BaseModel):
        model_config = ConfigDict(str_min_length=2, str_max_length=4)
        name: str = Field(min_length=1)
        choice: Literal["x"]

    adapter = introspect(Lengths)
    assert [label for label, _ in adapter.constraints] == ["len(name) >= 1", "len(name) <= 4"]
    assert adapter.construct({"name": "a", "choice": "x"}).choice == "x"


def test_global_string_lengths_set_to_none_impose_nothing():
    class Lengths(BaseModel):
        model_config = ConfigDict(str_min_length=None)
        name: str

    adapter = introspect(Lengths)
    assert (adapter.constraints, adapter.unsupported) == ((), ())


@pytest.mark.parametrize("setting", ["str_to_lower", "str_strip_whitespace", "use_enum_values"])
def test_transforming_configuration_diagnostic(setting):
    class Transform(BaseModel):
        model_config = ConfigDict(**{setting: True})
        value: str

    assert setting in " ".join(introspect(Transform).unsupported)


def test_post_init_diagnostic_and_execution():
    class PostInit(BaseModel):
        value: int

        def model_post_init(self, context):
            self.value += 1

    adapter = introspect(PostInit)
    assert "model_post_init" in " ".join(adapter.unsupported)
    assert adapter.construct({"value": 2}).value == 3


def test_private_attributes_are_supported():
    class Private(BaseModel):
        value: int
        _cache: dict = PrivateAttr(default_factory=dict)

    assert introspect(Private).unsupported == ()


@dataclass
class Data:
    first: int
    second: int = 5
    label: ClassVar[str] = "data"

    def __post_init__(self):
        if self.second <= self.first:
            raise ValueError("order")


def test_dataclass_discovery_and_full_validation():
    adapter = introspect(Data)
    assert [f.name for f in adapter.fields] == ["first", "second"]
    assert adapter.unsupported == ()
    assert adapter.validators[0].kind == "dataclass"
    assert adapter.fields[-1].preferred() == 5
    assert adapter.construct({"first": 3, "second": 4}) == Data(3, 4)
    with pytest.raises(ValueError, match="order"):
        adapter.construct({"first": 4, "second": 3})


def test_frozen_slots_dataclass_supported():
    @dataclass(frozen=True, slots=True, kw_only=True)
    class Frozen:
        value: int = field(default_factory=lambda: 9)

    adapter = introspect(Frozen)
    assert adapter.unsupported == ()
    assert adapter.fields[0].preferred() == 9
    assert adapter.construct({"value": 2}).value == 2


def test_custom_dataclass_init_diagnostic():
    @dataclass
    class Custom:
        value: int

        def __init__(self, value):
            self.value = value + 1

    assert "Custom dataclass __init__" in " ".join(introspect(Custom).unsupported)


def test_init_false_dataclass_field_rejected():
    @dataclass
    class Derived:
        value: int = field(default=1, init=False)

    with pytest.raises(UnsupportedConstraint, match="init=False"):
        introspect(Derived)


def test_init_var_rejected():
    @dataclass
    class Input:
        value: int
        initial: InitVar[int]

    with pytest.raises(UnsupportedConstraint, match="InitVar"):
        introspect(Input)


@pytest.mark.parametrize(
    "annotation",
    [
        float,
        list[int],
        int | str,
        Literal[1, "x"],
        Literal["a", chr(0x30000)],  # Z3 strings stop at U+2FFFF.
        None,
        AlwaysEqual,
        FakeLength,
        Permissions,
    ],
)
def test_unsupported_types_become_external_fields(annotation):
    @dataclass
    class Unsupported:
        value: annotation

    adapter = introspect(Unsupported)
    assert adapter.fields[0].domain is None
    adapter.check_fixed({"value": object()})  # The constructor validates external values.
    with pytest.raises(UnsupportedConstraint, match="cannot generate value"):
        adapter.check_fixed({})


def test_optional_literal_and_time_domains():
    class Types(BaseModel):
        maybe: int | None = None
        legacy: Optional[bool]  # noqa: UP045
        choice: Literal["a", "b", None]
        day: date
        moment: datetime
        span: timedelta | None
        bounded: datetime | None = Field(default=None, ge=datetime(2025, 1, 1))

    adapter = introspect(Types)
    assert {field.name: field.domain for field in adapter.fields} == {
        "maybe": Domain("int", nullable=True),
        "legacy": Domain("bool", nullable=True),
        "choice": Domain("str", ("a", "b"), nullable=True),
        "day": Domain("date"),
        "moment": Domain("datetime"),
        "span": Domain("timedelta", nullable=True),
        "bounded": Domain("datetime", nullable=True),
    }
    assert adapter.unsupported == ()
    bound = "bounded >= datetime.datetime(2025, 1, 1, 0, 0)"
    assert [(label, rule.op) for label, rule in adapter.constraints] == [(bound, "or")]


def test_invalid_model_argument():
    with pytest.raises(TypeError, match="model class"):
        introspect(Data(1))
    with pytest.raises(TypeError, match="supports Pydantic"):
        introspect(int)


def test_custom_attribute_access_diagnostic():
    @dataclass
    class Custom:
        value: int

        def __getattribute__(self, name):
            return object.__getattribute__(self, name)

    assert "__getattribute__" in " ".join(introspect(Custom).unsupported)


@pytest.mark.parametrize("base", [Enum, IntEnum, StrEnum])
def test_standard_enum_domains_supported(base):
    enum_type = base("Choices", {"FIRST": "a" if base is StrEnum else 1})

    @dataclass
    class WithEnum:
        value: enum_type

    assert introspect(WithEnum).unsupported == ()
