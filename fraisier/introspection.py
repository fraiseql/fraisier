"""Subcommand → config-section map + ``!envvar`` walker (#221 bundle B).

This module answers the question *"if I ran ``fraisier <subcommand>``,
which YAML paths would it touch, and which ``!envvar`` references inside
those paths exist?"* without actually running the subcommand.

Two layers:

1. **Static map** — ``SUBCOMMAND_CONFIG_SECTIONS`` declares which
   top-level config sections each CLI subcommand materializes.
   Hand-curated; a drift-guard test asserts every registered command is
   either declared here or in ``COMMANDS_WITHOUT_CONFIG_ACCESS``.

2. **Dynamic walker** — ``reachable_envvars(config, subcommand)`` walks
   the declared sections of a parsed ``fraises.yaml`` and returns every
   ``LazyEnv`` it finds as ``EnvVarRef(name, yaml_path, is_set)``.
   Reuses ``LazyEnv`` from ``fraisier.config._lazy_env`` so we never
   resolve secrets — only inspect placeholders.

Why no auto-derivation? Click's dynamic dispatch + per-command
``ctx.obj["config"]`` access patterns make static analysis of "which
sections does this subcommand read?" unreliable. A hand map drifts only
when commands change config consumption, and the drift guard catches
that at CI time.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import Any, NamedTuple

from fraisier.config._lazy_env import LazyEnv


class EnvVarRef(NamedTuple):
    """A ``!envvar`` reference reachable from a subcommand's config sections."""

    name: str
    yaml_path: str
    is_set: bool


@dataclass(frozen=True)
class ConfigPath:
    """Dotted YAML path with ``*`` glob support on segment boundaries.

    ``ConfigPath("environments.*.database")`` matches
    ``environments.production.database`` but not
    ``environments.production.smoke_tests``. A path also matches any
    descendant of its declared subtree
    (``ConfigPath("environments.*")`` matches
    ``environments.production.database.url``).
    """

    spec: str

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.spec.split("."))

    def matches(self, path: str) -> bool:
        spec_parts = self.parts
        # Drop trailing list-index notation [N] from path segments before
        # comparing — globbing matches on the dotted shape, not indexing.
        path_parts = [_strip_index(p) for p in path.split(".")]
        if len(path_parts) < len(spec_parts):
            return False
        # Match each declared part to the corresponding path part; allow
        # longer paths to be considered matches (prefix semantics).
        for spec_part, path_part in zip(spec_parts, path_parts, strict=False):
            if not fnmatch.fnmatchcase(path_part, spec_part):
                return False
        return True


def _strip_index(segment: str) -> str:
    idx = segment.find("[")
    return segment[:idx] if idx >= 0 else segment


# Shared constants — keep diffs reviewable when many commands reuse the
# same section glob.
_GIT = ConfigPath("git")
_SHIP = ConfigPath("ship")
_FRAISES = ConfigPath("fraises")
_NOTIFICATIONS = ConfigPath("notifications")
_HOOKS = ConfigPath("hooks")
_BOOTSTRAP = ConfigPath("bootstrap")
_ENV_DATABASE = ConfigPath("environments.*.database")
_ENV_HEALTH = ConfigPath("environments.*.health_check")
_ENV_SMOKE = ConfigPath("environments.*.smoke_tests")
_ENV_NOTIFICATIONS = ConfigPath("environments.*.notifications")
_ENV_SYSTEMD = ConfigPath("environments.*.systemd_service")
_ENV_POST_MIGRATE = ConfigPath("environments.*.database")
_ENV_GIT = ConfigPath("environments.*.git")
_ENV_SSH = ConfigPath("environments.*.ssh")
_ENV_FULL = ConfigPath("environments.*")
_FULL_CONFIG = ConfigPath("*")  # walk everything

_DB_SECTIONS: frozenset[ConfigPath] = frozenset({_ENV_DATABASE})
_DEPLOY_SECTIONS: frozenset[ConfigPath] = frozenset(
    {
        _ENV_DATABASE,
        _ENV_HEALTH,
        _ENV_SMOKE,
        _ENV_NOTIFICATIONS,
        _ENV_SYSTEMD,
        _ENV_GIT,
    }
)
_HEALTH_SECTIONS: frozenset[ConfigPath] = frozenset({_ENV_HEALTH})
_ROLLBACK_SECTIONS: frozenset[ConfigPath] = frozenset(
    {_ENV_SYSTEMD, _ENV_DATABASE, _ENV_GIT}
)
_DIAGNOSE_SECTIONS: frozenset[ConfigPath] = frozenset({_ENV_SYSTEMD, _ENV_HEALTH})


