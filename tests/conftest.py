"""Hypothesis profiles: repeatable by default; ``--hypothesis-profile=deep`` explores."""

from hypothesis import settings

settings.register_profile("ci", max_examples=100, derandomize=True, deadline=None)
settings.register_profile("deep", max_examples=2000, deadline=None)
settings.load_profile("ci")
