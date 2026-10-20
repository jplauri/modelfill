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
from collections.abc import Callable, Iterable
from copy import deepcopy
from decimal import Decimal, getcontext
from enum import Enum
from fractions import Fraction
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    NewType,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from annotated_types import (
    Ge,
    GroupedMetadata,
    Gt,
    Le,
    Lt,
    MaxLen,
    MinLen,
    MultipleOf,
    Not,
    Predicate,
)
from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    PlainSerializer,
    RootModel,
    Tag,
    UrlConstraints,
    ValidationError,
    WrapSerializer,
)
from pydantic.fields import FieldInfo
from pydantic.types import PathType, Strict, UuidVersion
from pydantic_core import PydanticUndefined

from .errors import InputTypeError, InputValueError, UnsupportedConstraint
from .ir import (
    INT,
    PRIMITIVES,
    TIME_TYPES,
    Domain,
    Expr,
    const,
    decode,
    encodable,
    evaluate,
    node,
    plain_enum,
    var,
)
from .patterns import Pattern

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
    input_path: tuple[str, ...] = ()  # Where the constructor reads it, if not by name.
    unchecked_default: bool = False
    required: bool = True
    annotation: Any = None
    metadata: tuple[Any, ...] = ()  # Pydantic's constraints, for boring values of external fields.
    pattern: Pattern | None = None  # A string field's pattern, checked outside the solver.

    @property
    def path(self) -> tuple[str, ...]:
        """Its name, its validation alias, even an empty one, or an AliasPath's keys."""
        return self.input_path or (self.name,)

    @property
    def keyword(self) -> str:
        """The constructor's keyword for the field."""
        return self.path[0]

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
    constants: dict[str, Any] = dataclasses.field(default_factory=dict)  # Its ClassVars.

    def inputs(self, values: dict[str, Any]) -> dict[str, Any]:
        """The constructor's keyword arguments for these field values, by input path."""
        inputs: dict[str, Any] = {}
        for field in self.fields:
            if field.name in values:
                *outer, last = field.path
                target = inputs
                for key in outer:  # An AliasPath reads the value from nested dicts.
                    target = target.setdefault(key, {})
                target[last] = values[field.name]
        return inputs

    def construct(self, values: dict[str, Any]) -> Any:
        """Run the model's real constructor, including its original validators."""
        return self.model(**self.inputs(values))

    @property
    def rejections(self) -> tuple[type[Exception], ...]:
        """What the constructor raises to turn values down. Pydantic wraps its validators'
        errors in ValidationError, so any other error it lets out, such as a default
        factory's, is not a rejection; a dataclass's __post_init__ raises them itself."""
        if issubclass(self.model, BaseModel):
            return (ValidationError,)
        return (ValueError, AssertionError)

    def check_fixed(self, values: dict[str, Any]) -> None:
        """The public API takes canonical field names and exact Python types."""
        fields = {field.name: field for field in self.fields}
        unknown = values.keys() - fields.keys()
        if unknown:
            raise InputValueError(f"Unknown field(s): {', '.join(sorted(unknown))}")
        for name, value in values.items():
            domain = fields[name].domain
            if domain is not None and not domain.accepts(value):
                expected = domain.kind + (" or None" if domain.nullable else "")
                raise InputTypeError(
                    f"Fixed field {name!r} requires a canonical {expected} value, got {value!r}"
                )


def _domain(annotation: Any, grid: Domain | None = None) -> Domain | None:
    """The solver domain of a field type, or None for an external field. A Decimal
    has the grid its decimal_places set, if any."""
    if annotation in PRIMITIVES:
        return PRIMITIVES[annotation]
    if annotation is Decimal:
        return grid
    arguments = get_args(annotation)
    if get_origin(annotation) in (Union, types.UnionType) and type(None) in arguments:
        options = [option for option in arguments if option is not type(None)]
        inner = _domain(options[0], grid) if len(options) == 1 else None
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
# Pydantic's general metadata settings modelfill reads, by kind; any other is code,
# such as to_upper, or a setting the solver does not model.
_SETTINGS = {"decimal": {"max_digits", "decimal_places"}, "str": {"pattern"}}
# Pydantic's class for constraints that annotated-types lacks, such as a pattern.
_GENERAL = type(Field(pattern="x").metadata[0])


