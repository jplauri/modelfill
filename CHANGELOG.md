# Changelog

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

## 0.1.0

Initial release.
