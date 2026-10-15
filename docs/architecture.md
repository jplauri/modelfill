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
   ask for satisfying assignments and decode values. Generation preferences are
   separate from the hard constraints used to establish impossibility.
5. **Generation API:** manage reuse, random preferences and bounded retries;
   construct and validate every candidate, preserve fixed inputs, and explain
   unsupported logic and failures.

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
assignments have no solution. The backend establishes this without random
preferences, arbitrary sampling ranges or accumulated failed-candidate blocks.
modelfill only makes this claim when all discovered model behavior is supported. If anything is unsupported, it demotes every extracted validator
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
timeouts have separate exception types. The solver budget applies to solver
queries; it cannot interrupt arbitrary Python user code. Pure deterministic,
terminating validators are a precondition.

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

Sampling is deliberately nonuniform. Seeded preferences encourage different
solutions but do not guarantee uniqueness or statistically uniform coverage.
For integer, date and time fields, one box-mode optimization finds each field's
feasible minimum and maximum (cached per set of fixed inputs), so targets land
inside real ranges and often on their edges, rather than in a fixed window
around zero that a range such as 1,000–1,000,000 would never meet. An optional
field's range covers its non-None values, so it gets a query of its own: adding
"not None" to the shared query would force every optional field to be present at
once. A missing bound, from an unbounded side or a timed-out query, falls back to
steps from the other bound or a window around typical values.
Reusing a `ModelFiller` avoids repeated analysis. Each thread receives its own
solver, private Z3 context and random generator; only compiled immutable
information is shared. Reproducibility is scoped to the same model,
configuration, dependency versions and serial call order within a thread. Threads
given the same seed may produce duplicate sequences. Compilation and the
top-level convenience filler use bounded LRU caches keyed by model class.

Z3 strings are restricted to code points U+0000 through U+2FFFF. Fixed strings
outside that range are explicitly unsupported. Literal backslashes are escaped
when crossing the solver boundary, and solver escape decoding uses one pass so a
Python string such as `r"\u{41}"` remains literal text.

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

## Hypothesis integration

modelfill is positioned as a plugin rather than a replacement: `from_model`
lets Hypothesis draw the fields a test cares about and any required external
fields, then solves the rest. Each example builds a fresh solver seeded from a
Hypothesis draw, because an incremental solver's answers depend on its history;
a fresh one makes every example a pure function of the draws, so failures replay
and shrink. A proven contradiction becomes `reject()`, which filters impossible
draws without the excessive-filtering failures that `.filter()` would cause for
cross-field rules.

## Verification and measurement

Acceptance tests exercise bidirectional completion, coupled constraints,
dataclasses, canonical fixed inputs, defaults, optional, date and external
fields, and bounded fallback. Exhaustive small-domain differential tests compare
fixed assignments against ordinary Python validation, especially branch
reachability, Boolean operands, `None` guards and date arithmetic.

`uv run python -m benchmarks.benchmark --count 100 --repeat 5` reports local
medians for cold compilation and warm generation. Cold measurements create fresh
Pydantic subclasses to avoid the model cache, so they also include class
creation overhead. All measurements include original validation. These numbers
are environment-specific and are not comparisons with other libraries.

One measured run on Windows AMD64, Python 3.12.10, Pydantic 2.14.1 and Z3 4.16.0
(100 instances per run, median of five runs) produced:

| Operation | Total | Per instance |
| --- | ---: | ---: |
| Cold | 1060.30 ms | 10.603 ms |
| Warm | 117.79 ms | 1.178 ms |
