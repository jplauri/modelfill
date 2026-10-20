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


class InputError(Exception):
    """A value given to modelfill that it turns down, raised as a ValueError or a
    TypeError: an unknown field, a value of another type, or a nested instance that
    breaks its own rules. What the caller's code raises, a provider or a default
    factory, is never one, so a fallback that takes the value as given cannot hide it."""


class InputValueError(InputError, ValueError):
    pass


class InputTypeError(InputError, TypeError):
    pass