# Pydantic markers whose schema hooks only constrain, tag or serialize a value. Being
# Pydantic's is not enough: ValidateAs, say, runs a function it is given.
_MARKERS = (PathType, PlainSerializer, Tag, UrlConstraints, UuidVersion, WrapSerializer)
# The packages whose types' schema hooks only check their own value, such as EmailStr's.
_PYDANTIC = ("pydantic", "pydantic_core")


def _runs_code(item: Any) -> bool:
    """Whether Pydantic could run code for this unpacked metadata, beyond checking it:
    anything with a schema hook but those markers, and annotated-types predicates."""
    if isinstance(item, Discriminator):
        return not isinstance(item.discriminator, str)  # A function picks the variant.
    return isinstance(item, (Predicate, Not)) or (
        hasattr(item, "__get_pydantic_core_schema__") and not isinstance(item, _MARKERS)
    )


def _foreign_code(annotation: Any, outer: tuple[Any, ...] = ()) -> list[str]:
    """Code that Pydantic would run for a value of this type beyond the field's own
    metadata: a schema hook of a type from outside Pydantic, and code in metadata deeper
    in the type, at any depth. Such code gets the model's data so far, which it may
    change. A model, whose code gets its own data, ends the search."""
    annotation, metadata = _resolve(annotation)
    if any(annotation is seen for seen in outer):  # A recursive type, read once.
        return []
    found = [repr(item) for item in _flatten(metadata) if _runs_code(item)] if outer else []
    origin = annotation if inspect.isclass(annotation) else get_origin(annotation)
    if inspect.isclass(origin):  # A generic alias, such as Box[int], runs its class's hooks.
        if issubclass(origin, BaseModel) or dataclasses.is_dataclass(origin):
            return found
        hooks = ("__get_pydantic_core_schema__", "__get_validators__")
        owner = next((cls for cls in origin.__mro__ if set(hooks) & vars(cls).keys()), None)
        if owner is not None and owner.__module__.partition(".")[0] not in _PYDANTIC:
            found.append(owner.__qualname__)
    outer += (annotation,)
    return found + [code for arg in get_args(annotation) for code in _foreign_code(arg, outer)]


def _flatten(metadata: Iterable[Any]) -> list[Any]:
    """Metadata as Pydantic reads it: groups such as StringConstraints, Interval and Len
    unpacked into their single items, at any depth; constr() and conint() leave None
    for each option not given."""
    items: list[Any] = []
    for item in metadata:
        if isinstance(item, GroupedMetadata):
            items += _flatten(item)
        elif item is not None:
            items.append(item)
    return items


def _resolve(annotation: Any) -> tuple[Any, list[Any]]:
    """A type without its NewType, type alias and Annotated layers, and the metadata they
    carry, innermost first, as Pydantic merges it ahead of a field's own."""
    if isinstance(annotation, NewType):
        return _resolve(annotation.__supertype__)
    if isinstance(annotation, TypeAliasType):
        return _resolve(annotation.__value__)
    if get_origin(annotation) is Annotated:
        base, *outer = get_args(annotation)
        inner, metadata = _resolve(base)
        for item in outer:  # A Field() in Annotated carries its constraints as metadata.
            metadata += item.metadata if isinstance(item, FieldInfo) else [item]
        return inner, metadata
    arguments = get_args(annotation)
    if get_origin(annotation) in (Union, types.UnionType) and type(None) in arguments:
        options = [option for option in arguments if option is not type(None)]
        if len(options) == 1:
            inner, metadata = _resolve(options[0])
            # Pydantic checks these on the value, never on None, as modelfill does an
            # Optional field's own constraints. But an AfterValidator there never sees
            # None, unlike one on the field, so that layer stays as it is.
            if not any(isinstance(item, AfterValidator) for item in _flatten(metadata)):
                return inner | None, metadata
    return annotation, []


