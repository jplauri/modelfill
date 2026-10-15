"""Model discovery and normal construction; no solver dependency.

Pydantic's original decorator functions are not exposed by its public field API.
That compatibility-sensitive discovery is confined to ``_pydantic_validators``;
the only other private API is the guarded ``init_private_attributes`` import.
"""

from __future__ import annotations

import dataclasses
import inspect
import math
import types
from collections.abc import Callable
from copy import deepcopy
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Union, get_args, get_origin, get_type_hints

from annotated_types import Ge, Gt, Le, Lt, MaxLen, MinLen, MultipleOf
from pydantic import BaseModel, ConfigDict
from pydantic.types import Strict
from pydantic_core import PydanticUndefined

from .errors import UnsupportedConstraint
from .ir import (
    INT,
    PRIMITIVES,
    TIME_TYPES,
    Domain,
    Expr,
    const,
    encodable,
    evaluate,
    node,
    plain_enum,
    var,
)

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

try:  # If a future Pydantic moves it, PrivateAttr models just fall back to guidance.
    from pydantic._internal._model_construction import init_private_attributes
except ImportError:
    init_private_attributes = None

MISSING = dataclasses.MISSING


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    name: str
    domain: Domain | None  # None: an external field, supplied by the caller or its default.
    default: Any
    default_factory: Callable[..., Any] | None = None  # Never data-dependent here.
    input_name: str | None = None
    unchecked_default: bool = False
    required: bool = True
    annotation: Any = None

    def preferred(self) -> Any:
        """Evaluate an ordinary default once per fill; it is a preference."""
        if self.default_factory is not None:
            return self.default_factory()
        return MISSING if self.default is MISSING else deepcopy(self.default)


@dataclasses.dataclass(frozen=True)
class ValidatorSpec:
    name: str
    function: Callable[..., Any]
    kind: Literal["model", "dataclass", "field"]
    mode: str
    field_name: str | None = None
    earlier_fields: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Adapter:
    model: type
    fields: tuple[FieldSpec, ...]
    constraints: tuple[tuple[str, Expr], ...]  # (label, rule) for explaining contradictions
    validators: tuple[ValidatorSpec, ...]
    unsupported: tuple[str, ...]

    def construct(self, values: dict[str, Any]) -> Any:
        """Run the model's real constructor, including its original validators."""
        inputs = {
            field.input_name or field.name: values[field.name]
            for field in self.fields
            if field.name in values
        }
        return self.model(**inputs)

    def check_fixed(self, values: dict[str, Any]) -> None:
        """The public API takes canonical field names and exact Python types."""
        fields = {field.name: field for field in self.fields}
        unknown = values.keys() - fields.keys()
        if unknown:
            raise ValueError(f"Unknown field(s): {', '.join(sorted(unknown))}")
        for name, value in values.items():
            domain = fields[name].domain
            if domain is not None and not domain.accepts(value):
                expected = domain.kind + (" or None" if domain.nullable else "")
                raise TypeError(
                    f"Fixed field {name!r} requires a canonical {expected} value, got {value!r}"
                )
        missing = [
            field.name
            for field in self.fields
            if field.domain is None and field.required and field.name not in values
        ]
        if missing:
            raise UnsupportedConstraint(
                f"modelfill cannot generate {', '.join(missing)}; pass values for them"
            )


def _domain(annotation: Any) -> Domain | None:
    """The solver domain of a field type, or None for an external field."""
    if annotation in PRIMITIVES:
        return PRIMITIVES[annotation]
    arguments = get_args(annotation)
    if get_origin(annotation) in (Union, types.UnionType) and type(None) in arguments:
        options = [option for option in arguments if option is not type(None)]
        inner = _domain(options[0]) if len(options) == 1 else None
        return None if inner is None else dataclasses.replace(inner, nullable=True)
    if inspect.isclass(annotation) and issubclass(annotation, Enum) and plain_enum(annotation):
        return Domain("enum", tuple(annotation)) if len(annotation) else None
    if get_origin(annotation) is Literal:
        values = tuple(choice for choice in arguments if choice is not None)
        homogeneous = values and all(type(x) is type(values[0]) for x in values)
        if homogeneous and all(encodable(value) for value in values):
            base = _domain(type(values[0]))
            if base is not None:
                return Domain(base.kind, values, nullable=len(values) < len(arguments))
    return None


