# What modelfill supports

This is the full list of what modelfill reads. A field of a type it does not solve is
an [external field](../README.md#external-fields): it gets your
value, a provider's, its default or a boring one, and Pydantic validates it as usual.
Validation logic outside this list is unsupported: strict mode refuses the model, and
auto mode [falls back](../README.md#unsupported-logic-and-fallback) to a bounded
search that never claims impossibility.

## Models

- Pydantic v2 `BaseModel` and `RootModel`, with inherited validators.
- Standard dataclasses, including frozen and slotted ones, with a pure
  `__post_init__` when the generated `__init__` calls it (a subclass that is not
  decorated again never runs its own). A dataclass gains no Pydantic validation:
  modelfill enforces only the solved field types, and `Annotated` metadata on a
  dataclass field is not a rule unless the dataclass's own code enforces it.
- Required nested models, Pydantic or dataclass, are solved with their parent, so
  its rules may read `self.customer.age`, and a dict fixes some of the nested
  fields. Optional nested models and those with defaults are completed on their own.
- These constructor shapes fail explicitly: Pydantic dataclasses, dataclass
  `InitVar` and `init=False` fields, duplicate aliases, and alias paths through a
  list that Pydantic cannot replace with the field name.

## Solved field types

- Exact `int`, `bool`, `str`, `date`, naive `datetime`, naive `time` and
  `timedelta`.
- `Decimal` with `decimal_places`, as whole numbers of its last place.
- Ordinary `Enum`, `IntEnum` and `StrEnum`, and homogeneous `Literal` subsets.
- Any of these `| None`, as `Optional` or as `None` inside a `Literal`, also behind
  a `NewType`, a `type` alias or an `Annotated` inside the `Optional`, whose
  constraints apply.

Every other type is an external field: floats, containers, UUIDs, aware datetimes,
other unions, mixed-type `Literal`s, Flags and Enums with custom behavior among them.

### Boring values

An external field without your value, a provider, a default or a nested model's
completion gets a boring value:

| Field | Boring value |
| --- | --- |
| `X \| None`, `Any` | `None` |
| `list`, `tuple[X, ...]`, `set`, `dict` | empty; a list or tuple with `min_length` repeats its element's boring value, and a set or dict gets distinct elements or keys: that value, then the next ones (`0`, `1`, … or `""`, `"1"`, …) |
| `tuple[int, str]` | each element's boring value: `(0, "")` |
| `float`, `Decimal` without `decimal_places` | `0`, or the whole number nearest 0 that the bounds allow (`gt=0` gives `1.0`) |
| `int \| str` and other unions | the first member's boring value |
| `UUID`, `UUID4`, … | `00000000-0000-4000-8000-000000000000`, with the version bits the annotation asks for |
| `bytes`, `AwareDatetime` | `b""`, 2025-01-01 00:00 UTC |
| `PastDate`, `FutureDate` | 2000-01-01 and 2100-01-01, fixed so canonical values stay put; `PastDatetime` and `FutureDatetime` likewise at midnight |
| `list[M]`, `tuple[M, ...]`, `dict[K, M]` of models | as many canonical completions of `M` as `min_length` asks, each a new object, under the keys above |
| `A \| B` of models | the first member that fills, in declaration order: a tagged union's first branch, whose tag is the one value its `Literal` allows |
| `EmailStr`, URLs, `SecretStr` | `user@example.com`, `https://example.com/`, `SecretStr("")` |
| Flags, unusual enums, mixed `Literal` | the first member |

Z3 strings cover U+0000 to U+2FFFF, ordinary text and emoji included. A fixed string
outside that range is unsupported. Such a literal in a validator makes strict mode
refuse the model and auto mode fall back, such a default is simply not preferred,
and a `Literal` with such a choice makes its field external.

## Field constraints

- `gt`, `ge`, `lt` and `le` on integers, decimals, dates, datetimes and timedeltas,
  strict or inclusive.
- `multiple_of` on integers and decimals, and a decimal's `max_digits` and
  `decimal_places`, exactly as Pydantic counts them.
- `min_length` and `max_length` on strings, and the global `str_min_length` and
  `str_max_length`.
- A [`pattern`](#patterns) on strings, with either regex engine.
- On optional fields, all of these constrain values other than `None`, as in
  Pydantic.

Metadata is read through Pydantic's public `model_fields` and combines as Pydantic
combines it: when a field repeats one kind of bound, the last one wins, with
`Field()` counted before `Annotated[...]`. So `Field(ge=10)` on an
`Annotated[int, Ge(0)]` alias leaves `ge=0` in force, in Pydantic and modelfill
alike. Groups such as `conint(...)`, `constr(...)`, `StringConstraints` and
`Interval` count as their single constraints. Validators are found through
Pydantic's private `__pydantic_decorators__` registry, which CI tests against the
oldest supported Pydantic release.

Aliases: distinct string input and validation aliases, `AliasPath`s of keys into
nested dicts, and `AliasChoices`, of which the first such choice is used, else the
field name when Pydantic accepts it. `fill()` still takes field names.

Defaults are preferences: copied values and zero-argument factories.

### Patterns

A string field's `pattern`, from `Field`, `constr` or `StringConstraints`, never
reaches the solver. Pydantic itself decides what matches: every candidate is
checked against the pattern by Pydantic, so any pattern Pydantic accepts, under
either `regex_engine`, means exactly what it means in your model. modelfill reads
the pattern with Python's regex parser only to spell values from it, as
[canonical values](../README.md#canonical-values) describes. A pattern it cannot
read, such as one with Rust's `\p{L}`, is still checked; only the field's
name-based candidates are tried for it.

Because the solver then allows more values than Pydantic, never fewer, a pattern
cannot cause a false proof of impossibility. A fixed value that breaks a pattern is
a proven contradiction: `code='usd' conflict with code pattern '^[A-Z]{3}$'`. When
the rules and a pattern cannot both hold, though, the result is
`GenerationExhausted`, naming the pattern.

## Validators

- `@model_validator(mode="after")` on instances, returning the original `self` on
  successful paths.
- `@field_validator(mode="after")`, and `Annotated[..., AfterValidator(f)]` with a
  named function `f`, returning the original value.
- A dataclass's `__post_init__`.

The whole relevant validator must be understood: translating part of it could
claim impossibility falsely. Early returns and nested branches are analyzed as
paths, rejection conditions are not blindly negated, and operators follow Python's
parsed precedence.

### Statements

- `if`, `elif` and `else`.
- `match`, whose cases form an if/elif chain. Patterns may be literals or dotted
  constants, compared with `==`; `None`, `True` and `False`, compared by identity;
  alternatives with `|`; captures, `as` and the wildcard `_`. Cases may have `if`
  guards. A name a pattern binds may be read in its case and guard only.
- Single-name assignments, `x = …` and `x: T = …`, other than to validator
  parameters, also inside branches. After the branches, a local holds the value of
  the branch taken. A local set on some paths only raises `UnboundLocalError` where
  read on the others, which rejects that path; one that may be `None` on some path
  is unsupported.
- `assert`: a failing one rejects, unless `python -O` removed it.
- `pass` and string docstrings.

### Rejecting and returning

- A path rejects by raising builtin `ValueError` or `AssertionError`, or
  `PydanticCustomError`, with any message, f-strings included, and any `from`
  clause. Both only run on paths that already reject.
- A path succeeds by returning the original `self` from a model validator or the
  original value from a field validator; a dataclass's `__post_init__` returns
  bare, returns `None` or falls through.

### Names and constants

- `self.field`, and `self.customer.age` through required nested models.
- A field validator's value parameter, or its fields when it validates a nested
  model; `info.data["earlier_field"]` with a literal key, and `info.field_name`.
- Locals assigned earlier.
- Named constants and standard Enum members from globals and closures, and the
  class's `ClassVar` constants through `self` or `cls`.
- Integer, Boolean, string, `None` and finite `Decimal` constants; `date(…)`,
  `datetime(…)`, `time(…)` and `timedelta(…)` with integer literal arguments;
  `Decimal(…)` of a string or integer literal.

### Operations

- Comparisons: `==` and `!=`; `<`, `<=`, `>` and `>=` between numbers (integers,
  Booleans and decimals, by value) or two values of one date/time type; chained
  comparisons; identity checks against `None` only.
- Boolean operations: `and`, `or` and `not`, with Python's truthiness and short
  circuits. Operand-valued `and`/`or` need operands of one supported type, or
  decimals and integers, that cannot be `None` (`x or default` excepted).
  Conditional expressions, `a if c else b`, follow the same terms, and only the
  branch taken runs.
- Membership: `in` and `not in` a literal tuple, list or set of supported constants
  or Enum members, outside comparison chains.
- Integer and Boolean arithmetic: `+`, `-`, unary `+` and `-`, `*` with an integer
  literal operand, and `%` and `//` by a positive integer literal.
- Decimal arithmetic: `+`, `-`, unary `+` and `-`, and `*` with an integer or
  `Decimal` literal operand, integers mixed in. Each decimal read needs
  `max_digits`, and no result may outgrow the decimal context's precision or
  exponent limits, so Python never rounds it. A filler compiles anew for each
  context it fills in.
- Date arithmetic, by Python's rules: `datetime ± timedelta`, `datetime - datetime`,
  `date ± timedelta` (whole days, like `timedelta.days`), `date - date`,
  `timedelta ± timedelta`, `timedelta * int` literal and `-timedelta`.
- Builtins: `abs()` of an integer or a decimal; `min()` and `max()` of two or more
  numbers, or of values of one date/time type, the first of equal values winning as
  in Python; `len()` of a string.
- Strings: equality, truthiness, `len()`, `startswith()` and `endswith()` with a
  string or a literal tuple of them, and substring `in` and `not in`.
- Dates and times: `.year`, `.weekday()` and `.isoweekday()` of a date or datetime;
  `.hour`, `.minute`, `.second` and `.microsecond` of a datetime or time; `.days`,
  `.seconds` and `.microseconds` of a timedelta; `.date()` and `.time()` of a
  datetime.
- Enums: equality within one Enum type, finite membership and member truthiness; a
  member's `.value` and `.name`, when its type's values share one supported type.
- `None`: `x is None`, `x is not None`, `x == None`, truthiness and `x or default`.
  An operation that raises `TypeError` on `None`, such as `x < y`, `x + 1` or
  `len(x)`, rejects that path, so guard it as Python code does:
  `x is not None and x < y`.

## Unsupported

Validation logic outside the lists above is unsupported, for example loops, other
calls and string methods, `try` and `with`, class, sequence and mapping patterns in
`match`, mutation, other exception classes and transformed return values. So are
async, generator and wrapped validators, validators without retrievable source, and
these Pydantic features:

- `before`, `plain` and `wrap` validators, and legacy validators;
- `Annotated` callbacks other than a pure `AfterValidator`;
- other field metadata, such as `StringConstraints(to_lower=True)`;
- transforming model config;
- custom initialization, core schema, attribute hooks and a custom
  `model_post_init` (private attributes are fine);
- default factories that read other fields.

A validator that reads an external field is unsupported too, and so is other code
in an external field's metadata, an `AfterValidator` or `ValidateAs` among them.
