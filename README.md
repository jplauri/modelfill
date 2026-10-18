# modelfill

[![PyPI](https://img.shields.io/pypi/v/modelfill)](https://pypi.org/project/modelfill/)
[![Python](https://img.shields.io/pypi/pyversions/modelfill)](https://pypi.org/project/modelfill/)
[![CI](https://github.com/jplauri/modelfill/actions/workflows/ci.yml/badge.svg)](https://github.com/jplauri/modelfill/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/jplauri/modelfill/blob/master/LICENSE)

**Fix only the fields your test cares about. modelfill fills in the rest with
boring, valid, deterministic values.**

modelfill completes Pydantic models and standard dataclasses from the validation
rules they already have. It turns field constraints and validators into solver
constraints, solves for the fields you left out, in either direction, and runs your
model's own constructor before returning anything. Models need no changes, no
factory classes and no second copy of their rules.

```python
from typing import Self

from pydantic import BaseModel, Field, model_validator

from modelfill import fill


class Meeting(BaseModel):
    start: int = Field(ge=8, le=17)
    end: int = Field(ge=9, le=18)
    duration: int = Field(ge=1, le=4)

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.end != self.start + self.duration:
            raise ValueError("end must be start + duration")
        return self


fill(Meeting)  # Meeting(start=8, end=9, duration=1), on every run
fill(Meeting, end=18, duration=4)  # Meeting(start=14, end=18, duration=4)
# fill(Meeting, start=17, duration=4) raises UnsatisfiableConstraints:
#   Meeting: start=17, duration=4 conflict with end <= 18, Meeting.check (line …)
```

**Contents:** [Why modelfill](#why-modelfill) · [Install](#install) ·
[Quickstart](#quickstart) · [How it works](#how-it-works) · [Guide](#guide) ·
[Reference](#reference) · [Development](#development)

## Why modelfill

A test usually needs a valid object but cares about one or two of its fields. The
usual ways to fill in the rest have a cost:

- **Hand-written factories and fixtures**, such as factory_boy's, repeat the
  model's rules in a second place, which drifts as the model changes.
- **Type-driven generators**, such as Polyfactory or Hypothesis's `st.builds`,
  follow types and per-field constraints, not validators: a rule across fields,
  such as `end == start + duration`, needs custom code, or invalid objects thrown
  away until one passes.

modelfill reads the rules themselves:

- **Valid by construction.** Every object went through your real constructor and
  validators; nothing bypasses validation.
- **Deterministic.** The same model and fixed fields give the same object on every
  run and machine, so a failing test reproduces.
- **Any direction.** Fix `end` and `duration`, and it solves `start`.
- **Honest.** Impossible inputs raise an error naming the rules in conflict, logic
  it cannot read is reported rather than guessed at, and it never claims an
  impossibility it cannot prove.

To hunt bugs instead, its [Hypothesis strategy](#exploration-with-hypothesis)
explores random values at the edges of what the rules allow. Polyfactory's wider
type coverage and factory customization remain good reasons to use it alongside;
the [design document](https://github.com/jplauri/modelfill/blob/master/docs/architecture.md#polyfactory-comparison)
compares the two.

## Install

```sh
pip install modelfill                # core
pip install "modelfill[hypothesis]"  # with the Hypothesis strategy
```

modelfill needs Python 3.12 or later, Pydantic 2.10 or later and the Z3 solver,
which installs from a wheel. Define models in a source file: modelfill reads
validators with `inspect.getsource()`, so a model defined interactively counts as
unsupported logic.

## Quickstart

With the `Meeting` model above:

```python
from modelfill import ModelFiller, UnsatisfiableConstraints

assert fill(Meeting, end=18, duration=4).start == 14  # Solved backwards from end.
assert fill(Meeting, start=10, end=13).duration == 3

try:
    fill(Meeting, start=17, duration=4)
except UnsatisfiableConstraints as error:
    print(error)  # Meeting: start=17, duration=4 conflict with end <= 18, Meeting.check (line …)

filler = ModelFiller(Meeting, mode="strict")  # Reusable; refuses rules it cannot read.
filler.batch(3)  # Three distinct meetings from start=8: durations 1, 2 and 3.
filler.explain()  # What it read, as a dictionary.
```

Dataclasses work the same way, with `__post_init__` as their validator, and so do
dates, durations and optional fields:

```python
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, model_validator

from modelfill import fill


@dataclass
class Interval:
    start: int
    end: int
    duration: int

    def __post_init__(self):
        if self.duration < 1 or self.end != self.start + self.duration:
            raise ValueError("Invalid interval")


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


assert fill(Interval, end=18, duration=4).start == 14
booking = fill(Booking, end=datetime(2025, 3, 1, 18), length=timedelta(hours=2))
assert booking.start == datetime(2025, 3, 1, 16)
```

From a checkout, `uv sync` and then `uv run python examples/meeting.py` runs the
example.

## How it works

1. **Read.** Field types and constraints become solver variables and bounds, and
   validators are parsed from their source into logic for the solver: a
   [documented subset](https://github.com/jplauri/modelfill/blob/master/docs/supported.md#validators)
   of Python, pure checks that reject invalid input. Fields of other types, such as
   UUIDs, floats, lists or nested models, are [external](#external-fields): they
   get values without the solver.
2. **Prove.** The Z3 solver checks the fixed fields against the rules. When no
   values for the other fields can satisfy them, `fill()` raises
   `UnsatisfiableConstraints`, naming the inputs and rules in conflict.
3. **Choose.** Each remaining field gets its [canonical value](#canonical-values):
   the allowed value nearest a neutral anchor, such as 0, field by field in
   declaration order.
4. **Construct.** The values go through your model's own constructor and
   validators. A fixed value keeps both its type and its value in the result.

When a model has logic modelfill cannot read, `mode="strict"` refuses it when the
filler is created, so every rule is modeled and every impossibility can be proven.
The default, `mode="auto"`, falls back to a bounded search that still validates
every candidate but never claims impossibility; see
[fallback](#unsupported-logic-and-fallback).

## Guide

### Canonical values

`fill()` returns the canonical completion: a pure function of the model, the fixed
fields, the modelfill version and the model's own default factories. It never
depends on call order, earlier calls, the thread or machine speed.

1. Fixed fields are hard constraints.
2. Defaults are used where they fit the rules and fixed fields, in declaration
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

Distance is the absolute difference, and a tie goes to the value above the anchor:
if `0` is not allowed but `-1` and `1` are, the value is `1`. A bound appears only
when it is the closest allowed value. Defaults are settled before any anchor, so a
default that fits wins over other fields' anchors. Fixing a later field leaves an
earlier one alone unless the rules force it to move, or the fixed value replaces a
default that had moved it: `fill(Meeting, duration=3)` keeps `start=8`, while
`end=18` forces `start=14`.

Strings never come from the solver's own string choices. The rule is the field's
name; else `name_1` to `name_9`; else the name cut or padded with `_` to the allowed
length nearest its own (a tie goes longer), then that with its last character
replaced by `1` to `9`; else the name inside strings the rules test the field to
start with, end with or contain, at the allowed length nearest that, so `SKU-sku`,
`filename.csv` or `X-code__-Z`; else the strings the rules mention, then other
fields' string values, so a confirmation field copies its password.

With a [pattern](https://github.com/jplauri/modelfill/blob/master/docs/supported.md#patterns),
every candidate must also match it. When none of the name's candidates does,
modelfill spells a value from the pattern before trying strings from elsewhere. If
a value can start with the name's first letter, it has the allowed length nearest
the name's and follows the name as far as the pattern lets it; otherwise it is the
shortest. Each other character is the first of `a`–`z`, `A`–`Z`, `0`–`9` that
fits:

| Field | Canonical value |
| --- | --- |
| `code: str = Field(pattern=r"^[A-Z]{3}$")` | `COD` |
| `city: str = Field(pattern=r"^[A-Z][a-z]+$")` | `City` |
| `sku: str = Field(pattern=r"^[A-Z]{3}-\d{4}$")` | `SKU-0000` |
| `zip: str = Field(pattern=r"^\d{5}$")` | `00000` |
| `phone: str = Field(pattern=r"^\+?[1-9]\d{1,14}$")` | `10` |

Canonical values are stable within a release but **not across versions**: a later
release may choose differently. A test should fix every field it asserts on.

### External fields

A field of a type the solver does not model, such as a UUID, a float, a list or a
nested model, takes its value from the first of these:

1. **Your value.** It goes to the constructor, which may convert it as usual (`5`
   to `5.0`). For a nested model, a dict fixes some of its fields and modelfill
   completes the rest; an instance is used as it is, so it must keep its own
   model's rules, which Pydantic does not check again. Dicts inside a given list,
   tuple or dict of models are completed the same way, keeping the shape given,
   and for a union of models the first model member that takes the dict does.
2. **A provider**, keyed by field path or by type; a path beats a type, and may lead
   into a list, tuple, dict or union of models, as `"lines.id"` does.
3. **The model's own default.**
4. **For a nested model, its canonical completion; otherwise a boring value**:
   `None` for an optional field, an empty collection, `0` for a float, a fixed UUID,
   and so on; [the full table](https://github.com/jplauri/modelfill/blob/master/docs/supported.md#boring-values)
   lists them. A required nested model is solved together with its parent, so the
   parent's rules can read its fields and a fixed parent field can determine them.
   Models in a list, tuple or dict, or in a union, are completed too.

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

A provider is a zero-argument callable, run once per returned object like a default
factory, so each batch member gets its own values. One that provides a required
nested model, which is solved with its parent, fixes it for the whole call, as an
instance or a dict, at any depth. Providers apply only to fields the solver does not
model, so one that targets a solved field, or a path that does not exist, fails when
the filler is created.

A field with no value from any source, such as one of an arbitrary class, raises
`UnsupportedConstraint`; pass a value or add a provider. If Pydantic keeps rejecting
a boring value, for example because of a custom validator, the search ends in
`GenerationExhausted`, which names the field. Validators that read external fields
are unsupported logic, and so is other code in their metadata, such as an
`AfterValidator` or `ValidateAs`: a model with either falls back to the bounded
search. So does a rule that reads an optional nested model or one with a default,
and any unsupported logic inside a nested model makes the whole object fall back.

### Batches

`batch(count)` returns `count` distinct completions, compared on the validated
objects' generated fields, in canonical order: the canonical completion, then the
canonical completion of what remains once each earlier one is excluded. It raises
`GenerationExhausted` when fewer distinct valid completions exist or can be found
within the budget. When each member gets its own provider values and unsupported
logic could judge candidates by them, a member first tries again, in order, the
candidates that earlier members tried.

### Diagnostics

`explain()` returns a fresh, JSON-friendly dictionary of what modelfill read: field
domains, patterns and defaults, each validator's extraction status, unsupported
reasons, whether the validators' constraints are active, and whether an
impossibility can be proven. In a model that falls back, the validators modelfill
could read report the status `guidance` rather than `modeled`.

```python
ModelFiller(Meeting).explain()
# {'model': 'Meeting', 'mode': 'auto',
#  'fields': {'start': {'type': 'int', 'choices': None, 'default': 'generated'}, ...},
#  'validators': [{'name': 'check', 'status': 'modeled', 'detail': 'pure rejection validator'}],
#  'unsupported': [], 'validator_constraints_active': True, 'can_prove_unsat': True,
#  'unsat_requires_explicit_fields': [], ...}
```

### Exploration with Hypothesis

Canonical values are for tests that need *a* valid object. To hunt bugs, explore
with `modelfill.hypothesis.from_model`, a Hypothesis strategy that needs the
`hypothesis` extra. Pass strategies for the fields you want Hypothesis to drive;
required external fields without a provider default to `st.from_type`, and
modelfill solves the rest, picking values at random, often at the edges of their
feasible ranges, where bugs tend to hide. Pass a configured `ModelFiller` in place
of the class to set its options, so they never collide with field names:

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

Draws that make the rules impossible, here a `break_minutes` outside 0–60, are
rejected like a `.filter()`, so draw the fields you choose from mostly possible
values, or the health check fails. The solved fields never need filtering, however
the rules tie them together. Each example uses a fresh solver seeded from the draw,
so failing examples replay and the drawn fields shrink as usual; the solved fields
replay too, but do not shrink.

## Reference

### API

```python
fill(Model, **fixed_fields)
ModelFiller(
    Model, *, mode="auto", max_attempts=100, work_limit=1_000_000, timeout_ms=1000, providers=None
)
filler.fill(**fixed_fields)
filler.batch(count, **fixed_fields)
filler.explain()
modelfill.hypothesis.from_model(Model_or_filler, **field_strategies)
```

| Option | Meaning |
| --- | --- |
| `mode` | `"auto"` falls back to a bounded search for logic modelfill cannot read; `"strict"` refuses such a model |
| `max_attempts` | how many candidates the bounded search tries |
| `work_limit` | each solver query's budget, in Z3's own resource units rather than time, so whether a query finishes never depends on machine speed |
| `timeout_ms` | a wall-clock safety net for each query, not a deadline for a whole fill, which makes several queries per field |
| `providers` | zero-argument callables for external fields, keyed by field path or by type |

- `fill()` takes **canonical field names**, even when a model uses input aliases.
  The options live on `ModelFiller`, so fields named `mode`, `model`, `count` or
  `self` work.
- A solved field takes an exact Python value: an `int`, `bool`, `str`, `date`, naive
  `datetime` or `time`, `timedelta`, a `Decimal` within its `decimal_places`, an
  Enum member, an allowed `Literal` value, or `None` for an optional field. There is
  no coercion: `"10"` or `True` for an `int` field, `Decimal("1.234")` for two
  places, or an aware datetime raises `TypeError`. An unknown field name raises
  `ValueError`.
- Fixed values are hard constraints, any subset of them solved together, and a
  returned object keeps both their type and value; validator normalization cannot
  silently replace them.
- Defaults are **preferences**, used where they fit the rules. A missing field's
  zero-argument default factory runs once per fill call, and standard defaults are
  copied.
- Every generated field, a selected default included, is passed to the constructor
  and validated, which is stricter than Pydantic's habit of leaving omitted defaults
  unchecked. So while a default Pydantic would leave unchecked is omitted, a
  contradiction is reported as `GenerationExhausted`, never as an impossibility.
  `explain()["unsat_requires_explicit_fields"]` lists such fields; fixing them, or
  Pydantic's `validate_default`, makes the proof possible.
- A dataclass runs its normal `__init__` and `__post_init__`. It gains no Pydantic
  validation; modelfill enforces only the solved field types.
- Compiled models are cached, so treat a model, and the Enum classes and constants
  its validators read, as immutable once filled; restart the process after
  monkeypatching or rebuilding one.

### Errors

| Exception | Meaning |
| --- | --- |
| `UnsatisfiableConstraints` | A clean solver check proves the supported constraints plus canonical fixed assignments contradictory. No random preferences, blocked candidates or artificial bounds contribute to this result. When the solver isolates the conflict, the message names the fixed inputs, field constraints and validators involved, such as `start=17, duration=4` against `end <= 18` and `Meeting.check`. |
| `UnsupportedConstraint` | An unsupported constructor shape, a required external field without a value, or unsupported validation in strict mode. |
| `GenerationExhausted` | The bounded search found no object that validates; also raised for a contradiction that unknown behavior or an omitted unchecked default keeps from being proven, and when the rules and a pattern cannot both hold. |
| `SolverTimeout` | A solver query exhausted its budget: its work limit, or the `timeout_ms` safety net. A subclass of `SolverUnknown`. |
| `SolverUnknown` | Z3 could not decide a query for another reason, or did not say why. |

All of them inherit from `ModelFillError`; incorrect arguments raise `TypeError` or
`ValueError`. A query that cannot finish raises rather than settling for a different
value, so raise `work_limit` if a heavy model hits `SolverTimeout`. The unit is Z3's
own, so a given limit buys about, not exactly, the same work across Z3 releases.

### What modelfill understands

At a glance;
[docs/supported.md](https://github.com/jplauri/modelfill/blob/master/docs/supported.md)
gives every form.

- **Models**: Pydantic v2 `BaseModel` and `RootModel`, and standard dataclasses
  with a pure `__post_init__`.
- **Solved fields**: `int`, `bool`, `str`, `date`, naive `datetime` and `time`,
  `timedelta`, `Decimal` with `decimal_places`, plain Enums and `Literal`s, each
  also `| None`, and behind a `NewType`, a `type` alias or `Annotated`.
- **Field constraints**: bounds, `multiple_of`, lengths, `pattern`, `max_digits` and
  `decimal_places`, also in groups such as `conint()` and `StringConstraints`.
- **Validators**: `@model_validator(mode="after")`, `@field_validator(mode="after")`,
  `AfterValidator` and a dataclass's `__post_init__`, written with `if`, `match`,
  `assert` and locals; comparisons and arithmetic; `abs()`, `min()`, `max()` and
  `len()`; `startswith()`, `endswith()` and substring `in`; date and time parts such
  as `.year`, `.weekday()` and `.hour`; and Enum members' `.value` and `.name`.
- **Everything else**: a field of another type is [external](#external-fields), and
  other validation logic is unsupported, as below.

### Unsupported logic and fallback

In strict mode, unsupported validation logic raises `UnsupportedConstraint` when
the filler is created. In auto mode it appears in `explain()["unsupported"]`, and
the validators modelfill could read are demoted to guidance, since unknown code may
transform values before another validator runs. Field constraints remain candidate
bounds, and normal construction still runs every original validator. A candidate it
rejects is excluded and the next canonical candidate is tried. Attempts alternate
between preferring the guidance and ignoring it, so misleading guidance cannot
starve the search, and the sequence is the same on every run. Guidance that leaves
no candidate at all, such as a value its pattern rules out, is dropped for the rest
of the call. The search tries at most `max_attempts` candidates and never claims
impossibility, and it can miss valid solutions, especially narrow conditions or
transformations.

A candidate counts as rejected only when construction turns it down the way its
kind of model does: with `ValidationError` from a Pydantic model, which wraps its
validators' errors, or with `ValueError` or `AssertionError` from a dataclass's
`__post_init__`. Any other exception propagates, so programming errors stay
visible, one from a default factory included. Validation is not sandboxed: keep
validators free of side effects and deterministic. Solver timeouts cannot
interrupt Python code.

## Development

```sh
uv sync
uv run pytest
uv run pytest tests/test_properties.py --hypothesis-profile=deep  # A longer random search.
uv run python -m benchmarks.benchmark --count 100 --repeat 5  # Local timings.
```

[CI](https://github.com/jplauri/modelfill/blob/master/.github/workflows/ci.yml) runs
the full set of checks. The
[design document](https://github.com/jplauri/modelfill/blob/master/docs/architecture.md)
covers the architecture, caching and threads, measured results, and comparisons
with Polyfactory and CrossHair.

## License

MIT; see [LICENSE](https://github.com/jplauri/modelfill/blob/master/LICENSE).
