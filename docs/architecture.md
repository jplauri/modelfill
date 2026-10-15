# modelfill design

modelfill compiles a deliberately small validation language, solves for missing
fields, then constructs the original model normally. It never modifies the model
class and never uses `model_construct()` as validation.

## Boundaries

1. **Model adapter:** discover fields, defaults and validators; convert the
   supported field metadata into domain constraints; construct instances through
   Pydantic or the dataclass constructor. Pydantic validator discovery needs its
   decorator registry, an internal API. That dependency belongs in the adapter,
   not the extractor or solver.
2. **AST extraction:** read validator source using `inspect`, dedent it and parse
   it with `ast`. Map field references and expressions into the IR. Accept only
   pure rejection logic whose complete relevant control flow can be understood.
3. **Typed IR:** frozen `Domain` and `Expr` values describe variables, constants,
   operations and resulting sorts. They contain no Z3 objects. Field constraints
   and validators use the same representation. A nullable domain (`X | None`)
   marks a field the solver models as a value plus an "is None" flag; `date`,
   `datetime` (naive) and `timedelta` are encoded as integers (days or
   microseconds), which preserves their order and turns Python's date arithmetic
   into exact integer arithmetic, bounded by the real `min`/`max` of each type.
   Fields of any other type are external: the solver never sees them.
4. **Z3 backend:** compile IR into expressions, impose canonical fixed inputs,
   prove impossibility, and choose values: canonical ones by pinning each field
   to its best allowed value, or explored ones from random preferences. Choices
   are separate from the hard constraints used to establish impossibility.
5. **Generation API:** manage reuse, cached canonical assignments and bounded
   retries; construct and validate every candidate, preserve fixed inputs, and
   explain unsupported logic and failures.

## Why control flow matters

Negating each `if` condition independently is incorrect. Consider:

```python
if self.a == 0:
    return self
if self.b < 0:
    raise ValueError("negative")
return self
```

The acceptance condition is `a == 0 or b >= 0`, rather than `b >= 0`. An accepted
early return ends that path. Nested branches contribute guarded constraints and
sequential statements only apply to paths that still reach them. A validator
that contains an unsupported operation is classified as a whole; extracting a
convenient subset could create a false UNSAT result if omitted code transforms
values or changes reachability.

Exception messages are not analyzed: they run only on a path that already
rejects, so their content cannot change acceptance. A failing `assert` rejects
like `raise`, unless `python -O` removed it. Local assignments must sit at the
top level of the validator, so every later read sees exactly one binding;
reassigning a parameter could disguise a transformed return value and is
unsupported.

Python's `and` and `or` return operands. They are logical connectives when their
results are tested for truth, but `(a or b) == 2` requires conditional operand
selection. The extractor preserves that distinction for supported types. Python
precedence comes from the parsed AST, rather than textual pattern matching.

Some operations raise for some inputs: `x < y` raises `TypeError` when `x` is
`None`, and date arithmetic past `date.max` raises `OverflowError`. The extractor
turns each into a requirement, so a path where one would raise rejects, exactly
as the exception does. Requirements respect evaluation order: the right side of
`x is not None and x < y`, or a later link of a chained comparison, only runs
when the earlier part allows it. That is what makes the usual Python guard
patterns for optional fields exact rather than conservative.

## Guarantees and budgets

`UnsatisfiableConstraints` means the sound modeled constraints and fixed
assignments have no solution. The backend establishes this without preferences,
pinned values or accumulated failed-candidate blocks. modelfill only makes this
claim when all discovered model behavior is supported. If anything is
unsupported, it demotes every extracted validator
predicate to soft guidance, keeps field constraints as candidate bounds, and
never reports UNSAT. This accounts for an unknown hook potentially transforming
a value before a supported validator runs. Guidance is preferred on alternate
attempts and only after a satisfiability check, so wrong guidance can waste
attempts but never rules out a candidate for good or reaches a verdict; the
original constructor decides. Diagnostics report both individual extraction results and
whether validator constraints are active.

An UNSAT verdict also explains itself. Only on that path, modelfill rebuilds
the assertions with tracking labels (fixed inputs, field constraints such as
`end <= 18`, type facts, and validators by name and line) and asks Z3 for a
minimized unsat core, so the success path pays nothing for explanations.

`strict` mode refuses unsupported model behavior. `auto` exposes it through
`explain()` and may try bounded candidates against the original constructor.
Failure of this search is `GenerationExhausted`, including when every remaining
candidate in a bounded or blocked search has been tried. Solver `unknown` and
exhausted budgets have separate exception types. The solver budget applies to
solver queries; it cannot interrupt arbitrary Python user code. Pure
deterministic, terminating validators are a precondition.

