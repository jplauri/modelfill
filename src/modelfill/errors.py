"""Public failure categories. Search failure never implies unsatisfiability."""


class ModelFillError(Exception):
    """Base class for generation failures."""


class UnsatisfiableConstraints(ModelFillError):
    """The modeled constraints and canonical fixed inputs are contradictory."""


class UnsupportedConstraint(ModelFillError):
    """A field, validator or model feature cannot be modeled safely."""


class GenerationExhausted(ModelFillError):
    """No validated candidate was found within the search budget."""


class SolverUnknown(ModelFillError):
    """The solver could not establish SAT or UNSAT."""


class SolverTimeout(SolverUnknown):
    """The solver exhausted its budget: its work limit or the wall-clock safety net."""
