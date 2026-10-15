"""Compilation, bounded generation, and the original validation boundary."""

from __future__ import annotations

import random
import threading
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import ValidationError

from .adapters import MISSING, Adapter, FieldSpec, _domain, introspect
from .backend import Solver
from .boring import boring, nested_model
from .errors import (
    GenerationExhausted,
    ModelFillError,
    UnsatisfiableConstraints,
    UnsupportedConstraint,
)
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


def _check_path(model: type, path: str, rest: str | None = None) -> None:
    """A provider path leads through nested models to a field the solver does not model."""
    name, _, rest = (path if rest is None else rest).partition(".")
    field = next((field for field in _compile(model).adapter.fields if field.name == name), None)
    if field is None:
        raise ValueError(f"Provider {path!r}: {model.__name__} has no field {name!r}")
    if rest:
        nested = nested_model(field.annotation)
        if nested is None:
            raise ValueError(f"Provider {path!r}: {name!r} is not a nested model")
        _check_path(nested, path, rest)
    elif field.domain is not None:
        raise TypeError(f"Provider {path!r}: modelfill solves this field; fix it per fill instead")


class ModelFiller[T]:
    """Compile once, complete repeatedly, and validate every returned object.

    ``strict`` rejects unsupported logic at initialization. ``auto`` (default)
    performs bounded, validated fallback when unsupported logic is discovered.
    Completions are canonical: the same inputs always give the same values, which
    are as boring as the rules allow. ``work_limit`` bounds each solver check in Z3
    resource units, which count work rather than time, so it decides the same way on
    every machine; raise it if a heavy model hits ``SolverTimeout``. ``timeout_ms``
    is a wall-clock safety net; validators must terminate themselves. Fixed values
    of solved fields use canonical Python types without coercion. Defaults are
    preferences, used when compatible with the rules and fixed values.

    ``providers`` maps a type, or a field path such as ``"customer.email"``, to a
    zero-argument callable for fields the solver does not model. A path beats a type,
    and a provider beats the model's default; otherwise such a field gets a boring
    value, and a nested model its own canonical completion.
    """

    def __init__(
        self,
        model: type[T],
        *,
        mode: Literal["strict", "auto"] = "auto",
        max_attempts: int = 100,
        work_limit: int = 1_000_000,
        timeout_ms: int = 1000,
        providers: Mapping[Any, Callable[[], Any]] | None = None,
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
        self.providers = dict(providers or {})
        self._compiled = _compile(model)
        if mode == "strict" and self._compiled.unsupported:
            raise UnsupportedConstraint("; ".join(self._compiled.unsupported))
        for key, provider in self.providers.items():
            if not callable(provider):
                raise TypeError(f"Provider {key!r} must be a zero-argument callable")
            if isinstance(key, str):
                _check_path(model, key)
            elif _domain(key) is not None:
                raise TypeError(f"Provider {key!r}: modelfill solves such fields")
        self._local = threading.local()
        self._nested: dict[str, ModelFiller[Any]] = {}  # One filler per nested model field.
        self._outer: tuple[type, ...] = ()  # The models this one is nested in.

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
        inputs, chosen = self._inputs(fixed_fields)
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
                return [self._construct(inputs, cache[key])]
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
                    results.append(self._construct(inputs, candidate))
                except (ValueError, AssertionError) as exc:
                    # Retrying changes only solved fields, so it cannot fix a value modelfill
                    # chose for another field.
                    if isinstance(exc, ValidationError) and (
                        rejected := chosen
                        & {error["loc"][0] for error in exc.errors() if error["loc"]}
                    ):
                        raise GenerationExhausted(
                            f"{self.model.__name__}: Pydantic rejected the value chosen for "
                            f"{', '.join(sorted(map(str, rejected)))}; pass a value or add a "
                            f"provider: {exc}"
                        ) from exc
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

    def _construct(self, inputs: dict[str, Any], candidate: dict[str, Any]) -> T:
        """Run the original constructor; a changed or noncanonical result is a rejection."""
        instance = self._compiled.adapter.construct({**inputs, **candidate})
        domains = self._compiled.domains
        invalid = [
            name
            for name, domain in domains.items()
            if not domain.accepts(getattr(instance, name, MISSING))
        ]
        # Fixed values of solved fields keep their exact type and value. Other fields go
        # through Pydantic as usual, which may convert them (5 to 5.0, a dict to a model).
        changed = [
            name
            for name, value in inputs.items()
            if name in domains
            and (
                type(getattr(instance, name, MISSING)) is not type(value)
                or getattr(instance, name, MISSING) != value
            )
        ]
        if isinstance(instance, self.model) and not invalid and not changed:
            return instance
        raise ValueError(
            f"Validation changed fixed fields {changed} or returned noncanonical "
            f"fields {invalid} / an incompatible object"
        )

    def _provider(self, field: FieldSpec) -> Callable[[], Any] | None:
        """The field's provider: its path first, then its type, which also matches X | None."""
        if field.name in self.providers:
            return self.providers[field.name]
        options = [field.annotation]
        if get_origin(field.annotation) in (Union, types.UnionType):
            options = [option for option in get_args(field.annotation) if option is not type(None)]
        return self.providers.get(options[0]) if len(options) == 1 else None

    def _fill_nested(self, name: str, model: type, fixed: dict[str, Any]) -> Any:
        """A nested model's canonical completion, with this filler's options and providers."""
        if name not in self._nested:
            if model in (*self._outer, self.model):
                raise UnsupportedConstraint(
                    f"{name}: {model.__name__} contains itself; pass a value"
                )
            prefix = f"{name}."
            providers = {
                key.removeprefix(prefix) if isinstance(key, str) else key: provider
                for key, provider in self.providers.items()
                if not isinstance(key, str) or key.startswith(prefix)
            }
            nested = ModelFiller(
                model,
                mode=self.mode,
                max_attempts=self.max_attempts,
                work_limit=self.work_limit,
                timeout_ms=self.timeout_ms,
                providers=providers,
            )
            nested._outer = (*self._outer, self.model)
            self._nested[name] = nested
        try:
            return self._nested[name].fill(**fixed)
        except ModelFillError as error:
            raise type(error)(f"{name}: {error}") from error

    def _inputs(self, fixed_fields: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        """Constructor inputs besides the solved fields, and the input names modelfill chose.

        For each field the solver does not model: the caller's value, with a dict for a
        nested model completed; else a provider; else the model's default; else a nested
        model's canonical completion or a boring value.
        """
        inputs, chosen, missing = dict(fixed_fields), set(), []
        for field in self._compiled.adapter.fields:
            if field.domain is not None:
                continue
            nested = nested_model(field.annotation)
            if field.name in fixed_fields:
                value = fixed_fields[field.name]
                if nested is not None and isinstance(value, dict):
                    inputs[field.name] = self._fill_nested(field.name, nested, value)
                continue
            provider = self._provider(field)
            if provider is not None:
                inputs[field.name] = provider()
            elif not field.required:
                continue  # The model's own default applies.
            elif nested is not None and nested is field.annotation:
                inputs[field.name] = self._fill_nested(field.name, nested, {})
            elif (value := boring(field.annotation, field.metadata)) is not MISSING:
                inputs[field.name] = value
            else:
                missing.append(field.name)
                continue
            chosen.add(field.input_name or field.name)
        if missing:
            raise UnsupportedConstraint(
                f"modelfill cannot generate {', '.join(missing)}; pass values or add providers"
            )
        return inputs, chosen

    def _describe(self, field: FieldSpec) -> dict[str, Any]:
        domain = field.domain
        if domain is None:
            if self._provider(field) is not None:
                source = "provider"
            elif not field.required:
                source = "model default"
            elif nested_model(field.annotation) is field.annotation:
                source = "nested model"
            elif boring(field.annotation, field.metadata) is not MISSING:
                source = "boring value"
            else:
                source = "required input"
            return {"type": "external", "choices": None, "default": source}
        has_default = field.default is not MISSING or field.default_factory is not None
        return {
            "type": domain.kind + (" | None" if domain.nullable else ""),
            "choices": None
            if domain.choices is None
            else [repr(value) for value in domain.choices],
            "default": "preference" if has_default else "generated",
        }

    def explain(self) -> dict[str, Any]:
        """Return fresh, JSON-friendly compilation and generation diagnostics."""
        compiled = self._compiled
        return {
            "model": self.model.__qualname__,
            "mode": self.mode,
            "fields": {field.name: self._describe(field) for field in compiled.adapter.fields},
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
