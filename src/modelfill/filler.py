"""Compilation, bounded generation, and the original validation boundary."""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from .adapters import MISSING, Adapter, FieldSpec, introspect
from .backend import Solver
from .errors import GenerationExhausted, UnsatisfiableConstraints, UnsupportedConstraint
from .extract import extract_validator
from .ir import Domain, Expr, encodable


@dataclass(frozen=True)
class _Compiled:
    adapter: Adapter
    domains: dict[str, Domain]  # Generated fields only; external fields pass through.
    constraints: tuple[tuple[str, Expr], ...]  # (label, rule)
    guidance: tuple[Expr, ...]
    validators: tuple[tuple[str, str, str], ...]
    unsupported: tuple[str, ...]
    unchecked: tuple[str, ...]  # Generated fields whose defaults Pydantic does not validate.


@lru_cache(maxsize=128)
def _compile(model: type) -> _Compiled:
    adapter = introspect(model)
    domains = {field.name: field.domain for field in adapter.fields if field.domain is not None}
    predicates: list[tuple[str, Expr]] = []
    diagnostics = []
    unsupported = list(adapter.unsupported)
    for validator in adapter.validators:
        name = validator.name
        if validator.field_name:
            name += f"[{validator.field_name}]"
        try:
            if validator.mode != "after":
                raise UnsupportedConstraint(f"{name}: mode={validator.mode!r} is unsupported")
            predicate = extract_validator(
                validator.function,
                domains,
                kind=validator.kind,
                field_name=validator.field_name,
                earlier_fields=validator.earlier_fields,
            )
        except UnsupportedConstraint as exc:
            unsupported.append(str(exc))
            diagnostics.append((name, "unsupported", str(exc)))
        else:
            predicates.append((_label(validator.function), predicate))
            diagnostics.append((name, "modeled", "pure rejection validator"))
    # An unknown hook may transform values before another validator runs, so its
    # neighbors' predicates may then only guide the search, never constrain it.
    constraints = adapter.constraints + (() if unsupported else tuple(predicates))
    guidance = tuple(rule for _, rule in predicates) if unsupported else ()
    unchecked = tuple(f.name for f in adapter.fields if f.unchecked_default and f.name in domains)
    return _Compiled(
        adapter, domains, constraints, guidance, tuple(diagnostics), tuple(unsupported), unchecked
    )


def _label(function: Any) -> str:
    """A validator as named in explanations, with the line where its definition starts."""
    code = getattr(function, "__code__", None)
    line = f" (line {code.co_firstlineno})" if code is not None else ""
    return f"{getattr(function, '__qualname__', function)}{line}"


def _contradiction(model: str, given: list[str], rules: list[str]) -> str:
    if not rules:
        return f"{model}: constraints contradict the fixed inputs"
    if not given:
        return f"{model}: no values satisfy {', '.join(rules)}"
    return f"{model}: {', '.join(given)} conflict with {', '.join(rules)}"


def _describe(field: FieldSpec) -> dict[str, Any]:
    domain = field.domain
    if domain is None:
        default = "required input" if field.required else "model default"
        return {"type": "external", "choices": None, "default": default}
    has_default = field.default is not MISSING or field.default_factory is not None
    return {
        "type": domain.kind + (" | None" if domain.nullable else ""),
        "choices": None if domain.choices is None else [repr(value) for value in domain.choices],
        "default": "preference" if has_default else "generated",
    }