def _input_path(name: str, alias: Any, by_alias: bool, by_name: bool) -> tuple[str, ...]:
    """Where the constructor reads a field: its name, its alias, or an AliasPath's keys
    into nested dicts; of AliasChoices, the first choice that is one of these."""
    if alias is None or not by_alias:
        return (name,)
    for choice in alias.choices if isinstance(alias, AliasChoices) else [alias]:
        path = (choice,) if isinstance(choice, str) else tuple(choice.path)
        if all(isinstance(key, str) for key in path):
            return path
    if by_name:  # Pydantic takes the field's name as well.
        return (name,)
    raise UnsupportedConstraint(f"Field {name!r}: an alias path through a list is unsupported")


def _applied(metadata: Iterable[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """The metadata items that hold, AfterValidators aside, and the general settings
    that do, as Pydantic applies them. Before any callback, it sets them on the schema
    itself: the last item of each kind holds, and of its general metadata, such as a
    pattern or to_upper, the last of each setting. Past a callback, it checks each item
    on its own on the callback's result, so every one holds."""
    items: list[Any] = []
    generals: list[dict[str, Any]] = [{}]
    first = True  # No callback yet.
    for item in metadata:
        if isinstance(item, AfterValidator):
            first = False
        elif type(item) is _GENERAL:
            settings = {key: value for key, value in vars(item).items() if value is not None}
            if first:
                generals[0] |= settings
            else:
                generals.append(settings)
        else:
            if first:
                items = [other for other in items if type(other) is not type(item)]
            items.append(item)
    return items, generals


def _grid(metadata: list[Any]) -> Domain | None:
    """The domain of a Decimal that decimal_places puts on a grid, from the settings
    before any callback, which Pydantic checks together. The last of each holds, and
    fewer digits than places bound the places too: max_digits=2 with decimal_places=3
    allows 0.99 but not 0.099. Past a callback it checks each alone, unmodeled here."""
    settings = _applied(metadata)[1][0]
    places, digits = settings.get("decimal_places"), settings.get("max_digits")
    if type(places) is not int or places < 0:
        return None
    if digits is not None and (type(digits) is not int or digits < 1):
        return None
    return Domain("decimal", scale=places if digits is None else min(places, digits), digits=digits)


def _exact(number: Any, kind: str) -> Fraction | None:
    """A finite numeric constraint's exact value. For a Decimal, Pydantic reads a float
    as its repr, so ge=0.1 allows 0.1; an int is compared with the float itself."""
    if type(number) is float and math.isfinite(number):
        return Fraction(repr(number) if kind == "decimal" else number)
    if type(number) is int or (
        kind == "decimal" and type(number) is Decimal and number.is_finite()
    ):
        return Fraction(number)
    return None


def _metadata_constraints(
    name: str, domain: Domain, metadata: list[Any], unsupported: list[str]
) -> tuple[list[tuple[str, Expr]], Any]:
    """A field's own constraints, as (label, rule) pairs, and its pattern, or None,
    from its flattened metadata, AfterValidators included."""
    pattern = None
    expression = var(name, domain)
    # Pydantic keeps the last constraint of each kind, with Field() before Annotated[...]:
    # Field(ge=10) on an Annotated[int, Ge(0)] alias leaves ge=0 in force.
    items, generals = _applied(metadata)
    allowed = _SETTINGS.get(domain.kind, set())  # The domain or the pattern holds these.
    settings = {key for general in generals for key, value in general.items() if value is not False}
    unmodeled = settings - allowed  # Code, such as to_upper, whose result a pattern may check.
    if domain.kind == "decimal":  # The grid holds those checked together, before callbacks.
        unmodeled |= {f"{key} past a callback" for g in generals[1:] for key in g.keys() & allowed}
    if unmodeled:
        unsupported.append(f"Field {name!r}: unsupported metadata {', '.join(sorted(unmodeled))}")
    elif patterns := [general["pattern"] for general in generals if "pattern" in general]:
        pattern = patterns[-1]  # Only checked: it never transforms a value.
    result = _rules(name, domain, expression, items, unsupported)
    if domain.digits is not None:  # Pydantic counts digits: |units| < 10**max_digits.
        low, high = (
            const(decode("decimal", sign * 10**domain.digits, domain.scale)) for sign in (-1, 1)
        )
        within = node("and", node("lt", low, expression), node("lt", expression, high))
        result.append((f"{name} has at most {domain.digits} digits", within))
    if domain.nullable:  # Pydantic applies these constraints to non-None values only.
        result = [(label, node("or", node("isnone", expression), rule)) for label, rule in result]
    return result, pattern


def _rules(
    name: str, domain: Domain, expression: Expr, items: Iterable[Any], unsupported: list[str]
) -> list[tuple[str, Expr]]:
    """The rules of one stage's metadata items, apart from Pydantic's general metadata."""
    result: list[tuple[str, Expr]] = []
    for item in items:
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
        elif isinstance(item, (Gt, Ge, Lt, Le)) and domain.kind in ("int", "decimal"):
            bound = getattr(item, type(item).__name__.lower())
            exact = _exact(bound, domain.kind)
            if exact is None:
                unsupported.append(f"Field {name!r}: unsupported numeric bound {bound!r}")
                continue
            units = exact * 10**domain.scale  # The bound's place on the grid.
            if isinstance(item, Gt):
                operation, value = "ge", math.floor(units) + 1
            elif isinstance(item, Ge):
                operation, value = "ge", math.ceil(units)
            elif isinstance(item, Lt):
                operation, value = "le", math.ceil(units) - 1
            else:
                operation, value = "le", math.floor(units)
            label = f"{name} {_SYMBOLS[type(item)]} {bound!r}"
            limit = const(decode(domain.kind, value, domain.scale))
            result.append((label, node(operation, expression, limit)))
        elif isinstance(item, MultipleOf) and domain.kind == "decimal":
            step = _exact(item.multiple_of, "decimal")
            if step is None or step <= 0:
                unsupported.append(f"Field {name!r}: unsupported multiple_of {item.multiple_of!r}")
                continue
            # A multiple's units are multiples of these: 0.25 on a grid of tenths leaves
            # multiples of 0.5.
            units = step * 10**domain.scale
            # The least quotient but 0 is 1 / units. Below 10**Emin it is subnormal, which
            # rounds more coarsely, to 0 even, and may pass as a multiple: not modeled.
            emin = getcontext().Emin
            if len(str(units.numerator)) > -emin and units > 10**-emin:
                unsupported.append(f"Field {name!r}: multiple_of could round past the context")
                continue
            divisor = const(decode("decimal", units.numerator, domain.scale))
            remainder = node("mod", expression, divisor, sort=Domain("decimal", scale=domain.scale))
            # Pydantic divides by the step in the context's precision: a quotient past it
            # rounds, and may pass without being a multiple, so Pydantic decides those.
            edge = math.ceil(Fraction(10 ** (getcontext().prec - 1), units.denominator))
            rule = node(
                "or",
                node("ge", expression, const(decode("decimal", edge, domain.scale))),
                node("le", expression, const(decode("decimal", -edge, domain.scale))),
                node("eq", remainder, const(Decimal(0))),
            )
            result.append((f"{name} % {item.multiple_of} == 0", rule))
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
    "regex_engine",  # Patterns are checked by Pydantic itself, with the model's engine.
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
    if model.__init__ not in (BaseModel.__init__, RootModel.__init__):  # Pydantic's own.
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
    names = tuple(model.model_fields)
    validators = list(_pydantic_validators(model, names, unsupported))
    # A field validator could reject a default that the field's constraints accept.
    validated = {validator.field_name for validator in validators if validator.field_name}
    # Validation aliases do not apply when validate_by_alias=False.
    by_alias = (
        config.get("validate_by_alias", True)
        if "validate_by_alias" in ConfigDict.__annotations__
        else True
    )
    by_name = bool(config.get("validate_by_name") or config.get("populate_by_name"))
    for index, (name, info) in enumerate(model.model_fields.items()):
        annotation, inherited = _resolve(info.annotation)
        # A RootModel takes its value as root, whatever alias the field has.
        alias = None if issubclass(model, RootModel) else info.validation_alias
        input_path = _input_path(name, alias, by_alias, by_name)
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
        pattern = None
        # Unpacked before anything reads it, so a group's own length limits override
        # the global ones, as in Pydantic.
        staged = _flatten([*inherited, *info.metadata])
        # An AfterValidator gets the field's validated value, as a field validator does,
        # so it is analyzed as one: a pure check is modeled, anything else unsupported.
        # Unlike a constraint of one kind, every one of them runs.
        checks = [item.func for item in staged if isinstance(item, AfterValidator)]
        metadata = [item for item in staged if not isinstance(item, AfterValidator)]
        domain = _domain(annotation, _grid(staged))
        validators += [
            ValidatorSpec(
                getattr(check, "__name__", repr(check)),
                check,
                "field",
                "after",
                name,
                names[:index],
            )
            for check in checks
        ]
        if checks:
            validated.add(name)
        if domain is None:
            # Pydantic validates an external field itself, but other code in its metadata
            # could replace a value that a rule reads, a required nested model's say, so it
            # is unsupported logic, as a transforming field validator of the field would be.
            unsupported += [
                f"Field {name!r}: unsupported metadata {item!r}"
                for item in metadata
                if _runs_code(item)
            ]
            unsupported += [
                f"Field {name!r}: {code} runs its own validation code"
                for code in _foreign_code(info.annotation)
            ]
        else:
            if domain.kind == "str" and domain.choices is None:
                # A field's own length limit overrides the global configuration.
                shortest, longest = config.get("str_min_length"), config.get("str_max_length")
                if shortest is not None and not any(isinstance(m, MinLen) for m in metadata):
                    staged.append(MinLen(shortest))
                if longest is not None and not any(isinstance(m, MaxLen) for m in metadata):
                    staged.append(MaxLen(longest))
            rules, regex = _metadata_constraints(name, domain, staged, unsupported)
            constraints.extend(rules)
            if regex is not None:
                pattern = Pattern(regex, config.get("regex_engine"))
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
                input_path,
                unchecked,
                info.is_required(),
                info.annotation,
                tuple(metadata),
                pattern,
            )
        )
    paths = [field.path for field in fields]  # Fields may share a dict, not a key in it.
    if len(set(paths)) < len(paths) or any(
        a != b and a[: len(b)] == b for a in paths for b in paths
    ):
        raise UnsupportedConstraint("Multiple fields share the same validation alias")
    if dotted := [field.name for field in fields if "." in field.name]:  # Paths use dots.
        raise UnsupportedConstraint(f"Field {dotted[0]!r}: a dot in a field name is unsupported")
    constants = {
        name: getattr(model, name) for name in model.__class_vars__ if hasattr(model, name)
    }
    return Adapter(
        model, tuple(fields), tuple(constraints), tuple(validators), tuple(unsupported), constants
    )


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
        domain = _domain(_resolve(annotation)[0])  # Dataclasses do not enforce metadata.
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
    constants = {
        name: getattr(model, name)
        for name, hint in annotations.items()
        if (hint is ClassVar or get_origin(hint) is ClassVar) and hasattr(model, name)
    }
    return Adapter(model, tuple(fields), (), validators, tuple(unsupported), constants)


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
