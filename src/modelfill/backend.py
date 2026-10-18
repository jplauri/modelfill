"""The only module that depends on Z3. Each instance owns a private context."""

from __future__ import annotations

import operator
import random
import re
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from enum import Enum
from functools import cache
from itertools import islice
from typing import TYPE_CHECKING, Any

import z3

from .errors import GenerationExhausted, SolverTimeout, SolverUnknown, UnsupportedConstraint
from .ir import BOUNDS, DAY, Domain, Expr, decode, encodable, encode

if TYPE_CHECKING:
    from .patterns import Pattern

# Kinds Z3 sees as integers: (anchor, step, spread). Canonical values sit as close to
# the anchor as the rules allow. The exploration sampler targets a finite feasible
# range's inside and edges; otherwise steps from the reference model or the anchor.
_INTEGERS = {
    "int": (0, 1, 100),
    "date": (encode(date(2025, 1, 1)), 1, 365),
    "datetime": (encode(datetime(2025, 1, 1)), 60_000_000, 525_600),
    "time": (0, 60_000_000, 1_440),  # Midnight, by the minute over a day.
    "decimal": (0, 1, 100),  # Units of the field's scale.
    "timedelta": (0, 60_000_000, 10_080),
}


def _aligned(terms: Sequence[Any], exprs: Sequence[Expr]) -> list[Any]:
    """Numbers' terms in units of the finest decimal grid among them; an int's is 1."""
    scale = max(expr.sort.scale for expr in exprs)
    return [
        term * 10 ** (scale - expr.sort.scale) if expr.sort.scale < scale else term
        for term, expr in zip(terms, exprs, strict=True)
    ]


def _show(value: Any) -> str:
    return f"{type(value).__name__}.{value.name}" if isinstance(value, Enum) else repr(value)


def _strings(exprs: Iterable[Expr]) -> Generator[str]:
    """String constants in rule order: readable candidates for string fields."""
    for expr in exprs:
        if expr.op == "const" and type(expr.value) is str:
            yield expr.value
        yield from _strings(expr.args)


