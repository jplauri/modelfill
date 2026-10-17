"""Hypothesis integration: Hypothesis draws the fields you choose, modelfill solves the rest.

Requires the optional dependency: ``pip install "modelfill[hypothesis]"``.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

from hypothesis import reject
from hypothesis import strategies as st

from .errors import GenerationExhausted, UnsatisfiableConstraints
from .filler import ModelFiller


def from_model[T](
    model: type[T] | ModelFiller[T], /, **fields: st.SearchStrategy[Any]
) -> st.SearchStrategy[T]:
    """A strategy for validated instances of a model.

    Pass the model class, or a configured ``ModelFiller`` to set ``mode``,
    ``max_attempts``, ``work_limit``, ``timeout_ms`` or ``providers``; keyword
    arguments are always field names, mapped to strategies. Required fields the
    solver does not model default to ``st.from_type`` of their annotation unless a
    provider supplies them, and modelfill solves every other field.
    Unlike ``fill``, the solved fields are explored: random, often at the edges of
    their feasible ranges. Draws that make the rules impossible are rejected instead
    of failing; Hypothesis counts that as filtering. Each example uses a fresh solver
    seeded from the draw, so examples replay and the drawn fields shrink; the solved
    fields do not shrink.
    """
    template = model if isinstance(model, ModelFiller) else ModelFiller(model)
    solved_nested = dict(template._compiled.nested)  # Explored by the solver instead.
    strategies = {  # A drawn value would override the field's provider.
        field.name: st.from_type(field.annotation)
        for field in template._compiled.adapter.fields
        if field.domain is None
        and field.required
        and field.name not in solved_nested
        and template._provider(field) is None
    }
    strategies.update(fields)

    @st.composite
    def instances(draw: Callable[[st.SearchStrategy[Any]], Any]) -> T:
        fixed = {name: draw(strategy) for name, strategy in strategies.items()}
        filler = ModelFiller(template.model, **template._options, providers=template.providers)
        rng = random.Random(draw(st.integers(0, 2**32 - 1)))
        try:
            return filler._complete(fixed, 1, rng)[0]
        except (UnsatisfiableConstraints, GenerationExhausted):
            reject()

    return instances()
