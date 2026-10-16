# Changelog

## Unreleased

### Added

- String patterns: a `pattern` in `Field`, `constr` or `StringConstraints` no
  longer makes a model fall back. Pydantic's own validation decides what matches,
  under either regex engine, and canonical values are spelled from the pattern,
  following the field's name where it fits: `^[A-Z]{3}$` gives a `code` field
  `COD`, and `^\d{5}$` gives `00000`. A fixed value that breaks a pattern is a
  proven contradiction, and exploration draws random matching values.
- Constraint groups such as `conint()`, `constr()`, `StringConstraints` and
  `Interval` are read as their single constraints, and the `regex_engine` setting
  is supported.

### Fixed

- When the search runs out of readable strings, the error keeps its usual details,
  such as the unsupported logic and the last rejection.
- A validator that reads a parameter such as `self`, `info` or `cls` as a bare
  name, or an unknown name, is unsupported logic. Such a name used to read as
  `None`, so `if info is None: raise ValueError(...)` counted as always rejecting
  and modelfill claimed the model impossible.
- A reused filler, including the cached one behind `fill()`, no longer returns an
  earlier call's values when external values decide which candidate validates:
  only a completion that validates on the first try is cached.
- A given nested instance that breaks its own model's rules, such as `Account()`
  keeping an unchecked default `balance=-1` despite `ge=0`, raises `ValueError`
  instead of a false `UnsatisfiableConstraints`. Pydantic does not check such an
  instance again, but modelfill solves with its rules.
- `batch()` compares the validated objects, so a validator that normalizes
  different candidates to the same value no longer yields duplicates.
- When Pydantic rejects a value modelfill chose for an external field, the search
  goes on instead of giving up: a validator of that field may read solved fields
  through `info.data`. If the search ends, the error still names the field.

## 0.3.0

Most models now fill without arguments, and rules can read the fields of nested
models.

### Added

- Fields the solver does not model get values too, so most models fill without
  arguments. A nested model gets its own canonical completion; other types get
  fixed, readable boring values, such as a zero UUID with the right version bits,
  empty collections, `None` and `0.0`.
- A dict for a nested model field fixes some of its fields, and modelfill
  completes the rest: `fill(Order, customer={"age": 30})`.
- `ModelFiller(providers=...)` supplies values for such fields by type or by field
  path, such as `"customer.email"`. A path beats a type, and a provider beats the
  model's default.
- `explain()` reports where each such field's value comes from.
- Rules across nested models: a required nested model is solved with its parent.
  Validators may read `self.customer.age`, a fixed parent field can determine
  nested fields and the other way round, and impossibility is proven across the
  whole object, with explanations by path. Optional nested models and those with
  defaults are still completed on their own.

### Fixed

- A value that Pydantic converts for a field the solver does not model, such as
  `total=5` for a float, is no longer rejected as a changed fixed value.

## 0.2.0

modelfill is now convenience-first: fields a test does not fix get boring, valid,
deterministic values, and bug hunting is an explicit opt-in through Hypothesis.

### Breaking changes

- `fill()` and `ModelFiller.fill()` return the canonical completion instead of a
  random one. It is a pure function of the model, the fixed inputs, the modelfill
  version and the model's default factories, and does not depend on call order,
  earlier calls, threads or machine speed. Canonical values may change between
  releases, so tests should fix every field they assert on.
- `ModelFiller` no longer accepts `seed`. `explain()` no longer reports it and
  reports `"selection": "canonical"` instead.
- `batch(count)` returns `count` distinct completions in canonical order instead
  of independent, possibly repeated fills. It raises `GenerationExhausted` when
  fewer distinct completions exist or can be found within the budget.
- Every solver query runs under a deterministic Z3 work limit, set by the new
  `work_limit` option (1,000,000 resource units per check by default), with
  `timeout_ms` as a wall-clock safety net. A canonical query that cannot finish
  raises `SolverTimeout` or `SolverUnknown` instead of silently settling for
  another value. `explain()` reports `work_limit_per_check`.
- The random, edge-biased sampler is reachable only through
  `modelfill.hypothesis.from_model`, which explores as before.

### Changes

- A repeated fill with the same fixed inputs and defaults reuses its cached
  canonical assignment, so it only reruns construction.
- Auto-mode fallback tries candidates in a deterministic canonical order.

### Fixes

Each of these could claim a valid assignment impossible:

- When a field repeats one kind of constraint, the last one now wins, as in
  Pydantic, instead of all of them applying. For example, `Field(ge=10)` on an
  `Annotated[int, Ge(0)]` alias leaves `ge=0` in force.
- Solver symbols use internal names, so a field name such as `"a is None"` can no
  longer collide with another field's None flag. A field named `part0` also no
  longer corrupts the explanation of a genuine contradiction.
- A dataclass `__post_init__` counts as a validator only if the generated
  `__init__` calls it. The hook of a subclass that was not decorated again never
  runs, so it no longer restricts values.

The documentation also no longer claims that rejected Hypothesis draws avoid
filtering: Hypothesis counts them toward its health checks.

## 0.1.0

Initial release.