class Solver:
    """Incremental solving over immutable IR; never shared between threads."""

    def __init__(
        self,
        domains: Mapping[str, Domain],
        constraints: Sequence[Expr],
        *,
        timeout_ms: int,
        work_limit: int,
        guidance: Sequence[Expr] = (),
        patterns: Mapping[str, Pattern] | None = None,
    ) -> None:
        self.domains = dict(domains)
        # String fields' patterns stay out of Z3: candidates are checked against them.
        self.patterns = dict(patterns or {})
        self.timeout_ms = timeout_ms
        self.work_limit = work_limit
        self.context = z3.Context()
        self.engine = z3.Solver(ctx=self.context)
        # Z3's resource limit (rlimit) counts work, not time, so it decides the same way
        # on any machine; the timeout is only a safety net. Z3's own search stays
        # deterministic too; exploration varies its preferences in Python instead.
        self.engine.set(rlimit=work_limit, timeout=timeout_ms, random_seed=0)
        # Solver names are internal (v0, n0, ...), so no field name, such as "part0" or
        # "a is None", can collide with another field's symbol or with explanation trackers.
        self.variables = {
            name: {"bool": z3.Bool, "str": z3.String}.get(domain.kind, z3.Int)(
                f"v{index}", ctx=self.context
            )
            for index, (name, domain) in enumerate(domains.items())
        }
        self.flags = {
            name: z3.Bool(f"n{index}", ctx=self.context)
            for index, (name, domain) in enumerate(domains.items())
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
        self.strings = list(dict.fromkeys(_strings([*constraints, *guidance])))
        self.ranges_key: frozenset[tuple[str, Any]] | None = None
        self.ranges: dict[str, tuple[int | None, int | None]] = {}

    def disjunction(self, args: Sequence[Any]) -> Any:
        return z3.Or(*args) if args else z3.BoolVal(False, self.context)

    def literal(self, value: Any, domain: Domain) -> Any:
        if domain.kind == "enum":
            return z3.IntVal(list(type(value)).index(value), self.context)
        if domain.kind in _INTEGERS:
            return z3.IntVal(encode(value, domain.scale), self.context)
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
            if set(kinds) <= {"int", "bool", "decimal"}:
                args = [z3.If(arg, 1, 0) if z3.is_bool(arg) else arg for arg in args]
            # Decimals line up on the finer grid; a product's units just multiply.
            if "decimal" in kinds and op != "mul":
                args = _aligned(args, expr.args)
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
            if expr.sort.kind == "decimal":  # Both branches on the result's grid.
                args = [args[0], *_aligned(args[1:], expr.args[1:])]
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
        # Z3 reports an exhausted work limit as "canceled" or "max. resource limit
        # exceeded"; some older releases only say "unknown" (a plain SolverUnknown).
        if any(word in reason.lower() for word in ("timeout", "canceled", "resource limit")):
            raise SolverTimeout(f"Z3 exhausted its budget: {reason}")
        raise SolverUnknown(f"Z3 could not decide: {reason}")

    def conflict(
        self, fixed: Mapping[str, Any], rules: Sequence[tuple[str, Expr]]
    ) -> tuple[list[str], list[str]]:
        """A small set of fixed inputs and rules that admit no solution (an unsat core)."""
        explainer = z3.Solver(ctx=self.context)
        explainer.set(rlimit=self.work_limit, timeout=self.timeout_ms)
        explainer.set("core.minimize", True)
        inputs = [
            (f"{name}={_show(value)}", self.equal(name, value)) for name, value in fixed.items()
        ]
        parts = inputs + self.facts + [(label, self.translate(expr)) for label, expr in rules]
        for index, (_, constraint) in enumerate(parts):
            explainer.assert_and_track(constraint, z3.Bool(f"t{index}", self.context))
        if explainer.check() != z3.unsat:
            return [], []
        core = {str(literal) for literal in explainer.unsat_core()}
        cited = [index for index in range(len(parts)) if f"t{index}" in core]
        given = [parts[index][0] for index in cited if index < len(inputs)]
        return given, [parts[index][0] for index in cited if index >= len(inputs)]

    # Canonical completion: every choice is the unique best value under a fixed rule,
    # so it never depends on which model Z3 happens to return.

    def fits(self, constraint: Any) -> bool:
        """Whether a constraint is satisfiable together with everything asserted."""
        self.engine.push()
        try:
            self.engine.add(constraint)
            return self.check()
        finally:
            self.engine.pop()

    def witness(self, constraint: Any, term: Any) -> int | None:
        """An integer term's value in some model of the constraint, or None if there is none."""
        self.engine.push()
        try:
            self.engine.add(constraint)
            if not self.check():
                return None
            return self.engine.model().eval(term, model_completion=True).as_long()
        finally:
            self.engine.pop()

    def least(self, term: Any, lower: int, upper: int | None = None) -> int | None:
        """An integer term's smallest feasible value above lower (and below upper)."""
        window = term > lower if upper is None else z3.And(term > lower, term < upper)
        best = self.witness(window, term)
        if best is None:
            return None
        # Bisect between the values known infeasible and the best found. The first
        # model often holds the minimum already, so probe just below it first.
        low, probe = lower + 1, best - 1
        while low <= probe:
            found = self.witness(z3.And(window, term <= probe), term)
            if found is None:
                low = probe + 1
            else:
                best = found
            probe = (low + best - 1) // 2
        return best

    def nearest(self, term: Any, anchor: int) -> int:
        """An integer term's feasible value closest to anchor; a tie goes above it."""
        if self.fits(term == anchor):
            return anchor
        above = self.least(term, anchor)
        # A value below wins only if strictly closer: -term in (-anchor, above - 2 * anchor).
        below = self.least(-term, -anchor, None if above is None else above - 2 * anchor)
        if below is not None:
            return -below
        assert above is not None  # A satisfiable session has a value on some side.
        return above

    def texts(self, name: str, known: Sequence[str], spelled: Iterable[str] = ()) -> Generator[str]:
        """Readable string candidates in canonical order, probing lengths only as needed.

        Values spelled from a pattern follow the name's: the first one comes before
        strings that the rules or other fields mention, the rest after them.
        """
        length = z3.Length(self.variables[name])
        name = name.rpartition(".")[2]  # A nested field reads as its own name.
        yield name
        if self.fits(length == len(name) + 2):
            yield from (f"{name}_{number}" for number in range(1, 10))
        size = self.nearest(length, len(name))
        base = (name + "_" * size)[:size]
        yield base
        yield from (base[:-1] + digit for digit in "123456789" if size)
        spelled = iter(spelled)
        yield from islice(spelled, 1)
        yield from self.strings  # Strings the rules mention, then other fields' values.
        yield from known
        yield from spelled

    def text(self, name: str, known: Sequence[str]) -> str:
        """The first readable candidate the rules allow; never Z3's own string choice.

        With a pattern, a candidate must match it too, and values spelled from the
        pattern join the candidates.
        """
        pattern = self.patterns.get(name)
        spelled = () if pattern is None else islice(self.spelled(name, pattern), 256)
        tried = set()
        for value in self.texts(name, known, spelled):
            if (
                value not in tried
                and encodable(value)
                and (pattern is None or value in pattern)
                and self.fits(self.equal(name, value))
            ):
                return value
            tried.add(value)
        if pattern is None:
            raise GenerationExhausted(f"no readable string remains for {name!r}")
        raise GenerationExhausted(
            f"found no string for {name!r} that matches '{pattern.source}'; pass a value"
        )

    def spelled(self, name: str, pattern: Pattern) -> Generator[str]:
        """Values spelled from a pattern, at lengths the rules allow."""
        length = z3.Length(self.variables[name])
        lowest = self.least(length, -1)
        if lowest is not None:
            allowed = cache(lambda size: self.fits(length == size))
            yield from pattern.spell(name.rpartition(".")[2], allowed, lowest)

    def resolve(self, name: str, known: Sequence[str]) -> Any:
        """A free field's canonical value, given everything pinned so far."""
        domain = self.domains[name]
        if domain.nullable and self.fits(self.flags[name]):
            return None
        if domain.choices is not None:
            return next(value for value in domain.choices if self.fits(self.equal(name, value)))
        if domain.kind == "bool":
            return not self.fits(self.equal(name, False))
        if domain.kind in _INTEGERS:
            anchor = _INTEGERS[domain.kind][0]
            return decode(domain.kind, self.nearest(self.variables[name], anchor), domain.scale)
        return self.text(name, known)

    def canonical(
        self, fixed: Mapping[str, Any], defaults: Mapping[str, Any], *, guided: bool = False
    ) -> dict[str, Any]:
        """The free fields' canonical values: guidance and defaults where they fit, then
        each remaining field in declaration order, pinned before the next is chosen."""
        self.engine.push()
        try:
            for predicate in self.guidance if guided else ():
                if self.fits(predicate):
                    self.engine.add(predicate)
            values: dict[str, Any] = {}
            for name, value in defaults.items():
                if self.fits(self.equal(name, value)):
                    values[name] = value
                    self.engine.add(self.equal(name, value))
            for name in self.domains:
                if name not in fixed and name not in values:
                    pinned = {**fixed, **values}
                    known = [pinned[n] for n in self.domains if type(pinned.get(n)) is str]
                    values[name] = self.resolve(name, known)
                    self.engine.add(self.equal(name, values[name]))
            return values
        finally:
            self.engine.pop()

    # Exploration: the edge-biased random sampler, reachable only through modelfill.hypothesis.

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

    def integer_target(self, name: str, reference: Any, rng: random.Random) -> Any:
        """A random target for an integer-encoded field: an edge, a value in range, or nearby."""
        domain = self.domains[name]
        kind = domain.kind
        anchor, step, spread = _INTEGERS[kind]
        low, high = self.ranges.get(name, (None, None))
        roll = rng.random()
        if low is not None and roll < 0.1:
            target = low
        elif high is not None and roll < 0.2:
            target = high
        elif low is not None and high is not None:
            target = rng.randint(low, high)
        elif low is not None:
            target = low + step * rng.randint(0, spread)
        elif high is not None:
            target = high - step * rng.randint(0, spread)
        else:
            near = anchor if reference is None else encode(reference, domain.scale)
            target = rng.choice(
                [
                    near + step * rng.randint(-10, 10),
                    anchor + step * rng.randint(-spread, spread),
                ]
            )
        if kind in BOUNDS:  # Stay within what Python can represent.
            target = min(max(target, BOUNDS[kind][0]), BOUNDS[kind][1])
        return decode(kind, target, domain.scale)

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
                result[name] = decode(domain.kind, value.as_long(), domain.scale)
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
        try:
            if self.fits(constraint):
                self.engine.add(constraint)
        except SolverUnknown:
            pass  # Exploration skips an undecided preference.

    def candidate(
        self,
        fixed: Mapping[str, Any],
        defaults: Mapping[str, Any],
        rng: random.Random,
        *,
        guided: bool = False,
    ) -> dict[str, Any]:
        """Sample soft random preferences, then read an exact satisfying assignment."""
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
                self.prefer(self.equal(name, value))
            names = [name for name in self.domains if name not in fixed and name not in defaults]
            rng.shuffle(names)
            # Obtain a feasible reference before choosing soft random targets.
            if not self.check():
                raise RuntimeError("A checked preference unexpectedly became unsatisfiable")
            base = self.values()
            for name in names:
                domain = self.domains[name]
                reference = base[name]
                if domain.nullable and rng.random() < 0.25:
                    target = None
                elif domain.choices is not None:
                    target = rng.choice(domain.choices)
                elif domain.kind == "bool":
                    target = bool(rng.getrandbits(1))
                elif domain.kind in _INTEGERS:
                    target = self.integer_target(name, reference, rng)
                else:
                    pattern = self.patterns.get(name)
                    target = None if pattern is None else pattern.sample(rng)
                    if target is None:
                        length = min(len(reference or ""), 64) + rng.randint(0, 8)
                        target = "".join(
                            rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(length)
                        )
                self.prefer(self.equal(name, target))
            if not self.check():
                raise RuntimeError("A checked preference unexpectedly became unsatisfiable")
            return self.values()
        finally:
            self.engine.pop()

    def block(self, values: Mapping[str, Any]) -> None:
        """Exclude a tried candidate (rejected, or already in a batch) for this session."""
        self.engine.add(self.disjunction([z3.Not(self.equal(k, v)) for k, v in values.items()]))
