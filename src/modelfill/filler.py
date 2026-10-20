"""Compilation, bounded generation, and the original validation boundary."""

from __future__ import annotations

import decimal
import random
import threading
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache, partial
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, RootModel, ValidationError

from .adapters import MISSING, Adapter, FieldSpec, _domain, _resolve, introspect
from .backend import Solver
from .boring import boring, complete, models_in, nested_model
from .errors import (
    GenerationExhausted,
    ModelFillError,
    UnsatisfiableConstraints,
    UnsupportedConstraint,
)
from .extract import extract_validator
from .ir import Domain, Expr, encodable, prefixed
from .patterns import Pattern


@dataclass(frozen=True)
class _Compiled:
    adapter: Adapter
    # Solved fields, with a required nested model's fields as paths such as "customer.age";
    # external fields pass through.
    domains: dict[str, Domain]
    solved: tuple[tuple[str, FieldSpec], ...]  # The same fields' specs, for their defaults.
    rules: tuple[tuple[str, Expr], ...]  # Field constraints, as (label, rule).
    predicates: tuple[tuple[str, Expr], ...]  # Extracted validators, as (label, predicate).
    validators: tuple[tuple[str, str, str], ...]
    unsupported: tuple[str, ...]
    unchecked: tuple[str, ...]  # Solved fields whose defaults Pydantic does not validate.
    nested: tuple[tuple[str, type], ...]  # Required nested models, solved with this one.

    @property
    def constraints(self) -> tuple[tuple[str, Expr], ...]:
        # An unknown hook may transform values before another validator runs, so its
        # neighbors' predicates may then only guide the search, never constrain it.
        return self.rules + (() if self.unsupported else self.predicates)

    @property
    def guidance(self) -> tuple[Expr, ...]:
        return tuple(rule for _, rule in self.predicates) if self.unsupported else ()

    @property
    def patterns(self) -> dict[str, Pattern]:
        return {name: field.pattern for name, field in self.solved if field.pattern is not None}


_COMPILING = threading.local()  # The models being compiled, to stop a model nesting itself.


class _Rejected(Exception):
    """A candidate turned down, as a constructor signals it or by the checks on what it
    returned; anything else raised during construction is an error and propagates."""


def _model(field: FieldSpec) -> type | None:
    """The model a field holds, through NewTypes and type aliases, but not M | None."""
    annotation = _resolve(field.annotation)[0]
    return annotation if nested_model(annotation) is annotation else None


def _relabel(label: str, prefix: str) -> str:
    """Field constraint labels start with the field, or len(field): prefix that field."""
    return f"len({prefix}{label[4:]}" if label.startswith("len(") else prefix + label


def _context() -> tuple[int, int, int]:
    """The Decimal context's precision and exponent limits. Its arithmetic rounds past
    them, and Pydantic's multiple_of divides in them, so a compilation holds under
    them only."""
    context = decimal.getcontext()
    return context.prec, context.Emin, context.Emax


def _compile(model: type) -> _Compiled:
    return _compile_under(model, _context())


@lru_cache(maxsize=128)
def _compile_under(model: type, context: tuple[int, int, int]) -> _Compiled:
    """A model's compilation, cached by the Decimal context it is made under."""
    adapter = introspect(model)
    stack = _COMPILING.__dict__.setdefault("stack", [])
    stack.append(model)
    try:
        return _compile_fields(model, adapter, stack)
    finally:
        stack.pop()