_SYMBOLS = {Gt: ">", Ge: ">=", Lt: "<", Le: "<="}


def _metadata_constraints(
    name: str, domain: Domain, metadata: list[Any], unsupported: list[str]
) -> list[tuple[str, Expr]]:
    """A field's own constraints, as (label, rule) pairs."""
    result: list[tuple[str, Expr]] = []
    expression = var(name, domain)
    # Pydantic keeps the last constraint of each kind, with Field() before Annotated[...]:
    # Field(ge=10) on an Annotated[int, Ge(0)] alias leaves ge=0 in force.
    for item in {type(item): item for item in metadata}.values():
        if isinstance(item, Strict):
            continue  # All generated/fixed inputs already have their canonical type.
        if isinstance(item, (Gt, Ge, Lt, Le)) and domain.kind in TIME_TYPES:
            operation = type(item).__name__.lower()
            bound = getattr(item, operation)
            if type(bound) is not TIME_TYPES[domain.kind] or getattr(bound, "tzinfo", None):
                unsupported.append(f"Field {name!r}: unsupported {domain.kind} bound {bound!r}")
                continue
            label = f"{name} {_SYMBOLS[type(item)]} {bound!r}"
            result.append((label, node(operation, expression, const(bound))))
        elif isinstance(item, MultipleOf) and domain.kind == "int":
            step = item.multiple_of
            if type(step) is not int or step < 1:
                unsupported.append(f"Field {name!r}: unsupported multiple_of {step!r}")
                continue
            remainder = node("mod", expression, const(step), sort=INT)
            result.append((f"{name} % {step} == 0", node("eq", remainder, const(0))))
        elif isinstance(item, (Gt, Ge, Lt, Le)) and domain.kind == "int":
            bound = getattr(item, type(item).__name__.lower())
            if type(bound) not in (int, float) or (
                type(bound) is float and not math.isfinite(bound)
            ):
                unsupported.append(f"Field {name!r}: unsupported numeric bound {bound!r}")
                continue
            if isinstance(item, Gt):
                operation, value = "ge", math.floor(bound) + 1
            elif isinstance(item, Ge):
                operation, value = "ge", math.ceil(bound)
            elif isinstance(item, Lt):
                operation, value = "le", math.ceil(bound) - 1
            else:
                operation, value = "le", math.floor(bound)
            label = f"{name} {_SYMBOLS[type(item)]} {bound!r}"
            result.append((label, node(operation, expression, const(value))))
        elif isinstance(item, (MinLen, MaxLen)) and domain.kind == "str":
            minimum = isinstance(item, MinLen)
            value = item.min_length if minimum else item.max_length
            if type(value) is not int:
                unsupported.append(f"Field {name!r}: unsupported length bound {value!r}")
                continue
            symbol, operation = (">=", "ge") if minimum else ("<=", "le")
            length = node("len", expression, sort=INT)
            result.append((f"len({name}) {symbol} {value}", node(operation, length, const(value))))
        else:
            unsupported.append(f"Field {name!r}: unsupported metadata {item!r}")
    if domain.nullable:  # Pydantic applies these constraints to non-None values only.
        return [(label, node("or", node("isnone", expression), rule)) for label, rule in result]
    return result


def _pydantic_validators(
    model: type[BaseModel], names: tuple[str, ...], unsupported: list[str]
) -> tuple[ValidatorSpec, ...]:
    """Validators from Pydantic's private decorator registry, isolated here."""
    decorators = getattr(model, "__pydantic_decorators__", None)
    if decorators is None:
        unsupported.append("Pydantic decorator registry is unavailable")
        return ()
    validators = []
    for name, decorator in decorators.model_validators.items():
        validators.append(ValidatorSpec(name, decorator.func, "model", decorator.info.mode))
    for name, decorator in decorators.field_validators.items():
        targets = decorator.info.fields
        for index, field_name in enumerate(names):
            if field_name in targets or "*" in targets:
                validators.append(
                    ValidatorSpec(
                        name,
                        decorator.func,
                        "field",
                        decorator.info.mode,
                        field_name,
                        names[:index],
                    )
                )
    if decorators.validators or decorators.root_validators:
        unsupported.append("Legacy Pydantic v1-style validators are unsupported")
    return tuple(validators)


