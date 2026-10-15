"""The only module that depends on Z3. Each instance owns a private context."""

from __future__ import annotations

import operator
import random
import re
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from enum import Enum
from typing import Any

import z3

from .errors import SolverTimeout, SolverUnknown, UnsupportedConstraint
from .ir import BOUNDS, DAY, Domain, Expr, decode, encodable, encode

# Kinds Z3 sees as integers, with random-target parameters (anchor, step, spread).
# Fields with a finite feasible range sample inside it and often at its edges;
# others near the reference model, or anywhere within anchor ± spread steps.
_INTEGERS = {
    "int": (0, 1, 100),
    "date": (encode(date(2025, 1, 1)), 1, 365),
    "datetime": (encode(datetime(2025, 1, 1)), 60_000_000, 525_600),
    "timedelta": (0, 60_000_000, 10_080),
}


def _show(value: Any) -> str:
    return f"{type(value).__name__}.{value.name}" if isinstance(value, Enum) else repr(value)


class Solver:
    """Incremental solving over immutable IR; never shared between threads."""

    def __init__(
        self,
        domains: Mapping[str, Domain],
        constraints: Sequence[Expr],
        *,
        timeout_ms: int,
        seed: int | None,
        guidance: Sequence[Expr] = (),
    ) -> None:
        self.domains = dict(domains)
        self.timeout_ms = timeout_ms
        self.context = z3.Context()
        self.engine = z3.Solver(ctx=self.context)
        self.engine.set(timeout=timeout_ms)
        self.rng = random.Random(seed)
        # Keep Z3's own search deterministic; vary preferences in Python instead.
        self.engine.set(random_seed=0)
        self.variables = {
            name: {"bool": z3.Bool, "str": z3.String}.get(domain.kind, z3.Int)(
                name, ctx=self.context
            )
            for name, domain in domains.items()
        }
        self.flags = {
            name: z3.Bool(f"{name} is None", ctx=self.context)
            for name, domain in domains.items()
            if domain.nullable
        }
        # What each field's type allows, labeled so that contradictions can cite it.
        self.facts: list[tuple[str, Any]] = []
        for name, domain in domains.items():
            if domain.choices is not None:
                options = [self.equal(name, value) for value in domain.choices]
                shown = [_show(value) for value in domain.choices]
                if domain.nullable:
                    options.append(self.flags[name])
                    shown.append("None")
                self.facts.append((f"{name} in ({', '.join(shown)})", self.disjunction(options)))
            if domain.kind in BOUNDS:  # Python cannot represent dates beyond these.
                low, high = BOUNDS[domain.kind]
                variable = self.variables[name]
                fact = z3.And(variable >= low, variable <= high)
                self.facts.append((f"{name} is a valid {domain.kind}", fact))
        self.engine.add(*(fact for _, fact in self.facts))
        self.engine.add(*(self.translate(expr) for expr in constraints))
        self.guidance = [self.translate(expr) for expr in guidance]
        self.ranges_key: frozenset[tuple[str, Any]] | None = None
        self.ranges: dict[str, tuple[int | None, int | None]] = {}

    def disjunction(self, args: Sequence[Any]) -> Any:
        return z3.Or(*args) if args else z3.BoolVal(False, self.context)

    def literal(self, value: Any, domain: Domain) -> Any:
        if domain.kind == "enum":
            return z3.IntVal(list(type(value)).index(value), self.context)
        if domain.kind in _INTEGERS:
            return z3.IntVal(encode(value), self.context)
        if domain.kind == "bool":
            return z3.BoolVal(value, self.context)
        if domain.kind == "str":
            if not encodable(value):
                raise UnsupportedConstraint("Z3 supports string code points only through U+2FFFF")
            # StringVal parses SMT unicode escapes; escape literal backslashes first.
            return z3.StringVal(value.replace("\\", "\\u{5c}"), self.context)
        raise UnsupportedConstraint(f"Cannot encode domain {domain.kind!r}")

    def equal(self, name: str, value: Any) -> Any:
        if value is None:
            return self.flags[name]
        matches = self.variables[name] == self.literal(value, self.domains[name])
        return z3.And(z3.Not(self.flags[name]), matches) if name in self.flags else matches

    def translate(self, expr: Expr) -> Any:
        if expr.op == "var":
            return self.variables[expr.value]  # A nullable field's value, when not None.
        if expr.op == "const":
            return self.literal(expr.value, expr.sort)
        if expr.op == "isnone":
            return self.flags[expr.args[0].value]  # The extractor applies it to fields only.
        op = expr.op
        args = [self.translate(arg) for arg in expr.args]
        if op in {
            "eq",
            "ne",
            "lt",
            "le",
            "gt",
            "ge",
            "add",
            "sub",
            "mul",
            "mod",
            "floordiv",
            "neg",
        }:
            kinds = [arg.sort.kind for arg in expr.args]
            # Python bool is numeric in comparisons/arithmetic, but Z3 Bool is not.
            if set(kinds) <= {"int", "bool"}:
                args = [z3.If(arg, 1, 0) if z3.is_bool(arg) else arg for arg in args]
            if "date" in kinds and "timedelta" in kinds:
                # date ± timedelta moves by whole days: timedelta.days rounds down.
                args = [
                    a / DAY if k == "timedelta" else a for a, k in zip(args, kinds, strict=True)
                ]
            elif op == "sub" and kinds == ["date", "date"]:
                return (args[0] - args[1]) * DAY  # A timedelta, in microseconds.
            elif op == "floordiv":
                return args[0] / args[1]  # Z3 integer division floors for positive divisors.
            return getattr(operator, op)(*args)  # IR op names match the operator module.
        if op == "and":
            return z3.And(*args) if args else z3.BoolVal(True, self.context)
        if op == "or":
            return self.disjunction(args)
        if op == "not":
            return z3.Not(args[0])
        if op == "ite":
            return z3.If(*args)
        if op == "len":
            return z3.Length(args[0])
        raise UnsupportedConstraint(f"Unknown IR operation: {op}")

    @contextmanager
    def session(self, fixed: Mapping[str, Any]) -> Generator[None]:
        self.engine.push()
        try:
            self.engine.add(*(self.equal(name, value) for name, value in fixed.items()))
            yield
        finally:
            self.engine.pop()

    def check(self) -> bool:
        result = self.engine.check()
        if result == z3.sat:
            return True
        if result == z3.unsat:
            return False
        reason = self.engine.reason_unknown()
        if "timeout" in reason.lower() or "canceled" in reason.lower():
            raise SolverTimeout(f"Z3 could not finish: {reason}")
        raise SolverUnknown(f"Z3 could not decide: {reason}")

    def conflict(
        self, fixed: Mapping[str, Any], rules: Sequence[tuple[str, Expr]]
    ) -> tuple[list[str], list[str]]:
        """A small set of fixed inputs and rules that admit no solution (an unsat core)."""
        explainer = z3.Solver(ctx=self.context)
        explainer.set(timeout=self.timeout_ms)
        explainer.set("core.minimize", True)
        inputs = [
            (f"{name}={_show(value)}", self.equal(name, value)) for name, value in fixed.items()
        ]
        parts = inputs + self.facts + [(label, self.translate(expr)) for label, expr in rules]
        for index, (_, constraint) in enumerate(parts):
            explainer.assert_and_track(constraint, z3.Bool(f"part{index}", self.context))
        if explainer.check() != z3.unsat:
            return [], []
        core = {str(literal) for literal in explainer.unsat_core()}
        cited = [index for index in range(len(parts)) if f"part{index}" in core]
        given = [parts[index][0] for index in cited if index < len(inputs)]
        return given, [parts[index][0] for index in cited if index >= len(inputs)]

    def feasible_ranges(
        self, names: Sequence[str], *assumptions: Any
    ) -> dict[str, tuple[int | None, int | None]]:
        """Each field's smallest and largest encoding allowed; None where unbounded."""
        if not names:
            return {}
        optimizer = z3.Optimize(ctx=self.context)
        optimizer.set(priority="box", timeout=self.timeout_ms)
        optimizer.add(*self.engine.assertions(), *assumptions)
        objectives = {
            name: (
                optimizer.minimize(self.variables[name]),
                optimizer.maximize(self.variables[name]),
            )
            for name in names
        }
        if optimizer.check() != z3.sat:
            return {}
        ranges = {}
        for name, extremes in objectives.items():
            # Python's own date limits are representation, not constraints.
            limits = BOUNDS.get(self.domains[name].kind, (None, None))
            values = [objective.value() for objective in extremes]
            low, high = (
                value.as_long() if z3.is_int_value(value) and value.as_long() != limit else None
                for value, limit in zip(values, limits, strict=True)
            )
            ranges[name] = (low, high)
        return ranges

    def integer_target(self, name: str, reference: Any) -> Any:
        """A random target for an integer-encoded field: an edge, a value in range, or nearby."""
        kind = self.domains[name].kind
        anchor, step, spread = _INTEGERS[kind]
        low, high = self.ranges.get(name, (None, None))
        roll = self.rng.random()
        if low is not None and roll < 0.1:
            target = low
        elif high is not None and roll < 0.2:
            target = high
        elif low is not None and high is not None:
            target = self.rng.randint(low, high)
        elif low is not None:
            target = low + step * self.rng.randint(0, spread)
        elif high is not None:
            target = high - step * self.rng.randint(0, spread)
        else:
            near = anchor if reference is None else encode(reference)
            target = self.rng.choice(
                [
                    near + step * self.rng.randint(-10, 10),
                    anchor + step * self.rng.randint(-spread, spread),
                ]
            )
        if kind in BOUNDS:  # Stay within what Python can represent.
            target = min(max(target, BOUNDS[kind][0]), BOUNDS[kind][1])
        return decode(kind, target)

    def values(self) -> dict[str, Any]:
        model = self.engine.model()
        result = {}
        for name, variable in self.variables.items():
            value = model.eval(variable, model_completion=True)
            domain = self.domains[name]
            if name in self.flags and z3.is_true(
                model.eval(self.flags[name], model_completion=True)
            ):
                result[name] = None
            elif domain.kind == "bool":
                result[name] = z3.is_true(value)
            elif domain.kind == "enum":
                assert domain.choices is not None
                result[name] = tuple(type(domain.choices[0]))[value.as_long()]
            elif domain.kind in _INTEGERS:
                result[name] = decode(domain.kind, value.as_long())
            else:
                # One pass preserves literal text such as backslash-u-brace-41.
                result[name] = re.sub(
                    r"\\u\{([0-9a-fA-F]+)\}",
                    lambda match: chr(int(match[1], 16)),
                    value.as_string(),
                )
        return result

    def prefer(self, constraint: Any) -> None:
        """Add a preference only after checking it; never a source of UNSAT."""
        self.engine.push()
        try:
            self.engine.add(constraint)
            accepted = self.check()
        except SolverUnknown:
            accepted = False
        finally:
            self.engine.pop()
        if accepted:
            self.engine.add(constraint)

    def candidate(
        self, fixed: Mapping[str, Any], defaults: Mapping[str, Any], *, guided: bool = False
    ) -> dict[str, Any]:
        """Sample soft preferences, then read an exact satisfying assignment."""
        key = frozenset(fixed.items())
        if key != self.ranges_key:  # Ranges depend only on hard constraints and fixed inputs.
            free = [
                name
                for name, domain in self.domains.items()
                if name not in fixed and domain.kind in _INTEGERS and domain.choices is None
            ]
            self.ranges = self.feasible_ranges([name for name in free if name not in self.flags])
            for name in free:
                if name in self.flags:  # An optional field's range covers its non-None values.
                    self.ranges |= self.feasible_ranges([name], z3.Not(self.flags[name]))
            self.ranges_key = key
        self.engine.push()
        try:
            for predicate in self.guidance if guided else ():
                self.prefer(predicate)
            for name, value in defaults.items():
                # A default the solver cannot represent is simply not preferred.
                if name not in fixed and self.domains[name].accepts(value) and encodable(value):
                    self.prefer(self.equal(name, value))
            names = [name for name in self.domains if name not in fixed and name not in defaults]
            self.rng.shuffle(names)
            # Obtain a feasible reference before choosing soft random targets.
            if not self.check():
                raise RuntimeError("A checked preference unexpectedly became unsatisfiable")
            base = self.values()
            for name in names:
                domain = self.domains[name]
                reference = base[name]
                if domain.nullable and self.rng.random() < 0.25:
                    target = None
                elif domain.choices is not None:
                    target = self.rng.choice(domain.choices)
                elif domain.kind == "bool":
                    target = bool(self.rng.getrandbits(1))
                elif domain.kind in _INTEGERS:
                    target = self.integer_target(name, reference)
                else:
                    length = min(len(reference or ""), 64) + self.rng.randint(0, 8)
                    target = "".join(
                        self.rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(length)
                    )
                self.prefer(self.equal(name, target))
            if not self.check():
                raise RuntimeError("A checked preference unexpectedly became unsatisfiable")
            return self.values()
        finally:
            self.engine.pop()

    def block(self, values: Mapping[str, Any]) -> None:
        """Exclude a failed candidate within the current fill session only."""
        self.engine.add(self.disjunction([z3.Not(self.equal(k, v)) for k, v in values.items()]))
