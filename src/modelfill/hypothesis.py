"""Hypothesis integration: Hypothesis draws the fields you choose, modelfill solves the rest.

Requires the optional dependency: ``pip install "modelfill[hypothesis]"``.
"""

from __future__ import annotations

import random
import types
from collections.abc import Callable
from typing import Any, Union, get_args, get_origin

from hypothesis import reject
from hypothesis import strategies as st

from .adapters import _resolve
from .boring import _LISTS, _MAPPINGS, models_in, nested_model
from .errors import GenerationExhausted, UnsatisfiableConstraints, UnsupportedConstraint
from .filler import ModelFiller


def from_model[T](
    model: type[T] | ModelFiller[T], /, **fields: st.SearchStrategy[Any]
) -> st.SearchStrategy[T]:
    """A strategy for validated instances of a model.

    Pass the model class, or a configured ``ModelFiller`` to set ``mode``,
    ``max_attempts``, ``work_limit``, ``timeout_ms`` or ``providers``; keyword
    arguments are always field names, mapped to strategies. Required fields the
    solver does not model default to ``st.from_type`` of their annotation unless a
    provider supplies them, except that a model inside one, as in ``list[Item]`` or
    ``Item | None``, is drawn by ``from_model``, so its rules hold. modelfill solves
    every other field. Unlike ``fill``, the solved fields are explored: random,
    often at the edges of their feasible ranges. Draws that make the rules
    impossible are rejected instead of failing; Hypothesis counts that as filtering.
    Each example uses a fresh solver seeded from the draw, so examples replay and the
    drawn fields shrink; the solved fields do not shrink.
    """
    template = model if isinstance(model, ModelFiller) else ModelFiller(model)
    solved_nested = dict(template._compiled.nested)  # Explored by the solver instead.
    strategies = {  # A drawn value would override the field's provider.
        field.name: _inferred(template, field.name, field.annotation)
        for field in template._compiled.adapter.fields
        if field.domain is None
        and field.required
        and field.name not in solved_nested
        and field.name not in fields  # Yours stands, so none is inferred.
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


def _inferred(
    template: ModelFiller[Any],
    name: str,
    annotation: Any,
    outer: tuple[tuple[Any, list[st.SearchStrategy[Any]]], ...] = (),
) -> st.SearchStrategy[Any]:
    """st.from_type, except that each model in the type, at any depth, is drawn by
    from_model with the template's options and the providers under the field: Hypothesis
    builds a model from independent fields, which its rules may reject. A model that
    cannot be completed, as one that contains itself, is built as before. Where a
    recursive type recurs, its strategy is drawn again, lazily; outer holds the types
    around this one, each with a cell for its strategy."""
    resolved = _resolve(annotation)[0]
    # An alias resolves to one object each time.
    recurring = next((cell for seen, cell in outer if seen is resolved), None)
    if recurring is not None:
        return st.deferred(lambda: recurring[0])
    if not models_in(resolved):
        return st.from_type(annotation)
    if nested_model(resolved) is resolved:
        try:
            return from_model(template._nested_filler(name, resolved))
        except UnsupportedConstraint:
            return st.from_type(annotation)
    origin, args = get_origin(resolved), get_args(resolved)
    cell: list[st.SearchStrategy[Any]] = []
    inner = [
        _inferred(template, name, arg, (*outer, (resolved, cell)))
        for arg in args
        if arg is not Ellipsis
    ]
    if origin in (Union, types.UnionType):
        strategy = st.one_of(inner)
    elif origin is tuple and args[-1] is not Ellipsis:
        strategy = st.tuples(*inner)
    elif origin in _LISTS or origin is tuple:
        strategy = st.lists(inner[0]).map(tuple) if origin is tuple else st.lists(inner[0])
    elif origin in _MAPPINGS and len(args) == 2:
        strategy = st.dictionaries(st.from_type(args[0]), inner[1])
    else:
        strategy = st.from_type(annotation)
    cell.append(strategy)
    return strategy