Fixed inputs use canonical types: exact `int`, `bool`, `str`, `date`, naive
`datetime` or `timedelta`, actual enum members, supported literal values, and
`None` for optional fields. An integer input of `True` or `"10"` is rejected
instead of coercing it, as is an aware datetime: mixing aware and naive values
would make comparisons raise. This keeps the relation between supplied values,
solver values and returned values explicit. Ordinary defaults are preferences
that may be replaced when necessary to satisfy a relationship; explicit inputs
are hard constraints.

Every field is supplied explicitly to the original constructor, including values
chosen from defaults. Pydantic therefore validates these defaults as ordinary
inputs even when the original model leaves `validate_default=False`. A model
whose unchecked default would fail a field constraint or field validator can
accept `Model()` while rejecting every fully supplied completion. To avoid a
false global impossibility claim, modelfill reports `GenerationExhausted` when
the solver finds no solution and such a default was omitted from the fixed
inputs. A default counts only if it could fail: one that satisfies its field's
modeled constraints, on a field without field validators, cannot bypass anything
(this covers the common `None` on optional fields). Fixing the remaining fields
explicitly, or enabling Pydantic's `validate_default`, permits an UNSAT proof.
Diagnostics list them under `unsat_requires_explicit_fields`. modelfill never
returns an unchecked default merely because ordinary Pydantic construction
permits it.

External fields are the exception that makes real models usable: their values
come from the caller, or the model's own default applies, and they pass to the
constructor unchanged. They cannot weaken an UNSAT claim, because no extracted
predicate can mention them: a validator that reads an external field is
unsupported, which already rules out UNSAT.

Z3 strings are restricted to code points U+0000 through U+2FFFF. Fixed strings
outside that range are explicitly unsupported. Literal backslashes are escaped
when crossing the solver boundary, and solver escape decoding uses one pass so a
Python string such as `r"\u{41}"` remains literal text.

## Canonical completion

Canonical completion is incremental pinning on the same solver that proves
impossibility. In a session holding the fixed inputs, guidance (on guided
attempts) and then defaults are pinned where they fit, in declaration order.
Every remaining field is resolved in declaration order and pinned before the
next: `None` for an optional field, the first allowed choice, `False`, the
integer encoding nearest its anchor, or a readable string. The nearest integer
comes from plain satisfiability probes: the anchor itself, then the least
allowed value above it, by bisection that starts from the model's own value
(often the minimum already), then a strictly closer value below. Strings try
readable candidates in a fixed order. Each answer is the unique best value under
the rule, so it depends neither on which model Z3 returns nor on solver history
or the Z3 version. The price is around a dozen small checks for the three-field
meeting; bisection is logarithmic in the gap between anchor and value.

`Optimize` with lexicographic priorities could fold the integer fields into one
query, but strings are not objectives, so the problem would split at every
string field; `None`, ordered choices and ties need extra objective encodings;
and the optimizer is a separate engine with its own performance and budget
behavior. Pinning keeps one engine and one budget rule for proofs and choices.

Defaults are settled before any anchor rather than interleaved with them in
declaration order. A default that fits therefore always wins, wherever its field
is declared, so reordering fields never changes the result. Interleaving would
drop a valid default whenever an earlier field's anchor conflicts with it: if
`balance >= minimum_balance` and `minimum_balance` defaults to 100, interleaving
gives `balance=0, minimum_balance=0` but the reverse declaration order gives 100
and 100. The cost of settling defaults first is that fixing a defaulted field can
release an earlier field that its default held: fixing `minimum_balance=0` moves
`balance` from 100 to 0 even though 100 would still be valid.

Every solver query runs under a per-check Z3 resource limit (`rlimit`), the
`work_limit` option, which counts work rather than time and so decides the same
way on any machine. The test suite's heaviest check uses about 2,500 of its
default 1,000,000 units; sustained solving burns 2,000-7,000 units per
millisecond on a desktop, so the default is roughly 0.15-0.5 s of work, inside
the 1 s default of `timeout_ms`, which remains a wall-clock safety net. The
option exists because the limit is what decides: a heavy model that hits it
needs a way to raise it. A canonical probe that cannot finish raises
`SolverTimeout` or `SolverUnknown` rather than settling for another value, so a
returned value never depends on machine speed. Resource use does depend on
solver history, so close to the limit, whether a query finishes can depend on
earlier calls; it then raises, and never returns a different value.

The answer depends only on the fixed inputs and default values, so each thread
caches canonical assignments by them (bounded): a repeated fill reruns only
construction, and default factories still run once per call. `batch` excludes
each completion in turn, enumerating distinct completions in canonical order.
Reusing a `ModelFiller` avoids repeated analysis. Each thread receives its own
solver and private Z3 context; only compiled immutable information is shared.
Compilation and the top-level convenience filler use bounded LRU caches keyed by
model class.