def _compile_fields(model: type, adapter: Adapter, stack: list[type]) -> _Compiled:
    solved: list[tuple[str, FieldSpec]] = []
    rules = list(adapter.constraints)
    predicates: list[tuple[str, Expr]] = []
    diagnostics: list[tuple[str, str, str]] = []
    unsupported = list(adapter.unsupported)
    unchecked: list[str] = []
    nested: list[tuple[str, type]] = []
    for field in adapter.fields:
        if field.domain is not None:
            solved.append((field.name, field))
            if field.unchecked_default:
                unchecked.append(field.name)
            continue
        # Only a required nested model joins this solver; others are completed alone.
        inner = _model(field)
        if inner is None or not field.required or inner in stack:
            continue
        try:
            sub = _compile(inner)
        except UnsupportedConstraint:
            continue  # Filling reports why this nested model cannot be completed.
        prefix = f"{field.name}."
        nested.append((field.name, inner))
        solved += [(prefix + name, spec) for name, spec in sub.solved]
        rules += [(_relabel(label, prefix), prefixed(rule, prefix)) for label, rule in sub.rules]
        predicates += [(label, prefixed(rule, prefix)) for label, rule in sub.predicates]
        diagnostics += [(prefix + name, status, detail) for name, status, detail in sub.validators]
        unsupported += [f"{field.name}: {reason}" for reason in sub.unsupported]
        unchecked += [prefix + name for name in sub.unchecked]
    domains = {name: spec.domain for name, spec in solved if spec.domain is not None}
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
                constants=adapter.constants,
            )
        except UnsupportedConstraint as exc:
            unsupported.append(str(exc))
            diagnostics.append((name, "unsupported", str(exc)))
        else:
            predicates.append((_label(validator.function), predicate))
            diagnostics.append((name, "modeled", "pure rejection validator"))
    return _Compiled(
        adapter,
        domains,
        tuple(solved),
        tuple(rules),
        tuple(predicates),
        tuple(diagnostics),
        tuple(unsupported),
        tuple(unchecked),
        tuple(nested),
    )


def _label(function: Any) -> str:
    """A validator as named in explanations, with the line where its definition starts."""
    code = getattr(function, "__code__", None)
    line = f" (line {code.co_firstlineno})" if code is not None else ""
    return f"{getattr(function, '__qualname__', function)}{line}"


def _get(instance: Any, path: str) -> Any:
    """A field by path: "customer.age" reads instance.customer.age."""
    for name in path.split("."):
        instance = getattr(instance, name, MISSING)
    return instance


def _inside_instance(path: str, inputs: Mapping[str, Any]) -> bool:
    """Whether a path's value comes from a given nested instance, such as inputs["customer"]."""
    parts = path.split(".")
    return any(".".join(parts[:index]) in inputs for index in range(1, len(parts)))


def _contradiction(model: str, given: list[str], rules: list[str]) -> str:
    if not rules:
        return f"{model}: constraints contradict the fixed inputs"
    if not given:
        return f"{model}: no values satisfy {', '.join(rules)}"
    return f"{model}: {', '.join(given)} conflict with {', '.join(rules)}"


def _check_path(model: type, path: str, rest: str | None = None) -> None:
    """A provider path leads through nested models to a field the solver does not model.
    A step may pass through a collection's elements or a union's members: one is enough."""
    name, _, rest = (path if rest is None else rest).partition(".")
    field = next((field for field in _compile(model).adapter.fields if field.name == name), None)
    if field is None:
        raise ValueError(f"Provider {path!r}: {model.__name__} has no field {name!r}")
    if rest:
        failures: list[Exception] = []
        for nested in models_in(field.annotation):
            try:
                return _check_path(nested, path, rest)
            except (TypeError, ValueError) as error:
                failures.append(error)
        if failures:
            raise failures[0]
        raise ValueError(f"Provider {path!r}: {name!r} is not a nested model")
    elif field.domain is not None:
        raise TypeError(f"Provider {path!r}: modelfill solves this field; fix it per fill instead")


def _leads(model: type, path: str) -> bool:
    """Whether a provider path leads anywhere in a model."""
    try:
        _check_path(model, path)
    except (TypeError, ValueError):
        return False
    return True