SUBCOMMAND_CONFIG_SECTIONS: dict[str, frozenset[ConfigPath]] = {
    # Alphabetized for reviewable diffs.
    "backup": _DB_SECTIONS,
    "bootstrap": frozenset({_ENV_FULL}),
    "bootstrap-preflight": frozenset({_ENV_FULL}),
    "db build": _DB_SECTIONS,
    "db exec": _DB_SECTIONS,
    "db migrate": _DB_SECTIONS,
    "db preflight": _DB_SECTIONS,
    "db reset": _DB_SECTIONS,
    "db restore": _DB_SECTIONS,
    "db-check": _DB_SECTIONS,
    "deployment-status": frozenset(),  # reads state files, not config
    "env-check": frozenset(),  # introspection over caller-named subcommand
    "diagnose": _DIAGNOSE_SECTIONS,
    "health": _HEALTH_SECTIONS,
    "history": frozenset(),  # reads deployment DB
    "list": frozenset({_FRAISES}),
    "logs": frozenset({_ENV_SYSTEMD}),
    "metrics": frozenset(),  # only reads its own server config
    "notify": frozenset({_ENV_NOTIFICATIONS, _GIT, _NOTIFICATIONS}),
    "repair-remote": _DIAGNOSE_SECTIONS,
    "rollback": _ROLLBACK_SECTIONS,
    "scaffold": frozenset({_FULL_CONFIG}),
    "scaffold-diff": frozenset({_FULL_CONFIG}),
    "scaffold-install": frozenset(),  # writes a new config; doesn't read existing
    "setup": frozenset({_ENV_FULL}),
    "ship": frozenset({_SHIP, _GIT}),
    "stats": frozenset(),  # reads deployment DB
    "status": frozenset({_ENV_FULL}),  # may show any field
    "status-all": frozenset(),  # walks deployment DB only
    "sync": frozenset({_GIT, _ENV_GIT}),
    "test-database": _DB_SECTIONS,
    "test-git": frozenset({_GIT, _ENV_GIT}),
    "test-health": _HEALTH_SECTIONS,
    "test-install": frozenset({_ENV_FULL}),
    "test-wrapper": frozenset({_ENV_SYSTEMD}),
    "trigger-deploy": _DEPLOY_SECTIONS,
    "validate": frozenset({_FULL_CONFIG}),
    "validate-deployment": frozenset({_FULL_CONFIG}),
    "validate-remote": frozenset({_FULL_CONFIG}),
    "validate-setup": frozenset({_FULL_CONFIG}),
    "webhooks": frozenset(),  # reads webhook DB only
}


COMMANDS_WITHOUT_CONFIG_ACCESS: frozenset[str] = frozenset(
    {
        "deploy-daemon",  # reads stdin JSON, not fraises.yaml
        "init",  # creates fraises.yaml, doesn't read existing
        "providers",  # reads provider registry only
        "provider-info",  # reads provider registry only
        "provider-test",  # reads optional --config-file, not fraises.yaml
        "test-db status",
        "test-db rebuild",
        "test-db clean",
        "version show",  # reads version.json, not fraises.yaml
        "version bump",  # reads pyproject.toml, not fraises.yaml
    }
)


def reachable_envvars(
    config: dict[str, Any] | None,
    subcommand: str,
    *,
    fraise: str | None = None,
    environment: str | None = None,
) -> list[EnvVarRef]:
    """Return every ``!envvar`` ref reachable from ``subcommand``'s sections.

    Walks the declared sections of ``config`` and returns ``EnvVarRef``
    tuples in traversal order (deterministic; not sorted alphabetically
    so consumers see YAML order). Never calls ``LazyEnv.resolve()`` —
    only checks ``os.environ`` for ``is_set`` via name lookup.

    Args:
        config: Parsed fraises.yaml as a dict, or ``None`` (returns ``[]``).
        subcommand: Subcommand name as registered in
            ``SUBCOMMAND_CONFIG_SECTIONS``. Unknown names return ``[]``.
        fraise: When set, restrict the walk to this fraise's subtree
            under ``fraises.<name>``. Otherwise walk all fraises.
        environment: When set with ``fraise``, restrict further to
            ``fraises.<name>.environments.<env>``.

    Returns:
        Per-LazyEnv refs in traversal order. May contain duplicates if
        the same env-var name appears at multiple YAML paths.
    """
    if config is None:
        return []
    sections = SUBCOMMAND_CONFIG_SECTIONS.get(subcommand)
    if not sections:
        return []

    refs: list[EnvVarRef] = []
    # We don't pre-filter by section glob — instead the walk emits
    # refs along with their constructed yaml_path and we keep those whose
    # path matches at least one declared section glob.
    _walk(config, "", refs, sections, fraise=fraise, environment=environment)
    return refs


def _walk(
    node: Any,
    path: str,
    out: list[EnvVarRef],
    sections: frozenset[ConfigPath],
    *,
    fraise: str | None,
    environment: str | None,
) -> None:
    # Fraise/env scoping: when the walker reaches `fraises.<name>` or
    # `fraises.<name>.environments.<env>`, restrict descent.
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            if fraise is not None and path == "fraises" and key != fraise:
                continue
            if (
                environment is not None
                and path.endswith("environments")
                and key != environment
            ):
                continue
            _walk(
                value, child_path, out, sections, fraise=fraise, environment=environment
            )
    elif isinstance(node, list):
        for i, item in enumerate(node):
            child_path = f"{path}[{i}]"
            _walk(
                item, child_path, out, sections, fraise=fraise, environment=environment
            )
    elif isinstance(node, LazyEnv):
        if _path_in_sections(path, sections):
            out.append(
                EnvVarRef(
                    name=node.name,
                    yaml_path=node.yaml_path or path,
                    is_set=node.name in os.environ,
                )
            )


def _path_in_sections(path: str, sections: frozenset[ConfigPath]) -> bool:
    """A LazyEnv at ``path`` is reachable when any declared section glob
    matches a prefix of ``path`` (or all of it, or the LazyEnv lives
    under ``fraises.<name>.environments.<env>.<declared-section>``).
    """
    # The declared sections live under fraises.*.environments.*. so we
    # translate a path like
    # `fraises.api.environments.production.smoke_tests[0].headers.Authorization`
    # into the env-relative form `environments.production.smoke_tests`
    # to compare against `environments.*.smoke_tests`.
    if path.startswith("fraises."):
        rest = path.split(".", 2)
        if len(rest) >= 3:
            env_relative = rest[2]  # e.g. environments.production.smoke_tests[0]....
            for cp in sections:
                if cp.matches(env_relative):
                    return True
    # Top-level sections (ship, git, notifications, hooks, bootstrap).
    for cp in sections:
        if cp.matches(path):
            return True
    # Full-config wildcard.
    return ConfigPath("*") in sections
