"""Lazy ``!envvar`` placeholder for fraises.yaml (#220).

A ``LazyEnv`` records the name of an environment variable referenced in
YAML and defers the ``os.environ`` lookup until a consumer (or
validator) actually inspects the value. Each inspection performs a
fresh lookup — there is no resolution cache. See the cycle plan for
the rationale (test ergonomics, picklability, and the ability for
long-running processes to observe env mutations between commands).

``to_str(value)`` is the boundary helper: pass any ``str | LazyEnv``
and receive a concrete ``str``. The two-symbol surface — ``LazyEnv``
for the placeholder, ``to_str`` for the boundary — keeps the
isinstance audit small at consumer sites.
"""

from __future__ import annotations

import os

from fraisier.errors import ConfigurationError


class LazyEnv:
    """Placeholder for ``!envvar NAME`` deferring ``os.environ`` lookup.

    Holds ``name`` (env var) and ``yaml_path`` (where it appeared in
    fraises.yaml, populated in Phase 4). ``resolve()`` consults
    ``os.environ`` on every call; there is no cache.
    """

    __slots__ = ("name", "yaml_path")

    def __init__(self, name: str, yaml_path: str) -> None:
        self.name = name
        self.yaml_path = yaml_path

    def resolve(self) -> str:
        """Look up the env var now. Raises if unset. No caching."""
        try:
            return os.environ[self.name]
        except KeyError:
            raise ConfigurationError(
                f"!envvar references environment variable {self.name!r} "
                f"which is not set (at {self.yaml_path})"
            ) from None

    def __str__(self) -> str:
        return self.resolve()

    def __format__(self, format_spec: str) -> str:
        return format(self.resolve(), format_spec)

    def __fspath__(self) -> str:
        return self.resolve()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LazyEnv):
            return self.resolve() == other.resolve()
        if isinstance(other, str):
            return self.resolve() == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.resolve())

    def __repr__(self) -> str:
        # Never calls resolve(): repr is safe to log even when the env
        # var carries a secret, and works fine when the var is unset.
        return f"LazyEnv(name={self.name!r}, yaml_path={self.yaml_path!r})"

    def __bool__(self) -> bool:
        # A configured env-var reference is meaningfully different
        # from an absent value; truthy without resolving. This makes
        # `if value:` checks in validators safe (Phase 3).
        return True


def to_str(value: str | LazyEnv) -> str:
    """Boundary helper: resolve a ``LazyEnv`` or pass through a ``str``."""
    if isinstance(value, LazyEnv):
        return value.resolve()
    return value
