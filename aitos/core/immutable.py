"""JSON-compatible immutable containers for frozen domain contracts.

These types retain dict/list compatibility for standard JSON encoders and
existing consumers. Nested mappings and sequences are frozen recursively.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FrozenDict(dict):
    """A recursively frozen dict that remains JSON-encoder compatible."""

    def __init__(self, value: Mapping | None = None, **kwargs: Any) -> None:
        source = dict(value or {})
        source.update(kwargs)
        dict.__init__(self, ((key, freeze_value(item)) for key, item in source.items()))

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        raise TypeError("FrozenDict is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        memo[id(self)] = self
        return self

    def __reduce__(self):
        return (type(self), (dict(self),))


class FrozenList(list):
    """A recursively frozen list that preserves list equality and JSON shape."""

    def __init__(self, values=()) -> None:
        list.__init__(self, (freeze_value(item) for item in values))

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        raise TypeError("FrozenList is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable

    def __copy__(self) -> FrozenList:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenList:
        memo[id(self)] = self
        return self

    def __reduce__(self):
        return (type(self), (list(self),))


def freeze_value(value: Any) -> Any:
    """Recursively freeze containers while retaining JSON-compatible shapes."""
    if isinstance(value, (FrozenDict, FrozenList)):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, list):
        return FrozenList(value)
    if isinstance(value, tuple):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value