class ModelFiller[T]:
    """Compile once, complete repeatedly, and validate every returned object.

    ``strict`` rejects unsupported logic at initialization. ``auto`` (default)
    performs bounded, validated fallback when unsupported logic is discovered.
    Completions are canonical: the same inputs always give the same values, which
    are as boring as the rules allow. ``work_limit`` bounds each solver check in Z3
    resource units, which count work rather than time, so it decides the same way on
    every machine; raise it if a heavy model hits ``SolverTimeout``. ``timeout_ms``
    is a wall-clock safety net; validators must terminate themselves. Fixed values
    use canonical Python types without coercion. Defaults are preferences, used when
    compatible with the rules and fixed values.
    """

    def __init__(
        self,
        model: type[T],
        *,
        mode: Literal["strict", "auto"] = "auto",
        max_attempts: int = 100,
        work_limit: int = 1_000_000,
        timeout_ms: int = 1000,
    ) -> None:
        if mode not in ("strict", "auto"):
            raise ValueError("mode must be 'strict' or 'auto'")
        for name, value in (
            ("max_attempts", max_attempts),
            ("work_limit", work_limit),
            ("timeout_ms", timeout_ms),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.model = model
        self.mode = mode
        self.max_attempts = max_attempts
        self.work_limit = work_limit
        self.timeout_ms = timeout_ms
        self._compiled = _compile(model)
        if mode == "strict" and self._compiled.unsupported:
            raise UnsupportedConstraint("; ".join(self._compiled.unsupported))
        self._local = threading.local()

    def _solver(self) -> Solver:
        if not hasattr(self._local, "solver"):
            self._local.solver = Solver(
                self._compiled.domains,
                [rule for _, rule in self._compiled.constraints],
                timeout_ms=self.timeout_ms,
                work_limit=self.work_limit,
                guidance=self._compiled.guidance,
            )
            self._local.cache = {}  # Canonical assignments by fixed values and defaults.
        return self._local.solver

    def fill(self, /, **fixed_fields: Any) -> T:
        """The canonical completion of canonical field-name inputs, keeping every fixed value."""
        return self._complete(fixed_fields, 1)[0]

    def batch(self, count: int, /, **fixed_fields: Any) -> list[T]:
        """count distinct completions in canonical order, compared on generated fields."""
        if type(count) is not int or count < 0:
            raise ValueError("count must be a nonnegative integer")
        return self._complete(fixed_fields, count) if count else []

    def _complete(
        self, fixed_fields: dict[str, Any], count: int, rng: random.Random | None = None
    ) -> list[T]:
        """count validated completions: canonical ones, or explored with rng (Hypothesis)."""
        adapter, domains = self._compiled.adapter, self._compiled.domains
        adapter.check_fixed(fixed_fields)
        # The solver sees generated fields; external values go straight to the constructor.
        solved = {name: value for name, value in fixed_fields.items() if name in domains}
        defaults = {}
        for field in adapter.fields:
            if field.name in domains and field.name not in fixed_fields:
                value = field.preferred()
                # A default the solver cannot represent is simply not preferred.
                if domains[field.name].accepts(value) and encodable(value):
                    defaults[field.name] = value
        solver = self._solver()
        # A canonical completion depends only on these, so repeated fills reuse it.
        key = (frozenset(solved.items()), tuple(defaults.items()))
        cache = self._local.cache
        if rng is None and count == 1 and key in cache:
            try:
                return [self._construct(fixed_fields, cache[key])]
            except (ValueError, AssertionError):
                del cache[key]  # Validation is meant to be deterministic; search again.
        last_error: Exception | None = None
        omitted = [name for name in self._compiled.unchecked if name not in fixed_fields]
        results: list[T] = []
        failures = 0
        with solver.session(solved):
            # This first check has no preferences or candidate exclusions.
            if not solver.check():
                if self._compiled.unsupported:
                    raise GenerationExhausted(
                        "Modeled guidance has no candidate; "
                        "unsupported logic prevents an UNSAT claim"
                    )
                if omitted:
                    raise GenerationExhausted(
                        "No fully validated completion; omitted unchecked Pydantic defaults "
                        f"prevent an UNSAT claim: {', '.join(omitted)}"
                    )
                given, rules = solver.conflict(solved, self._compiled.constraints)
                raise UnsatisfiableConstraints(_contradiction(self.model.__name__, given, rules))
            while len(results) < count and failures < self.max_attempts:
                if (results or failures) and not solver.check():
                    break  # Every remaining candidate has been tried.
                # Alternate, so guidance misled by an unknown hook cannot starve the search.
                guided = failures % 2 == 0
                if rng is None:
                    candidate = solver.canonical(solved, defaults, guided=guided)
                else:
                    candidate = solver.candidate(solved, defaults, rng, guided=guided)
                solver.block(candidate)  # Retries and batch members must differ.
                try:
                    results.append(self._construct(fixed_fields, candidate))
                except (ValueError, AssertionError) as exc:
                    failures, last_error = failures + 1, exc
                    continue
                failures = 0
                if rng is None and count == 1:
                    if len(cache) >= 256:  # Bounded: forget the oldest assignment.
                        del cache[next(iter(cache))]
                    cache[key] = candidate
        if len(results) == count:
            return results
        found = f"found {len(results)} of {count} distinct completions; " if count > 1 else ""
        reason = (
            f"no validated completion within {self.max_attempts} attempts"
            if failures >= self.max_attempts
            else "no candidates remain"
        )
        message = f"{self.model.__name__}: {found}{reason}"
        if self._compiled.unsupported:
            message += "; unsupported logic: " + "; ".join(self._compiled.unsupported)
        if last_error is not None:
            message += f"; last rejection: {last_error}"
        raise GenerationExhausted(message) from last_error

    def _construct(self, fixed_fields: dict[str, Any], candidate: dict[str, Any]) -> T:
        """Run the original constructor; a changed or noncanonical result is a rejection."""
        instance = self._compiled.adapter.construct({**fixed_fields, **candidate})
        invalid = [
            name
            for name, domain in self._compiled.domains.items()
            if not domain.accepts(getattr(instance, name, MISSING))
        ]
        changed = [
            name
            for name, value in fixed_fields.items()
            if type(getattr(instance, name, MISSING)) is not type(value)
            or getattr(instance, name, MISSING) != value
        ]
        if isinstance(instance, self.model) and not invalid and not changed:
            return instance
        raise ValueError(
            f"Validation changed fixed fields {changed} or returned noncanonical "
            f"fields {invalid} / an incompatible object"
        )

    def explain(self) -> dict[str, Any]:
        """Return fresh, JSON-friendly compilation and generation diagnostics."""
        compiled = self._compiled
        return {
            "model": self.model.__qualname__,
            "mode": self.mode,
            "fields": {field.name: _describe(field) for field in compiled.adapter.fields},
            "validators": [
                {
                    "name": name,
                    "status": "guidance" if compiled.guidance and status == "modeled" else status,
                    "detail": detail,
                }
                for name, status, detail in compiled.validators
            ],
            "unsupported": list(compiled.unsupported),
            "validator_constraints_active": not bool(compiled.unsupported),
            "can_prove_unsat": not compiled.unsupported and not compiled.unchecked,
            "unsat_requires_explicit_fields": list(compiled.unchecked),
            "selection": "canonical",
            "max_attempts": self.max_attempts,
            "work_limit_per_check": self.work_limit,
            "timeout_ms_per_check": self.timeout_ms,
        }


@lru_cache(maxsize=128)
def _default_filler[T](model: type[T]) -> ModelFiller[T]:
    return ModelFiller(model)


def fill[T](model: type[T], /, **fixed_fields: Any) -> T:
    """The canonical completion of a model, with cached automatic-mode compilation.

    Use ModelFiller for strictness or execution-budget settings. All keyword
    arguments here are model fields, including names such as ``mode``.
    """
    return _default_filler(model).fill(**fixed_fields)
