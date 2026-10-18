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
   A date's `.weekday()` is its day number's remainder by 7, and its `.year`
   counts the whole 400-, 100-, 4- and 1-year cycles in it, as `datetime` does:
   division by constants, which Z3 keeps linear. Clock parts such as `.hour`, and a
   datetime's `.date()` and `.time()`, divide the microseconds the same way.
   A `Decimal` with `decimal_places` counts units of its last place, so `12.34`
   with two places is 1234 hundredths: operations line operands up on the finer
   grid, and a product's places are its operands' places added.
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
like `raise`, unless `python -O` removed it. A local that branches set differently
holds, after them, the value of the branch taken: a conditional value the solver
already models. Where only some paths set it, a read on the others raises
`UnboundLocalError`, which rejects like the requirements below. `match` is the same
chain of tests as `if`/`elif`, one per case. Reassigning a parameter could disguise
a transformed return value and is unsupported.

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
original constructor decides. The check cannot see a pattern, so guidance may
fit and still leave no matching string; that attempt then goes without it, and so
do the rest, since excluding more candidates never brings one back. Diagnostics report both individual extraction results and
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
`datetime` or `timedelta`, a `Decimal` on its field's grid, actual enum members,
supported literal values, and `None` for optional fields. An integer input of `True` or `"10"` is rejected
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

Decimals are exact only within the decimal context's precision, 28 digits by
default: past it, Python rounds a result without a word. Comparisons stay exact at
any size, so validators may compare any decimals, but arithmetic is modeled only
where digit bounds prove Python exact. Each decimal sort bounds the digits of its
units: `max_digits` for a field, the count for a constant, one digit more for a
sum and the digits added for a product. A result that could outgrow the
precision is unsupported. Pydantic's `multiple_of` divides in that precision too,
so where a quotient is too large to divide exactly, the rule leaves the value to
Pydantic, and no UNSAT proof rests on a rounded division.

External fields are the exception that makes real models usable. Their values
come from the caller, a provider, the model's own default, a nested model's
canonical completion or a fixed boring value, in that order, and they pass to the
constructor, which validates them as usual; only solved fields must keep their
exact type. They cannot weaken an UNSAT claim, because no extracted predicate can
mention them: a validator that reads an external field is unsupported, which
already rules out UNSAT. Other code in an external field's metadata, such as
`ValidateAs`, is unsupported too: on a required nested model it could replace the
object whose fields the parent's rules read, after that model's own rules held
for the original. Groups are unpacked first, as Pydantic unpacks them, and only
markers known to constrain, tag or serialize a value stay supported; coming from
Pydantic is not enough, since `ValidateAs` runs a function it is given. An
`AfterValidator`, on any field, gets the field's validated value as an after
field validator does, so it is analyzed as one: only a pure check, which returns
the value it was given, is modeled. A nested model is completed by its own
filler, so a contradiction among a nested dict's fields is proven by that
filler's solver and is a genuine impossibility for the outer model too. When Pydantic rejects a
boring or provided value, the search goes on: an error's location says where it
was reported, not what it depends on, and a validator of that field may read
solved fields through `info.data`. If the search ends, the error names the field.

Z3 strings are restricted to code points U+0000 through U+2FFFF. Fixed strings
outside that range are explicitly unsupported. Literal backslashes are escaped
when crossing the solver boundary, and solver escape decoding uses one pass so a
Python string such as `r"\u{41}"` remains literal text.

## Nested models

A required nested model is solved with its parent rather than beside it. Its
compiled fields, field constraints and validator predicates join the parent's
under paths such as `customer.age`, at the nested field's place in declaration
order, so the parent's rules can read `self.customer.age` and fixing either side
can determine the other. The extractor resolves such attribute chains to those
paths; reading the nested object as a whole stays unsupported, because the
solver only knows its fields. Construction builds each nested object from its
solved values and its own external fields, innermost first, then the parent. A
given nested instance is used as it is, and its solved values become fixed, so
the parent's rules see them. Pydantic does not validate it again by default, so
an instance that breaks its own model's rules, such as one keeping an unchecked
default, would turn those rules into a false contradiction; it is refused as an
input error instead. Dropping its rules for that call would be more faithful, but
only under its model's default `revalidate_instances="never"`.

Unsupported logic inside a nested model counts for the whole object: an unknown
hook there could change values the parent's rules read, so every predicate is
demoted to guidance, as for unsupported logic in the parent itself. Optional
nested models and those with defaults are completed on their own, and a rule
that reads one is unsupported. A model that contains itself is never solved this
way, so filling it reports the problem.

