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
from typing import Any, TypeGuard

from fraisier.errors import ConfigurationError


class LazyEnv:
    """Placeholder for ``!envvar NAME`` deferring ``os.environ`` lookup.

    Holds ``name`` (env var) and ``yaml_path`` (where it appeared in
    fraises.yaml, populated in Phase 4). ``resolve()`` consults
    ``os.environ`` on every call; there is no cache.
    """

    __slots__ = ("name", "yaml_path")

    def __init__(self, name: str, yaml_path: str | None = None) -> None:
        self.name = name
        self.yaml_path = yaml_path

    def resolve(self) -> str:
        """Look up the env var now. Raises if unset. No caching."""
        try:
            return os.environ[self.name]
        except KeyError:
            path = self.yaml_path or "<unknown>"
            raise ConfigurationError(
                f"!envvar references environment variable {self.name!r} "
                f"which is not set (at {path})"
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


def is_string_like(value: Any) -> TypeGuard[str | LazyEnv]:
    """Return True iff *value* is a ``str`` or ``LazyEnv``.

    The replacement for ``isinstance(value, str)`` at config-validator
    inspection sites. Searchable by name so future contributors can
    grep all the places that widen the type for env-var deferral.
    """
    return isinstance(value, str | LazyEnv)


# Inventory of remaining ``isinstance(x, str)`` sites (Phase 5 Cycle 5.9).
#
# Audit conclusion: every remaining ``isinstance(_, str)`` call in
# ``fraisier/`` is either (a) already LazyEnv-aware, (b) operates on a
# non-config value (subprocess output, IPC payload, IdP response, public
# API argument), or (c) lives in an else-branch after a LazyEnv
# isinstance check has already widened the type. None of them need
# ``is_string_like``.
#
#   File:Line                                Status      Reason
#   ───────────────────────────────────────  ──────────  ─────────────────
#   _lazy_env.py:61                          internal    LazyEnv.__eq__
#                                                        comparing to a str
#                                                        peer; not a
#                                                        consumer site.
#
#   _validation.py:158 (health_check.field)  not-eligible
#                                                        version_field /
#                                                        migration_field
#                                                        are response-shape
#                                                        identifiers, not
#                                                        !envvar-eligible.
#
#   _validation.py:308 (validate_pg_url)     after-LazyEnv
#                                                        Line 306 returns
#                                                        early on LazyEnv;
#                                                        line 308 validates
#                                                        a concrete str.
#
#   _validation.py:371 (zfs.<field>)         after-LazyEnv
#                                                        Pre-checked via
#                                                        is_string_like at
#                                                        line 366; line 371
#                                                        is the str-branch.
#
#   _validation.py:427 (preferred_compression) after-LazyEnv
#                                                        Line 425 short-
#                                                        circuits on
#                                                        LazyEnv; line 427
#                                                        is the str branch.
#
#   schema.py:294 (restricted_paths[])       not-eligible
#                                                        Element-shape
#                                                        union of str|dict
#                                                        in the nginx
#                                                        restricted_paths
#                                                        list — items are
#                                                        literal paths, not
#                                                        secrets.
#
#   install_helper.py:91,94                  non-config  Validates an IPC
#                                                        request payload
#                                                        received over a
#                                                        Unix socket; the
#                                                        bytes never come
#                                                        from fraises.yaml.
#
#   providers/docker_compose/provider.py:166 non-config  Branch on whether
#                                                        the runtime
#                                                        ``command`` arg
#                                                        is a str (shlex-
#                                                        split) or a list.
#                                                        Caller passes a
#                                                        plain str.
#
#   smoke_tests.py:261 (load_smoke_tests)    LazyEnv-aware
#                                                        Phase 3: branches
#                                                        on str vs LazyEnv
#                                                        for the URL-based
#                                                        default-name
#                                                        derivation.
#
#   token_providers.py:390 (_validate_format) not-eligible
#                                                        Phase 3 explicitly
#                                                        rejects !envvar
#                                                        for `format` —
#                                                        the str check is
#                                                        the post-rejection
#                                                        guard.
#
#   token_providers.py:488 (access_token)    non-config  IdP response body
#                                                        field; not a
#                                                        fraises.yaml
#                                                        value.
#
#   validation.py:875 (dependency_command)   not-eligible
#                                                        health_check
#                                                        dependency command
#                                                        is `str | list`
#                                                        per schema; not
#                                                        !envvar-eligible.
#
#   versioning.py:382 (dep)                  non-config  Parses
#                                                        pyproject.toml
#                                                        dependency entries.
#
#   zfs/operations.py:41,50                  non-config  Public API
#                                                        argument checks on
#                                                        dataset / snapshot
#                                                        names supplied by
#                                                        callers, not from
#                                                        fraises.yaml.
#
# If a NEW config-derived ``isinstance(x, str)`` site appears, widen it
# with ``is_string_like`` and add a row above.
