import random
from typing import Literal

import pytest
import z3
from pydantic import BaseModel, Field, create_model, model_validator

from modelfill import (
    GenerationExhausted,
    ModelFiller,
    SolverTimeout,
    SolverUnknown,
    UnsatisfiableConstraints,
    UnsupportedConstraint,
    fill,
)
from modelfill.backend import Solver
from modelfill.ir import INT, const, node, var


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("timeout", SolverTimeout),
        ("canceled", SolverTimeout),
        ("max. resource limit exceeded", SolverTimeout),
        ("incomplete theory", SolverUnknown),
    ],
)
def test_unknown_is_never_unsat(monkeypatch, reason, expected):
    solver = Solver({"x": INT}, (), timeout_ms=1000, work_limit=10**6)
    monkeypatch.setattr(solver.engine, "check", lambda: z3.unknown)
    monkeypatch.setattr(solver.engine, "reason_unknown", lambda: reason)
    with pytest.raises(expected, match=reason):
        solver.check()


def test_session_is_restored_after_exception():
    solver = Solver({"x": INT}, (), timeout_ms=1000, work_limit=10**6)
    with pytest.raises(ValueError), solver.session({"x": 3}):
        raise ValueError("interrupt")
    with solver.session({"x": 4}):
        assert solver.check()
        assert solver.values() == {"x": 4}


def test_field_names_never_collide_with_solver_symbols():
    # "a is None" once named the flag of the optional field a, making this impossible.
    clash = create_model("Clash", a=(int | None, ...), **{"a is None": (bool, ...)})
    values = {"a": None, "a is None": False}
    assert fill(clash, **values) == clash(**values)

    class Parts(BaseModel):  # part0 once named an explanation tracker.
        part0: bool
        x: int = Field(ge=10)

    with pytest.raises(UnsatisfiableConstraints, match="x=5 conflict with x >= 10"):
        fill(Parts, part0=False, x=5)


def test_outside_z3_unicode_is_explicitly_unsupported():
    class Text(BaseModel):
        s: str

    with pytest.raises(UnsupportedConstraint, match="U\\+2FFFF"):
        fill(Text, s=chr(0x30000))


def test_unrepresentable_defaults_and_choices_do_not_break_filling():
    class Text(BaseModel):
        note: str = chr(0x30000)  # Not preferred: the solver cannot represent it.
        symbol: Literal["a", chr(0x30000)]  # External, so the caller supplies it.

    assert ModelFiller(Text).explain()["fields"]["symbol"]["type"] == "external"
    assert fill(Text, symbol="a").note != chr(0x30000)


class Relationship(BaseModel):
    x: int
    y: int

    @model_validator(mode="after")
    def check(self):
        if self.y != self.x + 5:
            raise ValueError("relation")
        return self


def test_filler_reuses_solver_within_thread():
    filler = ModelFiller(Relationship)
    solver = filler._solver()
    for x in (0, -1, 10**60):
        assert filler.fill(x=x).y == x + 5
        assert filler._solver() is solver
    with pytest.raises(UnsatisfiableConstraints):
        filler.fill(x=0, y=6)
    assert filler.fill(y=9).x == 4


def test_preferences_never_add_hard_bounds():
    x = var("x", INT)
    solver = Solver({"x": INT}, [node("ge", x, const(10**100))], timeout_ms=1000, work_limit=10**6)
    with solver.session({}):
        assert solver.check()
        assert solver.canonical({}, {}) == {"x": 10**100}  # The value nearest 0.
        assert solver.candidate({}, {}, random.Random(2))["x"] >= 10**100


def test_exploration_ranges_stay_within_the_work_limit():
    # The optimizer behind exploration's ranges had only the wall-clock timeout, so a
    # slow machine could find other ranges, and a replayed seed draw another value.
    x = var("x", INT)
    rule = node("and", node("ge", x, const(3)), node("le", x, const(7)))
    ample = Solver({"x": INT}, [rule], timeout_ms=10**4, work_limit=10**6)
    assert ample.feasible_ranges(["x"]) == {"x": (3, 7)}
    scant = Solver({"x": INT}, [rule], timeout_ms=10**4, work_limit=1)
    assert scant.feasible_ranges(["x"]) == {}  # The same on any machine.


def test_exhaustion_after_candidate_blocking_is_not_proof():
    class Reject(BaseModel):
        flag: bool

        @model_validator(mode="after")
        def check(self):
            if str(self.flag):
                raise ValueError("always rejects")
            return self

    filler = ModelFiller(Reject, max_attempts=10)
    with pytest.raises(GenerationExhausted):
        filler.fill()
    assert not filler.explain()["can_prove_unsat"]


def test_unknown_hook_demotes_other_validator_predicates():
    class Transform(BaseModel):
        x: int

        @model_validator(mode="before")
        @classmethod
        def change(cls, data):
            return {"x": data["x"] + 1}

        @model_validator(mode="after")
        def reject(self):
            if self.x != 0:
                raise ValueError("zero")
            return self

    filler = ModelFiller(Transform)
    assert not filler.explain()["validator_constraints_active"]
    # A fixed input cannot be silently normalized by unknown transformation.
    with pytest.raises(GenerationExhausted):
        filler.fill(x=-1)


def test_transformed_types_are_not_returned_as_canonical_values():
    class Transform(BaseModel):
        x: int

        @model_validator(mode="after")
        def change(self):
            self.x = "invalid"
            return self

    with pytest.raises(GenerationExhausted):
        ModelFiller(Transform, max_attempts=2).fill()


def test_keyword_names_do_not_collide_with_api_options():
    class Keywords(BaseModel):
        model: int
        mode: int
        seed: int
        count: int
        self: int

    values = {"model": 1, "mode": 2, "seed": 3, "count": 4, "self": 5}
    assert fill(Keywords, **values).model_dump() == values
    assert ModelFiller(Keywords).batch(1, **values)[0].model_dump() == values


@pytest.mark.parametrize(
    "argument", [{"max_attempts": 0}, {"work_limit": 0}, {"timeout_ms": 0}, {"mode": "random"}]
)
def test_bad_options(argument):
    with pytest.raises(ValueError):
        ModelFiller(Relationship, **argument)
