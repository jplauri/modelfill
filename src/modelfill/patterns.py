"""String patterns: Pydantic decides what matches, and a small automaton proposes values.

The solver never sees a pattern, so it allows more values than Pydantic does, never
fewer, and a proof of impossibility stays sound. Pydantic checks every candidate
against the pattern instead. Python's regex parser reads the pattern only to
propose values: a pattern it cannot read is still checked, just not spelled.
"""

from __future__ import annotations

import random
import re
import string
import threading
import unicodedata
import warnings
from collections.abc import Callable, Iterable, Iterator
from itertools import islice
from re import _constants as sre  # ty: ignore[unresolved-import]
from re import _parser  # ty: ignore[unresolved-import]
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError

_TOP = 0x2FFFF  # Z3 strings end here, so spelled values do too.
_STATES = 4096  # A larger automaton, as for x{1,9999}, is not spelled.
# Nor is one that takes more sequences than this to read: a repeat of something that
# reads nothing, as of the empty group in (){1000000000}, adds no states to count.
_SEQUENCES = 16 * _STATES
_PREFERRED = string.ascii_lowercase + string.ascii_uppercase + string.digits
# Unicode White_Space, which Rust's \s means. Python's \s differs slightly; Pydantic decides.
_WHITE_SPACE = frozenset(
    {9, 10, 11, 12, 13, 32, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029}
    | {0x202F, 0x205F, 0x3000}
)


def _digit(code: int) -> bool:
    return unicodedata.category(chr(code)) == "Nd"


def _space(code: int) -> bool:
    return code in _WHITE_SPACE


def _word(code: int) -> bool:
    char = chr(code)
    return char.isalnum() or unicodedata.category(char) in ("Pc", "Mn", "Mc", "Me")


_CATEGORIES = {
    sre.CATEGORY_DIGIT: (_digit, False),
    sre.CATEGORY_NOT_DIGIT: (_digit, True),
    sre.CATEGORY_SPACE: (_space, False),
    sre.CATEGORY_NOT_SPACE: (_space, True),
    sre.CATEGORY_WORD: (_word, False),
    sre.CATEGORY_NOT_WORD: (_word, True),
}
_BEGIN = (sre.AT_BEGINNING, sre.AT_BEGINNING_STRING)
_END = (sre.AT_END, sre.AT_END_STRING)  # Rust's $ means the very end, unlike Python's.


class _Unreadable(Exception):
    """A pattern construct the automaton does not follow, such as a lookahead."""


class _Chars:
    """The characters one step accepts: ranges and category tests, possibly negated.

    Under (?i) a set also matches the other case, which only adds characters, except
    that a negated set then leaves out the case variants of what it names too.
    """

    def __init__(
        self,
        ranges: Iterable[tuple[int, int]] = (),
        tests: Iterable[tuple[Callable[[int], bool], bool]] = (),
        negate: bool = False,
        fold: bool = False,
    ) -> None:
        self.ranges, self.tests, self.negate, self.fold = tuple(ranges), tuple(tests), negate, fold
        if negate or self.tests:
            self.least = next((code for code in range(_TOP + 1) if code in self), None)
        else:
            self.least = min((low for low, _ in self.ranges if low <= _TOP), default=None)

    def named(self, code: int) -> bool:
        return any(low <= code <= high for low, high in self.ranges) or any(
            test(code) != inverted for test, inverted in self.tests
        )

    def __contains__(self, code: int) -> bool:
        if not self.negate:
            return self.named(code)
        variants = [code]
        if self.fold:
            char = chr(code)
            variants += [ord(other) for other in (char.lower(), char.upper()) if len(other) == 1]
        return not any(map(self.named, variants))