## CrossHair decision

[CrossHair's implementation guide](https://crosshair.readthedocs.io/en/latest/how_does_it_work.html)
describes repeated execution with symbolic Python values backed by Z3. It chooses
and records branch decisions to explore execution paths, without AST analysis.
Its machinery addresses richer Python behavior, aliasing, conversions and
semantic differences between Python and the solver.

That is valuable prior art, but modelfill needs a bounded, auditable grammar and
reusable constraints rather than general path exploration. Direct AST extraction
keeps the supported language explicit and avoids introducing tracing and
interception machinery as a runtime dependency. This is an architectural scope
decision, not a benchmark claim that AST extraction outperforms CrossHair.

[CrossHair's contract documentation](https://crosshair.readthedocs.io/en/latest/contracts.html)
also notes that it executes analyzed code and that side-effect protection is
incomplete. modelfill's extraction does not execute validator bodies; only final
ordinary validation does. It likewise does not sandbox validation code.

## Polyfactory comparison

Polyfactory remains useful for generating diverse data with custom factories.
Its documented [`post_generated` decorator](https://polyfactory.litestar.dev/latest/usage/decorators.html)
uses a user-written function whose parameter names declare dependent fields.
That can express a range endpoint derived from a start point. modelfill's narrow
distinction is inferring the relation from an existing supported validator and
solving it in either direction: fixing an endpoint and duration can determine the
start. This does not claim that modelfill replaces Polyfactory's larger type
coverage or factory customization.

## Exploration lives in Hypothesis

Edge-biased random values find bugs, but in fields a test does not care about
they cause failures unrelated to the test's intent. Exploration is also only
useful with shrinking, replay and a failure database. Hypothesis has all of
them, so modelfill keeps no second, weaker engine on its default path: `fill` is
canonical, and the random sampler is reachable only through `from_model`.

`from_model` lets Hypothesis draw the fields a test cares about and any required
external fields, then solves the rest with that sampler: seeded soft preferences
in shuffled field order, discarded when infeasible. Integer, date and time
targets land inside each field's feasible range and often on its edges, from one
box-mode optimization per set of fixed inputs, plus one per optional field over
its non-None values (adding "not None" to the shared query would force every
optional field to be present at once). A missing bound, from an unbounded side
or a timed-out query, falls back to steps from the other bound or a window
around typical values. Choices are uniform, optional fields are `None` a quarter
of the time, and strings are random lowercase text. Each example builds a fresh
solver seeded from a Hypothesis draw, because an incremental solver's answers
depend on its history; a fresh one makes every example a pure function of the
draws, so failures replay and drawn fields shrink; the solved fields replay but
do not shrink. A proven contradiction becomes `reject()`. Hypothesis counts that
as filtering, so drawn fields that are mostly impossible still trip its health
check. The benefit is narrower: Hypothesis draws only the fields a test chooses,
and modelfill solves the dependent ones, so complete objects never have to be
drawn independently and filtered for cross-field rules.

## Verification and measurement

Acceptance tests exercise canonical values and their independence from call
order, threads and time budgets, bidirectional completion, coupled constraints,
dataclasses, canonical fixed inputs, defaults, optional, date and external
fields, and bounded fallback. Exhaustive small-domain differential tests compare
fixed assignments against ordinary Python validation, especially branch
reachability, Boolean operands, `None` guards and date arithmetic.

Property tests generate inputs and check them against oracles independent of
modelfill: random validators within the supported grammar against plain Python,
random metadata combinations against Pydantic, every impossibility claim for
partial meetings against brute force, plus date and string round trips through
the solver and the stability of canonical values. Final validation protects only
the objects modelfill returns; a false impossibility claim never reaches the
constructor, so these oracles are what guard the UNSAT guarantee. CI runs a
repeatable 100 examples per property; the `deep` profile searches longer at
random.

`uv run python -m benchmarks.benchmark --count 100 --repeat 5` reports local
medians. Cold compiles and solves; cold measurements create fresh Pydantic
subclasses to avoid the model cache, so they also include class creation
overhead. Warm repeats a fill, which reuses the cached canonical assignment.
Solve clears that cache first, as for fills whose fixed inputs keep changing.
All measurements include original validation. These numbers are
environment-specific and are not comparisons with other libraries.

One measured run on Windows AMD64, Python 3.12.10, Pydantic 2.14.1 and Z3 4.16.0
(100 instances per run, median of five runs) produced:

| Operation | Total | Per instance |
| --- | ---: | ---: |
| Cold | 850.53 ms | 8.505 ms |
| Warm | 0.61 ms | 0.006 ms |
| Solve | 258.80 ms | 2.588 ms |
