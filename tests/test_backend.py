from typing import Literal

import pytest
import z3
from pydantic import BaseModel, model_validator

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
    ("reason", "expected"), [("timeout", SolverTimeout), ("incomplete theory", SolverUnknown)]
)
def test_unknown_is_never_unsat(monkeypatch, reason, expected):
    solver = Solver({"x": INT}, (), timeout_ms=1000, seed=1)
    monkeypatch.setattr(solver.engine, "check", lambda: z3.unknown)
    monkeypatch.setattr(solver.engine, "reason_unknown", lambda: reason)
    with pytest.raises(expected, match=reason):
        solver.check()


def test_session_is_restored_after_exception():
    solver = Solver({"x": INT}, (), timeout_ms=1000, seed=1)
    with pytest.raises(ValueError), solver.session({"x": 3}):
        raise ValueError("interrupt")
    with solver.session({"x": 4}):
        assert solver.check()
        assert solver.values() == {"x": 4}


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


def test_random_preferences_never_add_hard_bounds():
    x = var("x", INT)
    solver = Solver({"x": INT}, [node("ge", x, const(10**100))], timeout_ms=1000, seed=2)
    with solver.session({}):
        assert solver.check()
        assert solver.candidate({}, {})["x"] >= 10**100


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

    filler = ModelFiller(Transform, seed=1)
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


@pytest.mark.parametrize("argument", [{"max_attempts": 0}, {"timeout_ms": 0}, {"mode": "random"}])
def test_bad_options(argument):
    with pytest.raises(ValueError):
        ModelFiller(Relationship, **argument)