class _Automaton:
    """A nondeterministic automaton for Pydantic's search: a value contains a match.

    An edge reads one character of a set, or nothing: anywhere (None), only at the
    start of the value ("^") or only at its end ("$").
    """

    def __init__(self, source: str, flags: int) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # Python warns about sets that Rust would nest.
            items = _parser.parse(source, flags)
        self.edges: list[list[tuple[_Chars | str | None, int]]] = []
        self.closures: dict[tuple[int, str], frozenset[int]] = {}
        self.lock = threading.Lock()
        self.sequences = 0  # Read so far, against _SEQUENCES.
        self.start, self.accept = self.state(), self.state()
        anything = _Chars(negate=True)
        state = self.loop(self.start, lambda state: self.step(anything, state))
        state = self.sequence(items, state, bool(items.state.flags & re.IGNORECASE))
        state = self.loop(state, lambda state: self.step(anything, state))
        self.edges[state].append((None, self.accept))
        done = (
            state for state in range(len(self.edges)) if self.accept in self.closure(state, "$")
        )
        self.reach = [frozenset(done)]  # By size: the states that many characters from a match.
        # The states that a path leads from to a match after some first character, where
        # ^ no longer holds; the path may have any length.
        sources: list[list[int]] = [[] for _ in self.edges]
        for state, edges in enumerate(self.edges):
            for label, target in edges:
                if label != "^" and (not isinstance(label, _Chars) or label.least is not None):
                    sources[target].append(state)
        live, stack = {self.accept}, [self.accept]
        while stack:
            for state in sources[stack.pop()]:
                if state not in live:
                    live.add(state)
                    stack.append(state)
        self.live = frozenset(live)

    def state(self) -> int:
        if len(self.edges) >= _STATES:
            raise _Unreadable("the automaton would be too large")
        self.edges.append([])
        return len(self.edges) - 1

    def step(self, label: _Chars | str, state: int) -> int:
        end = self.state()
        self.edges[state].append((label, end))
        return end

    def loop(self, state: int, body: Callable[[int], int]) -> int:
        """Zero or more repetitions of body."""
        loop = self.state()
        self.edges[state].append((None, loop))
        self.edges[body(loop)].append((None, loop))
        return loop

    def sequence(self, items: Iterable[tuple[Any, Any]], state: int, fold: bool) -> int:
        """Read items in order; fold says whether (?i) is in force."""
        self.sequences += 1
        if self.sequences > _SEQUENCES:
            raise _Unreadable("the pattern would take too long to read")
        for op, av in items:
            state = self.item(op, av, state, fold)
        return state

    def item(self, op: Any, av: Any, state: int, fold: bool) -> int:
        if op is sre.LITERAL:
            return self.step(_Chars([(av, av)]), state)
        if op is sre.NOT_LITERAL:
            return self.step(_Chars([(av, av)], negate=True, fold=fold), state)
        if op is sre.ANY:  # Without the s flag, . is anything but a newline.
            return self.step(_Chars([(10, 10)], negate=True), state)
        if op is sre.IN:
            ranges, tests, negate = [], [], False
            for kind, value in av:
                if kind is sre.NEGATE:
                    negate = True
                elif kind is sre.LITERAL:
                    ranges.append((value, value))
                elif kind is sre.RANGE:
                    ranges.append(value)
                elif kind is sre.CATEGORY and value in _CATEGORIES:
                    tests.append(_CATEGORIES[value])
                else:
                    raise _Unreadable(kind)
            return self.step(_Chars(ranges, tests, negate, fold), state)
        if op is sre.BRANCH:
            end = self.state()
            for branch in av[1]:
                begin = self.state()
                self.edges[state].append((None, begin))
                self.edges[self.sequence(branch, begin, fold)].append((None, end))
            return end
        if op is sre.SUBPATTERN:  # A group, perhaps turning (?i) on or off inside.
            _, add, remove, items = av
            fold = (fold or bool(add & re.IGNORECASE)) and not remove & re.IGNORECASE
            return self.sequence(items, state, fold)
        if op is sre.AT and av in _BEGIN:
            return self.step("^", state)
        if op is sre.AT and av in _END:
            return self.step("$", state)
        if op in (sre.MAX_REPEAT, sre.MIN_REPEAT):
            low, high, items = av
            if not items:  # Nothing, however often, is nothing: (?:){1000000000} is "".
                return state
            for _ in range(low):
                state = self.sequence(items, state, fold)
            if high is sre.MAXREPEAT:
                return self.loop(state, lambda state: self.sequence(items, state, fold))
            end = self.state()
            self.edges[state].append((None, end))
            for _ in range(high - low):
                state = self.sequence(items, state, fold)
                self.edges[state].append((None, end))
            return end
        raise _Unreadable(op)

    def closure(self, state: int, anchors: str = "") -> frozenset[int]:
        """The states reachable without reading a character, passing the given anchors."""
        key = (state, anchors)
        if key not in self.closures:
            seen, stack = {state}, [state]
            while stack:
                for label, target in self.edges[stack.pop()]:
                    free = label is None or (type(label) is str and label in anchors)
                    if free and target not in seen:
                        seen.add(target)
                        stack.append(target)
            self.closures[key] = frozenset(seen)
        return self.closures[key]

    def closed(self, states: Iterable[int]) -> frozenset[int]:
        return frozenset().union(*map(self.closure, states))

    def first(self) -> frozenset[int]:
        """The states before the first character, where ^ holds."""
        return self.closure(self.start, "^")

    def reads(self, states: Iterable[int]) -> Iterator[tuple[_Chars, int]]:
        """The edges from these states that read a character, from a nonempty set."""
        for state in states:
            for label, target in self.edges[state]:
                if isinstance(label, _Chars) and label.least is not None:
                    yield label, target

    def reachable(self, size: int) -> frozenset[int]:
        """The states from which exactly size more characters complete a match."""
        with self.lock:
            while len(self.reach) <= size:
                later = self.reach[-1]
                self.reach.append(
                    frozenset(
                        state
                        for state in range(len(self.edges))
                        if any(target in later for _, target in self.reads(self.closure(state)))
                    )
                )
            return self.reach[size]

    def accepts(self, size: int) -> bool:
        if size == 0:  # The start is also the end, so both anchors hold.
            return self.accept in self.closure(self.start, "^$")
        later = self.reachable(size - 1)
        return any(target in later for _, target in self.reads(self.first()))

    def targets(self, states: Iterable[int], code: int) -> frozenset[int]:
        return frozenset(target for chars, target in self.reads(states) if code in chars)

    def options(self, states: Iterable[int]) -> list[str]:
        """Characters to try, readable ones first; each set's least one stands for the rest."""
        least = {chars.least for chars, _ in self.reads(states)}
        return [*_PREFERRED, *map(chr, sorted(code for code in least if code is not None))]

    def spell(self, name: str, size: int) -> Iterator[str]:
        """Values of one size in canonical order: the name's letters while the value still
        spells the name, then a-z, A-Z, 0-9, then each set's least character."""

        def steps(states: frozenset[int], prefix: str) -> Iterator[tuple[frozenset[int], str]]:
            index = len(prefix)
            later = self.reachable(size - index - 1)
            own = name[index : index + 1] if prefix.lower() == name[:index].lower() else ""
            for char in dict.fromkeys([*own, *own.swapcase(), *self.options(states)]):
                following = self.targets(states, ord(char))
                if following & later:
                    yield self.closed(following), prefix + char

        # Depth first with an explicit stack: a value may be longer than Python's
        # recursion limit.
        stack = [iter([(self.first(), "")])]
        while stack:
            step = next(stack[-1], None)
            if step is None:
                stack.pop()
            elif len(step[1]) == size:
                yield step[1]
            else:
                stack.append(steps(*step))

    def sample(self, rng: random.Random, size: int) -> str:
        """A random value of one size; reachability rules out dead ends."""
        states, value = self.first(), ""
        for index in range(size):
            later = self.reachable(size - index - 1)
            options = self.options(states)
            rng.shuffle(options)
            char = next(char for char in options if self.targets(states, ord(char)) & later)
            states, value = self.closed(self.targets(states, ord(char))), value + char
        return value