# Configuration that cannot transform canonical values or bypass validation.
_SAFE_CONFIG = {
    "title",
    "model_title_generator",
    "field_title_generator",
    "extra",
    "frozen",
    "populate_by_name",
    "validate_by_alias",
    "validate_by_name",
    "loc_by_alias",
    "alias_generator",
    "validate_assignment",
    "arbitrary_types_allowed",
    "from_attributes",
    "ignored_types",
    "json_schema_extra",
    "json_encoders",
    "strict",
    "revalidate_instances",
    "validate_default",
    "protected_namespaces",
    "hide_input_in_errors",
    "defer_build",
    "schema_generator",
    "json_schema_serialization_defaults_required",
    "json_schema_mode_override",
    "validation_error_cause",
    "use_attribute_docstrings",
    "cache_strings",
    "serialize_by_alias",
    "url_preserve_empty_path",
}


def _pydantic(model: type[BaseModel]) -> Adapter:
    unsupported: list[str] = []
    constraints: list[tuple[str, Expr]] = []
    fields = []
    config = model.model_config
    for key, value in config.items():
        if key in ("str_min_length", "str_max_length") or key in _SAFE_CONFIG:
            continue
        if key in ("str_to_lower", "str_to_upper", "str_strip_whitespace", "use_enum_values"):
            if not value:
                continue
        unsupported.append(f"Unsupported Pydantic configuration: {key}={value!r}")
    if model.__init__ is not BaseModel.__init__:
        unsupported.append("Custom Pydantic __init__ is unsupported")
    if model.__new__ is not BaseModel.__new__:
        unsupported.append("Custom Pydantic __new__ is unsupported")
    for hook in ("__getattribute__", "__getattr__", "__setattr__"):
        if getattr(model, hook, None) is not getattr(BaseModel, hook, None):
            unsupported.append(f"Custom Pydantic {hook} is unsupported")
    if type(model).__call__ is not type(BaseModel).__call__:
        unsupported.append("Custom Pydantic metaclass __call__ is unsupported")
    # Pydantic installs init_private_attributes for PrivateAttr; it never touches fields.
    if model.model_post_init not in (BaseModel.model_post_init, init_private_attributes):
        unsupported.append("Pydantic model_post_init is unsupported")
    for parent in model.__mro__:
        if "__get_pydantic_core_schema__" in parent.__dict__:
            if parent is not BaseModel:
                unsupported.append("Custom Pydantic core schema is unsupported")
            break
    validators = _pydantic_validators(model, tuple(model.model_fields), unsupported)
    # A field validator could reject a default that the field's constraints accept.
    validated = {validator.field_name for validator in validators if validator.field_name}
    # Validation aliases do not apply when validate_by_alias=False.
    by_alias = (
        config.get("validate_by_alias", True)
        if "validate_by_alias" in ConfigDict.__annotations__
        else True
    )
    for name, info in model.model_fields.items():
        domain = _domain(info.annotation)
        alias = info.validation_alias
        if alias is not None and not isinstance(alias, str):
            raise UnsupportedConstraint(
                f"Field {name!r}: complex validation aliases are unsupported"
            )
        input_name = alias if by_alias else name
        factory = info.default_factory
        if info.default_factory_takes_validated_data:  # None without a factory.
            unsupported.append(f"Field {name!r}: data-dependent default factory is unsupported")
            factory = None
        default = MISSING if info.default is PydanticUndefined else info.default
        validates_default = (
            info.validate_default
            if info.validate_default is not None
            else config.get("validate_default", False)
        )
        rules: list[tuple[str, Expr]] = []
        if domain is not None:  # Pydantic itself validates values of external fields.
            metadata = list(info.metadata)
            if domain.kind == "str" and domain.choices is None:
                # A field's own length limit overrides the global configuration.
                shortest, longest = config.get("str_min_length"), config.get("str_max_length")
                if shortest is not None and not any(isinstance(m, MinLen) for m in metadata):
                    metadata.append(MinLen(shortest))
                if longest is not None and not any(isinstance(m, MaxLen) for m in metadata):
                    metadata.append(MaxLen(longest))
            rules = _metadata_constraints(name, domain, metadata, unsupported)
            constraints.extend(rules)
        # Pydantic skips field validation for an omitted default; that only matters
        # when the validation would have rejected the default.
        passes = (
            domain is not None
            and default is not MISSING
            and name not in validated
            and domain.accepts(default)
            and encodable(default)
            and all(evaluate(rule, {name: default}) for _, rule in rules)
        )
        unchecked = not info.is_required() and not validates_default and not passes
        fields.append(
            FieldSpec(
                name,
                domain,
                default,
                factory,
                input_name,
                unchecked,
                info.is_required(),
                info.annotation,
            )
        )
    inputs = [field.input_name or field.name for field in fields]
    if len(set(inputs)) != len(inputs):
        raise UnsupportedConstraint("Multiple fields share the same validation alias")
    return Adapter(model, tuple(fields), tuple(constraints), validators, tuple(unsupported))


