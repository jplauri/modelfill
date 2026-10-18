# modelfill

**Fix only the fields your test cares about. modelfill fills in the rest with
boring, valid, deterministic values.**

modelfill completes Pydantic models and standard dataclasses from their existing
validation rules. It translates the rules into solver constraints, solves for the
missing fields in either direction and runs the original constructor before
returning anything. Every value it picks is [canonical](#canonical-values): as
close to 0, `False`, `None`, 2025-01-01 or the field's own name as the rules
allow, and the same on every run. Models require no changes, factory classes,
decorators, or duplicate rules.

To hunt bugs instead, [explore with Hypothesis](#exploration-with-hypothesis):
Hypothesis draws the fields you care about, and modelfill solves the rest with
random values that favor the edges of what the rules allow.

modelfill reads a [documented subset](#exact-recognized-validator-language) of
Python: pure, deterministic validators that reject invalid input. It reports
logic outside that subset, and it never claims a contradiction it cannot prove.

## Install and try

modelfill needs Python 3.12 or later:

```sh
pip install modelfill                # core
pip install "modelfill[hypothesis]"  # with the Hypothesis strategy
```

To run the example from a checkout, use [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run python examples/meeting.py
```

Save models in a Python source file: extraction requires `inspect.getsource()`.
Interactive definitions without retrievable source use automatic fallback or
fail in strict mode.

```python
from typing import Self
from pydantic import BaseModel, Field, model_validator
from modelfill import fill, ModelFiller, UnsatisfiableConstraints


class Meeting(BaseModel):
    start: int = Field(ge=8, le=17)
    end: int = Field(ge=9, le=18)
    duration: int = Field(ge=1, le=4)

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.end != self.start + self.duration:
            raise ValueError("Invalid meeting")
        return self


# A test that cares about end and duration fixes them; modelfill solves start.
assert fill(Meeting, end=18, duration=4).start == 14
assert fill(Meeting, start=10, end=13).duration == 3
assert fill(Meeting, start=10, duration=2).end == 12
print(fill(Meeting))  # start=8 end=9 duration=1, on every run

try:
    fill(Meeting, start=17, duration=4)
except UnsatisfiableConstraints:
    pass  # end would be 21, but end <= 18
else:
    raise AssertionError("Expected an impossible assignment")

filler = ModelFiller(Meeting, mode="strict")
assert filler.fill(end=18, duration=4).start == 14
meetings = filler.batch(5)  # Five distinct meetings, in canonical order.
print(filler.explain())
```

The same analysis works on ordinary dataclasses:

```python
from dataclasses import dataclass
from modelfill import fill


@dataclass
class Interval:
    start: int
    end: int
    duration: int

    def __post_init__(self):
        if self.duration < 1 or self.end != self.start + self.duration:
            raise ValueError("Invalid interval")


assert fill(Interval, end=18, duration=4).start == 14
```

Dates, datetimes, durations and optional fields work the same way:

```python
from datetime import datetime, timedelta
from pydantic import BaseModel, model_validator
from modelfill import fill


class Booking(BaseModel):
    start: datetime
    end: datetime
    length: timedelta
    note: str | None = None

    @model_validator(mode="after")
    def check(self):
        if self.end != self.start + self.length:
            raise ValueError("end must equal start + length")
        if not timedelta(minutes=30) <= self.length <= timedelta(hours=4):
            raise ValueError("bookings last from 30 minutes to 4 hours")
        return self


booking = fill(Booking, end=datetime(2025, 3, 1, 18), length=timedelta(hours=2))
assert booking.start == datetime(2025, 3, 1, 16)
```

## API and input semantics

```python
fill(Model, **fixed_fields)
ModelFiller(
    Model, *, mode="auto", max_attempts=100, work_limit=1_000_000, timeout_ms=1000, providers=None
)
filler.fill(**fixed_fields)
filler.batch(count, **fixed_fields)
filler.explain()  # fresh JSON-friendly dictionary
```

- All `fill()` keywords are **canonical model field names**, even if a model uses
  input aliases. Configuration lives on `ModelFiller`; fields named `mode`,
  `model`, `count`, or `self` work.
- Fields modelfill solves take exact Python `int`, `bool`, `str`, `date`, naive
  `datetime` and `time`, `timedelta`, a `Decimal` within its `decimal_places`, Enum
  members, allowed Literal values, or `None` for optional fields. There is no
  coercion: `"10"` or `True` for an `int` field, `Decimal("1.234")` for two places,
  or an aware datetime, raises `TypeError`. Unknown field names raise `ValueError`.
- Fields of any other type (UUIDs, floats, lists, nested models, …) are
  **external**: the solver does not model them. Your value goes to the
  constructor, which may convert it as usual (`5` to `5.0`); without one, such a
  field gets a value as described in
  [fields the solver does not model](#fields-the-solver-does-not-model).
  Validators that read external fields are unsupported logic.
- Explicit values of solved fields are hard constraints. A returned object keeps
  both their type and value; validator normalization cannot silently replace them.
- Defaults are **preferences**, used when compatible with the solved constraints.
  A missing field's zero-argument default factory runs once per fill call.
  Standard defaults are copied. A conflicting default can be replaced.
- All generated fields, including selected defaults, are passed explicitly to
  the original constructor and validated. This is stronger than Pydantic's
  default behavior of leaving omitted defaults unchecked, and modelfill does not
  search by omitting fields. A default Pydantic would leave unchecked can
  therefore make a contradiction unprovable: while such a field is omitted, a
  contradiction is reported as `GenerationExhausted`, never UNSAT.
  `explain()["unsat_requires_explicit_fields"]` lists these fields; passing them
  explicitly, or enabling Pydantic's `validate_default`, makes UNSAT provable.
- Dataclasses run their normal `__init__` and `__post_init__`. They do not gain
  Pydantic validation; supported type domains are enforced by modelfill.
- Every returned object has successfully completed its original construction
  pipeline, has canonical field types, and preserves fixed values. Neither
  `model_construct()` nor any other validation bypass is used.

`batch()` returns `count` **distinct** completions in canonical order; see
[canonical values](#canonical-values).

## Supported features

| Area | Support |
| --- | --- |
| Pydantic | v2 `BaseModel` and `RootModel`, inherited validators, scalar fields |
| Dataclasses | Standard dataclasses, including frozen and slotted classes; pure `__post_init__`, when the generated `__init__` calls it (a subclass that is not decorated again never runs its own) |
| Types | Exact `int`, `bool`, `str`, `date`, naive `datetime`, naive `time`, `timedelta`; `Decimal` with `decimal_places`; ordinary `Enum`, `IntEnum`, `StrEnum`; homogeneous `Literal` subsets; any of these `\| None` (`Optional`, or `None` inside a `Literal`), also behind a `NewType`, a `type` alias or an `Annotated` inside the `Optional`, whose constraints apply; other types as [external fields](#fields-the-solver-does-not-model) |
| Numeric metadata | Integer, decimal, date, datetime and timedelta `Field(gt, ge, lt, le)`, respecting strict and inclusive bounds; integer and decimal `Field(multiple_of)`; a decimal's `max_digits` and `decimal_places`, exactly as Pydantic counts them; on optional fields they constrain non-None values, as in Pydantic |
| String metadata | `Field(min_length, max_length)` and global `str_min_length` / `str_max_length`; a [`pattern`](#string-patterns), with either regex engine |
| Model validators | Instance `@model_validator(mode="after")`, returning the original `self` on successful paths |
| Field validators | `@field_validator(mode="after")`, and `Annotated[..., AfterValidator(f)]` with a named function `f`, returning the original value, including literal `info.data["earlier_field"]` reads |
| Nested models | A required nested model (Pydantic or dataclass) is solved with its parent: rules may read `self.customer.age`, and a dict fixes some of its fields. Optional nested models and those with defaults are completed on their own |
| Aliases | Distinct string input/validation aliases, `AliasPath`s of keys into nested dicts, and `AliasChoices`, of which the first such choice is used, else the field name if Pydantic accepts it; API still uses field names |
| Defaults | Copied values and zero-argument factories as preferences |
| Fixed fields | Any subset, solved simultaneously; no left-to-right dependency order required |

Pydantic field metadata is read through public `model_fields`, and combines as
Pydantic combines it: when a field repeats one kind of bound, the last one wins,
with `Field()` counted before `Annotated[...]`. So `Field(ge=10)` on an
`Annotated[int, Ge(0)]` alias leaves `ge=0` in force, in Pydantic and modelfill
alike. Groups such as `conint(...)`, `constr(...)`, `StringConstraints` and
`Interval` count as their single constraints. Validator discovery uses Pydantic's
private `__pydantic_decorators__` registry; CI tests it against the oldest
supported Pydantic release. Dataclass `Annotated` metadata is not a validation
rule unless the dataclass's own code enforces it, so modelfill does not impose it.

### String patterns

A string field's `pattern`, from `Field`, `constr` or `StringConstraints`, never
reaches the solver. Pydantic itself decides what matches: every candidate is
checked against the pattern by Pydantic, so any pattern Pydantic accepts, under
either `regex_engine`, means exactly what it means in your model. modelfill reads
the pattern with Python's regex parser only to spell values from it, as
[canonical values](#canonical-values) describes. A pattern it cannot read, such as
one with Rust's `\p{L}`, is still checked; only the field's name-based candidates
are tried for it.

Because the solver then allows more values than Pydantic, never fewer, a pattern
cannot cause a false proof of impossibility. A fixed value that breaks a pattern is
a proven contradiction: `code='usd' conflict with code pattern '^[A-Z]{3}$'`. When
the rules and a pattern cannot both hold, though, the result is
`GenerationExhausted`, naming the pattern.

### Exact recognized validator language

| Construct | Accepted forms |
| --- | --- |
| Statements | `if` / `elif` / `else`, `pass`, string docstrings, `assert`, supported `raise`, supported `return`, single-name local assignments (`x = …`, `x: T = …`) outside branches, other than to validator parameters |
| Rejection | `raise` of builtin `ValueError` or `AssertionError`, or of `PydanticCustomError`, with any message, including f-strings, and any `from` clause (both only run on paths that already reject); a failing `assert`, which `python -O` removes as usual |
| Successful return | Original `self` for model validators, original value for field validators; bare return, `return None`, or fallthrough for dataclasses |
| References | `self.field`, and `self.customer.age` through required nested models; field validator value parameter, or its fields when it validates a nested model; `info.data["earlier_field"]`, and `info.field_name`; previously assigned locals; named constants and standard Enum members from globals/closures, and the class's `ClassVar` constants through `self` or `cls` |
| Constants | Integer, Boolean, string, `None`, finite `Decimal`; `date(…)`, `datetime(…)`, `time(…)` and `timedelta(…)` calls with integer literal arguments; `Decimal(…)` of a string or integer literal |
| Builtin calls | `abs(x)` of an integer or a decimal; `min(…)` and `max(…)` of two or more numbers (integers and decimals) or values of one date/time type, the first of equal values winning as in Python |
| Arithmetic | Integer/Boolean `+`, `-`, unary `+`/`-`; multiplication with an integer literal operand; `%` and `//` by a positive integer literal; decimal `+`, `-`, unary `+`/`-` and multiplication with an integer or `Decimal` literal operand, integers mixed in, when each decimal read has `max_digits` and no result can outgrow the decimal context's precision, so Python never rounds it; Python's date rules: `datetime ± timedelta`, `datetime - datetime`, `date ± timedelta` (whole days, like `timedelta.days`), `date - date`, `timedelta ± timedelta`, `timedelta * int literal`, `-timedelta` |
| Comparisons | `==`, `!=`; `<`, `<=`, `>`, `>=` between numbers (integers, Booleans and decimals, by value) or two values of one date/time type; chained comparisons; identity checks only against `None` |
| `None` | `x is None`, `x is not None`, `x == None`, truthiness, `x or default`; an operation that raises `TypeError` on `None` (`x < y`, `x + 1`, `len(x)`) rejects that path, so guard it as Python code does: `x is not None and x < y` |
| Boolean operations | `and`, `or`, `not`, with Python truthiness and short circuit meaning; operand-valued `and`/`or` when operands have the same supported type, or are decimals and integers, and cannot be `None` (`x or default` excepted); conditional expressions `a if c else b` on the same terms, where only the branch taken runs |
| Membership | `in` / `not in` a literal tuple, list or set of supported constants or Enum members, outside comparison chains |
| Strings | Equality, inequality, truthiness, membership in finite literal collections, builtin `len(field)`; `startswith()` and `endswith()` with a string or a literal tuple of them; substring `in` / `not in` |
| Dates | `.year`, `.weekday()` and `.isoweekday()` of a date or datetime; `.hour`, `.minute`, `.second` and `.microsecond` of a datetime or time; `.days`, `.seconds` and `.microseconds` of a timedelta; `.date()` and `.time()` of a datetime |
| Enums | Equality/inequality within the same Enum type, finite membership, exact member truthiness; a member's `.value` and `.name`, when its type's values share one supported type |

Early returns and nested branches are analyzed as paths. Rejection conditions
are not blindly negated. The entire relevant validator must be understood;
partially translating it would risk false claims of impossibility. Operators
follow Python's parsed precedence, including operand-returning `and` and `or`.

### Unsupported features and fallback

Field types the solver does not model (floats, containers, nested models, UUIDs,
aware datetimes, other unions, mixed-type Literals, Flags, custom Enum behavior,
…) become external fields, described above. Unsupported **constructor shapes**
fail explicitly: Pydantic dataclasses, dataclass `InitVar` / `init=False`
fields, duplicate aliases, and alias paths through a list that Pydantic cannot
replace with the field name.

Validator code outside the [language above](#exact-recognized-validator-language)
is unsupported **validation logic**, for example loops, other calls and string
methods, `try`, `with` and `match`, mutation, other exception classes and
transformed return values. So are async, generator and wrapped validators,
validators without retrievable source, and these Pydantic features: `before`,
`plain` or `wrap` validators; legacy validators; Annotated callbacks other than
a pure `AfterValidator`; other field metadata, such as
`StringConstraints(to_lower=True)`; transforming model config; custom
initialization, core schema, attribute hooks and a custom `model_post_init`
(private attributes are fine); and data-dependent default factories.

In **strict** mode, unsupported validation logic raises `UnsupportedConstraint`
when the filler is created. In **auto** mode it appears in
`explain()["unsupported"]`. If any model behavior is unsupported, auto mode
demotes the extracted validator predicates to guidance, since unknown code may
transform values before another validator runs. Field constraints remain
candidate bounds; normal construction still runs every original validator. A
candidate it rejects is excluded and the next canonical candidate is tried.
Attempts alternate between preferring the guidance and ignoring it, so
misleading guidance cannot starve the search, and the sequence is the same on
every run. Guidance that leaves no candidate at all, such as a value its pattern
rules out, is dropped for the rest of the call. It tries at most `max_attempts`
candidates and never claims UNSAT in this fallback mode. This bounded search can
miss valid solutions, especially narrow conditions or transformations.

A candidate counts as rejected only when construction turns it down the way its
kind of model does: with `ValidationError` from a Pydantic model, which wraps its
validators' errors, or with `ValueError` or `AssertionError` from a dataclass's
`__post_init__`. Any other exception propagates so programming errors are visible,
including one from a default factory. Validation is not sandboxed: do not use side
effects or nondeterministic validators. Solver timeouts cannot interrupt Python
code.

Z3 string values cover U+0000–U+2FFFF, including ordinary text and emoji. Fixed
strings outside this range are explicitly unsupported. Such literals in a
validator cause strict rejection or automatic fallback; such defaults are simply
not preferred, and a `Literal` with such a choice makes its field external.
Arbitrary Python string behavior is not modeled.

## Failure categories

| Exception | Meaning |
| --- | --- |
| `UnsatisfiableConstraints` | A clean solver check proves the supported constraints plus canonical fixed assignments contradictory. No random preferences, blocked candidates or artificial bounds contribute to this result. When the solver isolates the conflict, the message names the fixed inputs, field constraints and validators involved, such as `start=17, duration=4` against `end <= 18` and `Meeting.check`. |
| `UnsupportedConstraint` | Unsupported constructor shape, a required external field without a value, or unsupported validation in strict mode. |
| `GenerationExhausted` | The bounded search did not find a normally validated object; also used for contradictions when unknown behavior or omitted unchecked defaults prevent an impossibility claim, or when the rules and a pattern cannot both hold. |
| `SolverTimeout` | A solver query exhausted its budget: its fixed work limit, or the `timeout_ms` wall-clock safety net. Subclass of `SolverUnknown`. |
| `SolverUnknown` | Z3 could not decide SAT/UNSAT for another reason, or did not say why. |

These exceptions inherit from `ModelFillError`. Incorrect API arguments raise
`TypeError` or `ValueError`. Each solver query has a fixed work limit,
`work_limit`, counted in Z3 resource units rather than time, so whether it finishes
never depends on machine speed; a query that cannot finish raises rather than
settling for a different value. Raise `work_limit` if a heavy model hits
`SolverTimeout`. The unit is Z3's own, so a given limit buys about, not exactly,
the same work across Z3 releases. `timeout_ms` is only a **per-query** wall-clock
safety net, not a deadline for a whole fill, which makes several queries per
field.

Diagnostics include field domains and patterns, defaults, each validator's
extraction status, unsupported reasons, whether extracted predicates are active,
and proof eligibility. In a fallback model, successfully extracted validators
report the status `guidance` rather than `modeled`.

## Canonical values

`fill()` returns the canonical completion: a pure function of the model, the
fixed inputs, the modelfill version and the model's own default factories. It
never depends on call order, earlier calls, the thread or machine speed.

1. Fixed inputs are hard constraints.
2. Defaults are used where they fit the rules and fixed inputs, in declaration
   order.
3. Every remaining field is chosen in declaration order: it takes the allowed
   value closest to a neutral anchor, given everything chosen before it, and is
   pinned before the next field is chosen. A required nested model's fields take
   its place in that order.

| Field | Canonical value |
| --- | --- |
| `int` | nearest `0` |
| `Decimal` with `decimal_places` | nearest `0`, with the field's places: `0.00` |
| `timedelta` | nearest `timedelta(0)` |
| naive `time` | nearest midnight |
| `date` | nearest 2025-01-01 |
| naive `datetime` | nearest 2025-01-01 00:00 |
| `bool` | `False` |
| `Enum`, `Literal` | the first declared choice allowed |
| optional (`X \| None`) | `None` when allowed |
| `str` | the field's name, or the rule below |

Distance is the absolute difference, and a tie goes to the value above the
anchor: if `0` is not allowed but `-1` and `1` are, the value is `1`. A bound
appears only when it is the closest allowed value. Defaults are settled before any
anchor, so a default that fits wins over other fields' anchors. Fixing a later
field leaves an earlier one alone unless the rules force it to move, or the fixed
value replaces a default that had moved it: `fill(Meeting, duration=3)` keeps
`start=8`, while `end=18` forces `start=14`.

Strings never come from the solver's own string choices. The rule is the
field's name; else `name_1` to `name_9`; else the name cut or padded with `_` to
the allowed length nearest its own (a tie goes longer), then that with its last
character replaced by `1` to `9`; else the name inside strings the rules test the
field to start with, end with or contain, at the allowed length nearest that, so
`SKU-sku`, `filename.csv` or `X-code__-Z`; else the strings the rules mention, then
other fields' string values, so a confirmation field copies its password.

With a [pattern](#string-patterns), every candidate must also match it. When none
of the name's candidates does, modelfill spells a value from the pattern before
trying strings from elsewhere. If a value can start with the name's first letter,
it has the allowed length nearest the name's and follows the name as far as the
pattern lets it; otherwise it is the shortest. Each other character is the first
of `a`–`z`, `A`–`Z`, `0`–`9` that fits:

| Field | Canonical value |
| --- | --- |
| `code: str = Field(pattern=r"^[A-Z]{3}$")` | `COD` |
| `city: str = Field(pattern=r"^[A-Z][a-z]+$")` | `City` |
| `sku: str = Field(pattern=r"^[A-Z]{3}-\d{4}$")` | `SKU-0000` |
| `zip: str = Field(pattern=r"^\d{5}$")` | `00000` |
| `phone: str = Field(pattern=r"^\+?[1-9]\d{1,14}$")` | `10` |

`batch(count)` returns `count` distinct completions, compared on the validated
objects' generated fields, in canonical order: the canonical completion, then the
canonical completion of what remains once each earlier one is excluded. It raises
`GenerationExhausted` when fewer distinct valid completions exist or can be found
within the budget. When each member gets its own provider values and unsupported
logic could judge candidates by them, a member first tries again, in order, the
candidates that earlier members tried.

Canonical values are stable within a release but **not across versions**: a later
release may choose differently. A test should fix every field it asserts on.

The solver's hard domains have no arbitrary integer or string-length bounds.
A canonical assignment that validates on the first try is cached per fixed inputs
and default values, so a repeated fill only reruns construction. Each thread owns a separate Z3 context and
solver; immutable compiled information is shared, and compiled models and the
convenience `fill()` fillers live in bounded caches. Treat model definitions and
the Enum classes and constants their validators read as immutable after first
compilation; restart the process after monkeypatching or rebuilding a model.

```sh
uv run python -m benchmarks.benchmark --count 100 --repeat 5
```

Measured results, the design and comparisons with Polyfactory and CrossHair are
in [the design document](https://github.com/jplauri/modelfill/blob/master/docs/architecture.md).

## Fields the solver does not model

A field of any other type, such as a UUID, a float, a list or a nested model, takes
its value from the first of these:

1. your value. For a nested model, a dict fixes some of its fields and modelfill
   completes the rest; an instance is used as it is, so it must keep its own
   model's rules, which Pydantic does not check again. Dicts inside a given list,
   tuple or dict of models are completed the same way, keeping the shape given,
   and for a union of models the first model member that takes the dict does;
2. a provider, keyed by field path or by type; a path beats a type, and may lead
   into a list, tuple, dict or union of models, as `"lines.id"` does;
3. the model's own default;
4. for a nested model, its canonical completion; otherwise a boring value. A
   required nested model is solved together with its parent, so the parent's
   rules can read its fields and a fixed parent field can determine them. Models
   in a list, tuple or dict, or in a union, are completed too.

```python
from uuid import UUID

from pydantic import BaseModel, Field
from modelfill import ModelFiller, fill


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


order = fill(Order)  # No arguments needed: every field gets a valid, boring value.
assert fill(Order, customer={"age": 30}).customer.age == 30
filler = ModelFiller(
    Order, providers={UUID: lambda: UUID(int=7), "customer.id": lambda: UUID(int=9)}
)
assert (filler.fill().id, filler.fill().customer.id) == (UUID(int=7), UUID(int=9))
```

| Field | Boring value |
| --- | --- |
| `X \| None`, `Any` | `None` |
| `list`, `tuple[X, ...]`, `set`, `dict` | empty; a list or tuple with `min_length` repeats its element's boring value, and a set or dict gets distinct elements or keys: that value, then the next ones (`0`, `1`, … or `""`, `"1"`, …) |
| `tuple[int, str]` | each element's boring value: `(0, "")` |
| `float`, `Decimal` without `decimal_places` | `0`, or the whole number nearest 0 that the bounds allow (`gt=0` gives `1.0`) |
| `int \| str` and other unions | the first member's boring value |
| `UUID`, `UUID4`, … | `00000000-0000-4000-8000-000000000000`, with the version bits the annotation asks for |
| `bytes`, `time`, `AwareDatetime` | `b""`, `time(0, 0)`, 2025-01-01 00:00 UTC |
| `PastDate`, `FutureDate` | 2000-01-01 and 2100-01-01, fixed so canonical values stay put; `PastDatetime` and `FutureDatetime` likewise at midnight |
| `list[M]`, `tuple[M, ...]`, `dict[K, M]` of models | as many canonical completions of `M` as `min_length` asks, each a new object, under the keys above |
| `A \| B` of models | the first member that fills, in declaration order: a tagged union's first branch, whose tag is the one value its `Literal` allows |
| `EmailStr`, URLs, `SecretStr` | `user@example.com`, `https://example.com/`, `SecretStr("")` |
| Flags, unusual enums, mixed `Literal` | the first member |

A provider is a zero-argument callable, run once per returned object like a default
factory, so each batch member gets its own values; one that provides a required
nested model, which is solved with its parent, fixes it for the whole call, as an
instance or a dict, at any depth.
Providers apply only to fields the solver does not model, so one that targets a
solved field, or a path that does not exist, fails when the filler is created.
A field with no value from any source, such as one of an arbitrary class, raises
`UnsupportedConstraint`; pass a value or add a provider. If Pydantic keeps
rejecting a boring value, for example because of a custom validator, the search
ends in `GenerationExhausted`, which names the field. Validators that read these fields
remain unsupported logic, an `AfterValidator` in their metadata included, and so
does other code there, such as `ValidateAs`. A model with either falls back to the
bounded search; so does a rule that reads an optional nested model or one with a
default, and any unsupported logic inside a nested model makes the whole object
fall back.

## Exploration with Hypothesis

Canonical values are for tests that need *a* valid object. To hunt bugs, explore
with `modelfill.hypothesis.from_model`, a Hypothesis strategy that needs the
`hypothesis` extra. Pass strategies for the fields you want Hypothesis to drive;
required external fields without a provider default to `st.from_type`, and
modelfill solves the rest from the validators. Unlike `fill()`, it picks those
values at random, often at the edges of their feasible ranges, where bugs tend to
hide. To set `mode`, `max_attempts`, `work_limit`, `timeout_ms` or `providers`,
pass a configured `ModelFiller` in place of the class, so options never collide
with field names:

```python
from datetime import datetime, timedelta
from uuid import UUID

from hypothesis import given, strategies as st
from pydantic import BaseModel, model_validator
from modelfill.hypothesis import from_model


class Shift(BaseModel):
    id: UUID  # External: drawn with st.from_type(UUID).
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


@given(from_model(Shift, start=st.datetimes(), break_minutes=st.integers(-30, 90)))
def test_shift_rules(shift):
    assert shift.end > shift.start
```

Draws that make the rules impossible (here `break_minutes` outside 0–60) are
rejected rather than failing the test. Hypothesis counts a rejection like a
`.filter()`, so a strategy that mostly draws impossible values still fails its
health check; draw the fields you choose from mostly possible values. The solved
fields never need filtering, however the rules tie them together. Each example
uses a fresh solver seeded from the draw, so failing examples replay and the
drawn fields shrink as usual. The solved fields replay too, but do not shrink.

## Development

```sh
uv sync
uv run pytest
uv run pytest tests/test_properties.py --hypothesis-profile=deep  # a longer random search
```

[CI](https://github.com/jplauri/modelfill/blob/master/.github/workflows/ci.yml) runs
the full set of checks.

MIT licensed. See [architecture and tradeoffs](https://github.com/jplauri/modelfill/blob/master/docs/architecture.md).