class Pattern:
    """A string field's pattern: Pydantic decides what matches; the automaton proposes."""

    def __init__(
        self, regex: str | re.Pattern[str], engine: Literal["rust-regex", "python-re"] | None
    ) -> None:
        self.source = regex if isinstance(regex, str) else regex.pattern
        # Pydantic checks the pattern alone, so a value matches exactly when Pydantic says
        # so, for either regex engine. The field's other metadata, such as a validator
        # that reads info.data, runs only in the real constructor, with the model's data.
        config = ConfigDict(regex_engine=engine) if engine else None
        self.validator = TypeAdapter(Annotated[str, Field(pattern=regex)], config=config)
        try:
            flags = 0 if isinstance(regex, str) else regex.flags
            self.automaton: _Automaton | None = _Automaton(self.source, flags)
        except (_Unreadable, re.error):
            self.automaton = None  # Rust's \p{L}, say: readable candidates may still match.

    def __contains__(self, value: object) -> bool:
        try:
            return self.validator.validate_python(value) == value
        except ValidationError:
            return False

    def spell(self, name: str, allowed: Callable[[int], bool], lowest: int) -> Iterator[str]:
        """Matching values in canonical order, at sizes from lowest that allowed accepts.

        When a value can start with the name's first letter, sizes go nearest the name's
        length first, a tie going above it; otherwise shortest first.
        """
        automaton = self.automaton
        if automaton is None:
            return
        width = len(name)
        # The shortest match is no longer than the automaton has states.
        sizes: Iterable[int] = range(lowest, max(width, lowest) + len(automaton.edges) + 64)
        first = automaton.first()
        starts = frozenset().union(
            *(automaton.targets(first, ord(char)) for char in {name[0], *name[0].swapcase()})
        )
        if starts & automaton.live:
            sizes = sorted(sizes, key=lambda size: (abs(size - width), size < width))
        for size in sizes:
            if automaton.accepts(size):
                values = automaton.spell(name, size)
                # The first value goes first: checking it is quick, while asking Z3 whether
                # a long free string can have this length is slow.
                yield from islice(values, 1)
                if allowed(size):
                    yield from values

    def sample(self, rng: random.Random) -> str | None:
        """A random matching value for exploration, or None if the pattern is unread."""
        automaton = self.automaton
        if automaton is None:
            return None
        accepted = (size for size in range(len(automaton.edges) + 64) if automaton.accepts(size))
        sizes = list(islice(accepted, 9))  # The nine shortest lengths.
        return automaton.sample(rng, rng.choice(sizes)) if sizes else None