Models inside a list, tuple or dict, or in a union, are completed on their own
too. The boring-value walk takes a callback that fills a model wherever the walk
meets one, so the shapes it already knows, min_length elements and distinct keys,
hold models without a second walker: each element is a fresh canonical completion,
and a union whose members have no boring value takes its first member that fills.
Boring values still come first, so an empty list stays empty. A given value is
walked the same way, completing each dict that stands for a model and keeping the
shape. Such fields stay external: no rule can read across their elements.

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
readable candidates in a fixed order, among them the name inside the strings a rule
tests the field to start with, end with or contain, which Z3 models exactly as
`PrefixOf`, `SuffixOf` and `Contains`. Each answer is the unique best value under
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

The first candidate depends only on the fixed inputs and default values, so each
thread caches it by them (bounded) when it validates on the first try: whenever
it validates again, a fresh search would return it too, so a repeated fill reruns
only construction, and default factories still run once per call. A later
candidate is never cached, because external values, which the key leaves out, may
have rejected the ones before it. `batch` excludes
each completion in turn, enumerating distinct completions in canonical order.
Exclusions outlive a member's provider values only while the model is fully
modeled, since then external values cannot change which candidates validate or
what they become. Under unsupported logic, a member whose values differ first tries
again, in order and without spending attempts, the candidates tried before.
Reusing a `ModelFiller` avoids repeated analysis. Each thread receives its own
solver and private Z3 context; only compiled immutable information is shared.
Compilation and the top-level convenience filler use bounded LRU caches keyed by
model class.

## String patterns

A string field's pattern stays out of Z3. As a Z3 regular expression, with each
character of a canonical value pinned by probes, every pattern using `\d` (71
Unicode digit ranges) exhausted the default work limit, and the other realistic
patterns tried took 22 ms to 1.5 s per value. Instead the solver ignores the
pattern and therefore allows more values than Pydantic does, never fewer, so a
proof of impossibility stays sound. Pydantic checks each candidate against the
pattern alone, which decides exactly, for either regex engine, flags and compiled
patterns included. The field's other metadata, such as a validator that reads
`info.data` or one that uppercases, runs only in the real constructor, which gives
it the model's data and keeps what it returns. A fixed value that fails the
pattern is a proven contradiction, unless unsupported logic, such as a before
validator, could change the value first, or the value comes from a given nested
instance, which is used as it is. When the rules and a pattern cannot both hold, the search ends
in `GenerationExhausted`; proving such conflicts would need the pattern in Z3.

Values are spelled in Python, in well under 3 ms for the same patterns. Python's
regex parser reads the pattern into a nondeterministic automaton for Pydantic's
search semantics: anything may come before and after a match, `^` and `$` are
edges that hold only at the very start and end, as in Rust, and under `(?i)` a
negated set also leaves out the case variants of what it names. For each length,
the automaton knows which states can still complete a match, so a depth-first
walk in a fixed character order never hits a dead end: the name's letters while
the value still spells the name, then `a`-`z`, `A`-`Z`, `0`-`9`, then each set's
least character. The length is the one nearest the name's if a value can start
with its first letter, and the shortest otherwise. Python's and Rust's syntax and
Unicode tables differ in corners, such as `\w` or nested sets, but a value the
automaton gets wrong is merely rejected by Pydantic. A pattern the parser cannot
read, or whose automaton would exceed 4096 states, is only checked. So is one that
takes more than 65536 sequences to read, since repeating something that reads
nothing, such as the empty group in `(){1000000}`, adds no state to count; an
empty repeated body, as in `(?:){1000000}`, simply reads as nothing. This bounds
compilation, which no solver budget covers.

Among a field's candidates, the first spelled value comes right after the
name's, so different fields keep values of their own; the strings that rules and
other fields mention come next, for when the rules exclude that value, and up to
255 further spelled values follow. Each length's first value is checked before Z3
is asked whether the field may have that length at all: Z3 4.16 needs about 3 s to
build a free string of 100 characters and gives up on longer ones, while checking
a given value takes milliseconds. So a 128-character digest still fills, but only
a value the rules reject leads Z3 into that slow question; a plain string field
that must be that long hits it already. Exploration prefers random members drawn
the same way.

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
partial meetings against brute force, values spelled and sampled from random
patterns against Pydantic, plus date and string round trips through the solver
and the stability of canonical values. Final validation protects only
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