def _dataclass(model: type[DataclassInstance]) -> Adapter:
    unsupported: list[str] = []
    try:
        annotations = get_type_hints(model)
    except (NameError, TypeError) as exc:
        raise UnsupportedConstraint(f"Cannot resolve dataclass annotations: {exc}") from exc
    if any(isinstance(annotation, dataclasses.InitVar) for annotation in annotations.values()):
        raise UnsupportedConstraint("Dataclass InitVar fields are unsupported")
    initializer = model.__init__
    if not hasattr(initializer, "__code__") or initializer.__code__.co_filename != "<string>":
        unsupported.append("Custom dataclass __init__ is unsupported")
    if model.__new__ is not object.__new__:
        unsupported.append("Custom dataclass __new__ is unsupported")
    for hook in ("__getattribute__", "__getattr__"):
        if getattr(model, hook, None) is not getattr(object, hook, None):
            unsupported.append(f"Custom dataclass {hook} is unsupported")
    if type(model).__call__ is not type.__call__:
        unsupported.append("Custom dataclass metaclass __call__ is unsupported")
    frozen = model.__dataclass_params__.frozen  # ty: ignore[unresolved-attribute]
    if model.__setattr__ is not object.__setattr__ and not frozen:
        unsupported.append("Custom dataclass __setattr__ is unsupported")
    fields = []
    for info in dataclasses.fields(model):
        if not info.init:
            raise UnsupportedConstraint(f"Dataclass init=False field {info.name!r} is unsupported")
        annotation = annotations[info.name]
        domain = _domain(annotation)
        factory = None if info.default_factory is MISSING else info.default_factory
        required = info.default is MISSING and factory is None
        fields.append(
            FieldSpec(
                info.name, domain, info.default, factory, required=required, annotation=annotation
            )
        )
        descriptor = inspect.getattr_static(model, info.name, None)
        if hasattr(descriptor, "__set__") and not isinstance(
            descriptor, types.MemberDescriptorType
        ):
            unsupported.append(f"Dataclass field {info.name!r} uses a custom descriptor")
    post_init = getattr(model, "__post_init__", None)
    # A generated __init__ calls __post_init__ only if one existed when @dataclass ran,
    # so the hook of a subclass that was not decorated again never runs.
    code = getattr(initializer, "__code__", None)
    runs = post_init is not None and code is not None and "__post_init__" in code.co_names
    validators = (ValidatorSpec("__post_init__", post_init, "dataclass", "after"),) if runs else ()
    return Adapter(model, tuple(fields), (), validators, tuple(unsupported))


def introspect(model: type) -> Adapter:
    """Discover supported domains and all known validation hooks conservatively."""
    if not inspect.isclass(model):
        raise TypeError("modelfill expects a model class, not an instance")
    if issubclass(model, BaseModel):
        return _pydantic(model)
    if dataclasses.is_dataclass(model):
        if hasattr(model, "__pydantic_validator__"):
            raise UnsupportedConstraint(
                "Pydantic dataclasses are unsupported; use a standard dataclass"
            )
        return _dataclass(model)
    raise TypeError("modelfill supports Pydantic BaseModel and standard dataclass classes")