class ModelFiller[T]:
    """Compile once, complete repeatedly, and validate every returned object.

    ``strict`` rejects unsupported logic at initialization. A fill under another
    Decimal context, which decides where Decimal arithmetic rounds, compiles the
    model anew, and ``strict`` checks it there too. ``auto`` (default) performs
    bounded, validated fallback when unsupported logic is discovered.
    Completions are canonical: the same inputs always give the same values, which
    are as boring as the rules allow. ``work_limit`` bounds each solver check in Z3
    resource units, which count work rather than time, so it decides the same way on
    every machine; raise it if a heavy model hits ``SolverTimeout``. ``timeout_ms``
    is a wall-clock safety net; validators must terminate themselves. Fixed values
    of solved fields use canonical Python types without coercion. Defaults are
    preferences, used when compatible with the rules and fixed values.

    ``providers`` maps a type, or a field path such as ``"customer.email"``, to a
    zero-argument callable for fields the solver does not model, run once per
    returned object. A path beats a type, and a provider beats the model's default;
    otherwise such a field gets a boring value, and a nested model its own canonical
    completion.
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
        self._context = _context()
        self._compiled = _compile_under(model, self._context)
        # This filler's twins under other Decimal contexts, which compile anew.
        self._twins: dict[tuple[int, int, int], ModelFiller[T]] = {}
        if mode == "strict" and self._compiled.unsupported:
            raise UnsupportedConstraint("; ".join(self._compiled.unsupported))
        for key, provider in self.providers.items():
            if not callable(provider):
                raise TypeError(f"Provider {key!r} must be a zero-argument callable")
            if isinstance(key, str):
                _check_path(model, key)
            elif _domain(_resolve(key)[0]) is not None:
                raise TypeError(f"Provider {key!r}: modelfill solves such fields")
        self._local = threading.local()
        # One filler per nested model field and model: a union's branches differ.
        self._nested: dict[tuple[str, type], ModelFiller[Any]] = {}
        self._outer: tuple[type, ...] = ()  # The models this one is nested in.

    @property
    def _options(self) -> dict[str, Any]:
        """What a nested model's filler, or a fresh one, takes from this one."""
        return {
            "mode": self.mode,
            "max_attempts": self.max_attempts,
            "work_limit": self.work_limit,
            "timeout_ms": self.timeout_ms,
        }

    def _twin(self) -> ModelFiller[T] | None:
        """This filler under the current Decimal context, unless that is its own."""
        context = _context()
        if context == self._context:
            return None
        if context not in self._twins:
            twin = ModelFiller(self.model, **self._options, providers=self.providers)
            twin._outer = self._outer
            self._twins[context] = twin
        return self._twins[context]

    def _solver(self) -> Solver:
        if not hasattr(self._local, "solver"):
            self._local.solver = Solver(
                self._compiled.domains,
                [rule for _, rule in self._compiled.constraints],
                timeout_ms=self.timeout_ms,
                work_limit=self.work_limit,
                guidance=self._compiled.guidance,
                patterns=self._compiled.patterns,
            )
            self._local.cache = {}  # Canonical assignments by fixed values and defaults.
        return self._local.solver

    def fill(self, /, **fixed_fields: Any) -> T:
        """The canonical completion of canonical field-name inputs, keeping every fixed value."""
        return self._complete(fixed_fields, 1)[0]

    def batch(self, count: int, /, **fixed_fields: Any) -> list[T]:
        """count distinct completions in canonical order, compared on generated values,
        solved and provided alike."""
        if type(count) is not int or count < 0:
            raise ValueError("count must be a nonnegative integer")
        return self._complete(fixed_fields, count) if count else []

    def _complete(
        self, fixed_fields: dict[str, Any], count: int, rng: random.Random | None = None
    ) -> list[T]:
        """count validated completions: canonical ones, or explored with rng (Hypothesis)."""
        if (twin := self._twin()) is not None:
            return twin._complete(fixed_fields, count, rng)
        domains = self._compiled.domains
        self._compiled.adapter.check_fixed(fixed_fields)
        # The solver sees generated fields; external values go straight to the constructor.
        inputs, chosen, fresh = self._inputs(fixed_fields)
        solved = {name: value for name, value in inputs.items() if name in domains}
        # A fixed value that breaks its pattern can never validate, unless unsupported
        # logic, such as a before validator, changes it first. A given nested instance
        # is used as it is, so its values are not checked again.
        for name, pattern in self._compiled.patterns.items():
            value = solved.get(name)
            if (
                isinstance(value, str)
                and not self._compiled.unsupported
                and not _inside_instance(name, inputs)
                and value not in pattern
            ):
                given, rules = [f"{name}={value!r}"], [f"{name} pattern '{pattern.source}'"]
                raise UnsatisfiableConstraints(_contradiction(self.model.__name__, given, rules))
        defaults = {}
        for name, field in self._compiled.solved:
            if name not in solved:
                value = field.preferred()
                # A default the solver cannot represent or that breaks the field's
                # pattern is simply not preferred.
                if (
                    domains[name].accepts(value)
                    and encodable(value)
                    and (field.pattern is None or value is None or value in field.pattern)
                ):
                    defaults[name] = value
        solver = self._solver()
        # The first candidate depends only on these, and whenever it validates a fresh
        # search returns it, so repeated fills reuse it. A later candidate may owe its
        # turn to external values, which the key leaves out, so it is never kept.
        # ponytail: a model whose first candidate fails searches on every fill; caching
        # the candidate sequence would avoid that if fallback models get slow.
        key = (frozenset(solved.items()), tuple(defaults.items()))
        cache = self._local.cache
        if rng is None and count == 1 and key in cache:
            try:
                return [self._construct(inputs, cache[key])]
            except _Rejected:
                pass  # These external values reject it; the search goes past it.
        last_error: BaseException | None = None
        omitted = [name for name in self._compiled.unchecked if name not in solved]
        results: list[T] = []
        # The results' generated values, solved and fresh, compared by ==: a provider's
        # may not hash. ponytail: a linear scan, quadratic in a long batch.
        seen: list[list[Any]] = []
        failures = 0
        stuck = ""  # Why no further candidate could be chosen, if that ended the search.
        guidance = bool(self._compiled.guidance)  # Dropped once it leaves no candidate.
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
            tried: list[dict[str, Any]] = []  # Every candidate so far, in order; all blocked.
            again: list[dict[str, Any]] = []  # Tried ones to try under new external values.
            members: list[tuple[dict[str, Any], dict[str, Any]]] = []  # Candidates and inputs.
            while len(results) < count and failures < self.max_attempts:
                cost = 0 if again else 1  # A retry costs no attempt: there are only so many.
                reused = False
                if again:
                    candidate = again.pop(0)
                elif (results or failures) and not solver.check():
                    # No candidate is left, but fresh values, a provider's say, may still
                    # tell a member apart: earlier members' candidates in turn, with them.
                    if not members or inputs == members[-1][1]:
                        break  # Every remaining candidate has been tried.
                    candidate, reused = members[len(results) % len(members)][0], True
                else:
                    # Alternate, so guidance misled by an unknown hook cannot starve the
                    # search, and go without it when it leaves no candidate at all.
                    for guided in (True, False) if guidance and failures % 2 == 0 else (False,):
                        try:
                            if rng is None:
                                candidate = solver.canonical(solved, defaults, guided=guided)
                            else:
                                candidate = solver.candidate(solved, defaults, rng, guided=guided)
                            break
                        except GenerationExhausted as exc:
                            stuck = str(exc)  # No readable or matching string remains, say.
                            if guided:  # Excluding more candidates never brings one back.
                                guidance = False
                    else:
                        break
                    stuck = ""
                    solver.block(candidate)  # Retries and batch members must differ.
                    tried.append(candidate)
                try:
                    instance = self._construct(inputs, candidate)
                except _Rejected as exc:
                    # Even an error at a value chosen for another field may depend on the
                    # solved fields, through info.data, so the search goes on.
                    failures, last_error = failures + cost, exc.__cause__ or exc
                    if reused:  # The next one tries new fresh values.
                        inputs = inputs | {path: make() for path, make in fresh.items()}
                    continue
                # Unsupported logic can turn different candidates into equal objects.
                generated = [_get(instance, path) for path in (*candidate, *fresh)]
                if generated in seen:
                    failures += cost
                    if reused:
                        inputs = inputs | {path: make() for path, make in fresh.items()}
                    continue
                seen.append(generated)
                members.append((candidate, inputs))
                results.append(instance)
                if rng is None and count == 1 and not failures:
                    if len(cache) >= 256:  # Bounded: forget the oldest assignment.
                        del cache[next(iter(cache))]
                    cache[key] = candidate
                failures = 0
                if len(results) < count:  # Each member gets its own provider values.
                    previous = inputs
                    inputs = inputs | {path: make() for path, make in fresh.items()}
                    # Unsupported logic may judge a candidate by these values, or turn it
                    # into another object, so earlier verdicts lapse: the candidates tried
                    # so far get another try, in order, before any new one.
                    # ponytail: each member retries them all, quadratic in a long batch.
                    if self._compiled.unsupported and inputs != previous:
                        again = list(tried)
        if len(results) == count:
            return results
        found = f"found {len(results)} of {count} distinct completions; " if count > 1 else ""
        if stuck:
            reason = stuck
        elif failures >= self.max_attempts:
            reason = f"no validated completion within {self.max_attempts} attempts"
        else:
            reason = "no candidates remain"
        message = f"{self.model.__name__}: {found}{reason}"
        if self._compiled.unsupported:
            message += "; unsupported logic: " + "; ".join(self._compiled.unsupported)
        # Only this model's own errors name its fields; a nested one's do not.
        if isinstance(last_error, ValidationError) and last_error.title == self.model.__name__:
            locations = {error["loc"][0] for error in last_error.errors() if error["loc"]}
            if rejected := chosen & locations:
                message += (
                    f"; Pydantic rejected the value chosen for "
                    f"{', '.join(sorted(map(str, rejected)))}; pass a value or add a provider"
                )
        if last_error is not None:
            message += f"; last rejection: {last_error}"
        raise GenerationExhausted(message) from last_error

    def _construct(self, inputs: dict[str, Any], candidate: dict[str, Any]) -> T:
        """Run the original constructors; a changed or noncanonical result is a rejection."""
        instance = self._assemble({**inputs, **candidate})
        domains = self._compiled.domains
        invalid = [
            name for name, domain in domains.items() if not domain.accepts(_get(instance, name))
        ]
        # Fixed values of solved fields keep their exact type and value. Other fields go
        # through Pydantic as usual, which may convert them (5 to 5.0, a dict to a model).
        changed = [
            name
            for name, value in inputs.items()
            if name in domains
            and (type(_get(instance, name)) is not type(value) or _get(instance, name) != value)
        ]
        if isinstance(instance, self.model) and not invalid and not changed:
            return instance
        raise _Rejected(
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

    def _assemble(self, values: dict[str, Any]) -> Any:
        """Build this model from its fields' values, a solved nested model's by path."""
        adapter = self._compiled.adapter
        try:
            return adapter.construct(self._own(values))
        except adapter.rejections as exc:
            raise _Rejected from exc

    def _own(self, values: dict[str, Any]) -> dict[str, Any]:
        """This model's field values, with each solved nested model's made from its paths.
        A Pydantic model gets a nested Pydantic model as its input, which it validates in
        place, as a given dict, so a validator there sees which field it is in; a
        dataclass, which validates nothing, gets it built."""
        local = {name: value for name, value in values.items() if "." not in name}
        for name, model in self._compiled.nested:
            if name not in local:  # A given nested instance is used as it is.
                prefix = f"{name}."
                inner = {
                    key.removeprefix(prefix): value
                    for key, value in values.items()
                    if key.startswith(prefix)
                }
                nested = self._nested_filler(name, model)
                if all(issubclass(kind, BaseModel) for kind in (self.model, model)) and not (
                    issubclass(model, RootModel)  # Its input would be the root value.
                ):
                    local[name] = nested._compiled.adapter.inputs(nested._own(inner))
                else:
                    local[name] = nested._assemble(inner)
        return local

    def _nested_inputs(
        self, name: str, model: type, given: Any
    ) -> tuple[dict[str, Any], dict[str, Callable[[], Any]]]:
        """A solved nested model's inputs by path, such as "customer.age", and how to make
        those that each batch member gets afresh, as _inputs says.

        A dict fixes some of its fields, as in any fill. A given instance is used as it
        is, and its solved values become fixed, so this model's rules see them. It must
        keep its own model's rules too: Pydantic does not check them again, but this
        model's solver holds its values to them.
        """
        filler = self._nested_filler(name, model)
        prefix = f"{name}."
        if given is MISSING or isinstance(given, dict):
            fixed = {} if given is MISSING else given
            filler._compiled.adapter.check_fixed(fixed)
            inner, _, fresh = filler._inputs(fixed)
            return (
                {prefix + key: value for key, value in inner.items()},
                {prefix + key: make for key, make in fresh.items()},
            )
        values = {path: _get(given, path) for path in filler._compiled.domains}
        for path, value in values.items():
            if not filler._compiled.domains[path].accepts(value):
                raise TypeError(f"{name}.{path} of the given {model.__name__} is {value!r}")
        # One that breaks them, by keeping an unchecked default say, is an input error,
        # not a contradiction.
        solver = filler._solver()
        with solver.session(values):
            if not solver.check():
                conflict = solver.conflict(values, filler._compiled.constraints)
                raise ValueError(
                    f"{name}: the given instance breaks its own rules, which Pydantic does "
                    f"not check again but modelfill solves with "
                    f"({_contradiction(model.__name__, *conflict)}); "
                    "pass a dict or a valid instance"
                )
        return {name: given} | {prefix + path: value for path, value in values.items()}, {}

    def _nested_filler(self, name: str, model: type) -> ModelFiller[Any]:
        """The filler of a nested model field, with this filler's options and providers."""
        if (name, model) not in self._nested:
            if model in (*self._outer, self.model):
                # Callers name the field.
                raise UnsupportedConstraint(f"{model.__name__} contains itself; pass a value")
            prefix = f"{name}."
            providers = {
                key.removeprefix(prefix) if isinstance(key, str) else key: provider
                for key, provider in self.providers.items()
                # A path for another member of a union does not apply here.
                if not isinstance(key, str)
                or (key.startswith(prefix) and _leads(model, key.removeprefix(prefix)))
            }
            nested = ModelFiller(model, **self._options, providers=providers)
            nested._outer = (*self._outer, self.model)
            self._nested[name, model] = nested
        return self._nested[name, model]

    def _fill_nested(self, name: str, model: type, fixed: dict[str, Any]) -> Any:
        """The canonical completion of a nested model completed on its own."""
        try:
            return self._nested_filler(name, model).fill(**fixed)
        except ModelFillError as error:
            raise self._nested_error(name, error) from error

    def _nested_error(self, name: str, error: ModelFillError) -> ModelFillError:
        """A nested model's error, under its field's name. Unsupported logic in this model
        may change or bypass the nested value, so no proof of impossibility carries over."""
        kind = type(error)
        if kind is UnsatisfiableConstraints and self._compiled.unsupported:
            kind = GenerationExhausted
        return kind(f"{name}: {error}")

    def _completed(self, name: str, annotation: Any, given: Any) -> Any:
        """A given value with each dict in it that stands for a model completed. When one
        cannot be, unsupported logic in this model, which may change or bypass it, gets
        the value as given, for the constructor to decide."""
        try:
            return complete(annotation, given, partial(self._fill_nested, name))
        except (ModelFillError, TypeError, ValueError):
            if not self._compiled.unsupported:
                raise
            return given

    def _inputs(
        self, fixed_fields: dict[str, Any]
    ) -> tuple[dict[str, Any], set[str], dict[str, Callable[[], Any]]]:
        """Constructor inputs besides the solver's candidate, the input names chosen, and
        how to make the inputs that each batch member gets afresh.

        Fixed solved values come first, a solved nested model's by path. For each field
        the solver does not model: the caller's value, with a dict for a nested model
        completed; else a provider; else the model's default; else a nested model's
        canonical completion or a boring value. All but the caller's values are made
        afresh, except what a provider gives a solved nested model: like the caller's
        values, that fixes the solver's inputs for the whole call.
        """
        inputs: dict[str, Any] = {}
        fresh: dict[str, Callable[[], Any]] = {}
        chosen, missing, solved_nested = set(), [], dict(self._compiled.nested)
        for field in self._compiled.adapter.fields:
            name, given = field.name, fixed_fields.get(field.name, MISSING)
            if field.domain is not None:
                if given is not MISSING:
                    inputs[name] = given
                continue
            provider = self._provider(field) if given is MISSING else None
            if name in solved_nested:  # Its fields join this solver.
                try:
                    given = provider() if provider is not None else given
                    known, made = self._nested_inputs(name, solved_nested[name], given)
                except ModelFillError as error:
                    raise self._nested_error(name, error) from error
                inputs.update(known)
                fresh.update(made)
                continue
            nested = _model(field)
            if given is not MISSING:
                # A dict that stands for a model, at any depth, is completed.
                if isinstance(given, (dict, list, tuple)) and models_in(field.annotation):
                    fresh[name] = partial(self._completed, name, field.annotation, given)
                    inputs[name] = fresh[name]()
                else:
                    inputs[name] = given
                continue
            if provider is not None:
                make = provider
            elif not field.required:
                # A dataclass runs its factory unvalidated, so it may as well run here,
                # where what it raises cannot pass for __post_init__ turning one down.
                if field.default_factory is None or issubclass(self.model, BaseModel):
                    continue  # The model's own default applies.
                make = field.default_factory
            elif nested is not None:
                make = partial(self._fill_nested, name, nested, {})
            elif boring(field.annotation, field.metadata) is not MISSING:
                make = partial(boring, field.annotation, field.metadata)
            elif models_in(field.annotation):  # Collections and unions of models.
                fill = partial(self._fill_nested, name)
                make = partial(boring, field.annotation, field.metadata, fill)
            else:
                missing.append(name)
                continue
            value = make()
            if value is MISSING:  # Models in a shape with no boring value, such as a set.
                missing.append(name)
                continue
            inputs[name], fresh[name] = value, make
            chosen.add(field.keyword)
        if missing:
            raise UnsupportedConstraint(
                f"modelfill cannot generate {', '.join(missing)}; pass values or add providers"
            )
        return inputs, chosen, fresh

    def _describe(self, field: FieldSpec) -> dict[str, Any]:
        domain = field.domain
        if domain is None:
            if self._provider(field) is not None:
                source = "provider"
            elif not field.required:
                source = "model default"
            elif _model(field) is not None:
                source = "nested model"
            elif boring(field.annotation, field.metadata) is not MISSING:
                source = "boring value"
            elif models_in(field.annotation):
                source = "nested models"
            else:
                source = "required input"
            return {"type": "external", "choices": None, "default": source}
        has_default = field.default is not MISSING or field.default_factory is not None
        description = {
            "type": domain.kind + (" | None" if domain.nullable else ""),
            "choices": None
            if domain.choices is None
            else [repr(value) for value in domain.choices],
            "default": "preference" if has_default else "generated",
        }
        if field.pattern is not None:
            description["pattern"] = field.pattern.source
        return description

    def explain(self) -> dict[str, Any]:
        """Return fresh, JSON-friendly compilation and generation diagnostics."""
        if (twin := self._twin()) is not None:
            return twin.explain()
        compiled = self._compiled
        return {
            "model": self.model.__qualname__,
            "mode": self.mode,
            "fields": {  # Solved nested fields appear by path, such as "customer.age".
                **{field.name: self._describe(field) for field in compiled.adapter.fields},
                **{name: self._describe(field) for name, field in compiled.solved if "." in name},
            },
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
