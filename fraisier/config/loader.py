"""Configuration loader for Fraisier deployment system.

Loads fraise definitions from fraises.yaml.
Supports hierarchical fraise -> environment structure.

Loading is split into two stages:

* **Stage 1** — cheap, contentless structural checks run in
  :meth:`FraisierConfig._load`: ``servers``, ``branch_mapping``, and
  ``service_manager``. Stage 1 never reads :mod:`os.environ` and never
  invokes a deep section validator.
* **Stage 2** — deep validators for ``fraises``, ``notifications``, and
  ``hooks`` run on first access of the matching property (e.g.
  :meth:`FraisierConfig.notifications`,
  :meth:`FraisierConfig.get_fraise_environment`) and memoize via
  :func:`functools.cached_property` / :func:`functools.cache`.

``!envvar`` references parse into :class:`fraisier.config.LazyEnv`
placeholders; the :func:`os.environ` lookup is deferred to consumption
time (``to_str(value)`` at consumer boundaries) and re-reads on every
access. ``fraisier validate --resolve-envvars`` forces a one-shot walk
that resolves every reachable placeholder for the pre-deploy CI gate.
"""

import functools
import logging
import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import yaml

from fraisier.config._lazy_env import LazyEnv
from fraisier.config._validation import (
    validate_branch_mapping,
    validate_hooks,
    validate_notifications,
    validate_one_fraise_environment,
    validate_servers,
    validate_service_manager,
)

# Import all schema definitions from schema module
from fraisier.config.schema import (
    _VALID_LOCK_BACKENDS,
    _VALID_STRATEGIES,
    DeploymentConfig,
    GithubActionsScaffoldConfig,
    HealthConfig,
    HealthResponseConfig,
    NginxScaffoldConfig,
    PostgresLoggingConfig,
    ScaffoldConfig,
    ShipCheckConfig,
    ShipConfig,
    SyncPair,
    SystemdScaffoldConfig,
    _config_search_locations,
)
from fraisier.errors import ConfigurationError, ValidationError

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 600  # 10 minutes
_MEMORY_SIZE_RE = re.compile(r"^\d+[KMGT]$")
_VALID_SERVICE_TYPES = {
    "simple",
    "exec",
    "forking",
    "oneshot",
    "dbus",
    "notify",
    "notify-reload",
    "idle",
}


class _FraisierYamlLoader(yaml.SafeLoader):
    """SafeLoader subclass that materializes ``!envvar`` tags as ``LazyEnv``.

    Use:
        headers:
          Authorization: !envvar SMOKE_TEST_JWT

    Resolution is deferred: parsing produces a :class:`LazyEnv`
    placeholder; the ``os.environ`` lookup happens at consumption time
    via :func:`to_str` (or any of the str-parity dunders on the
    placeholder). Empty strings are still "set" and resolve to ``""``.
    """


def _construct_envvar(loader: yaml.Loader, node: yaml.Node) -> LazyEnv:
    """Construct a :class:`LazyEnv` for a ``!envvar`` YAML tag.

    No ``os.environ`` lookup happens here. The placeholder carries the
    env var ``name``; ``yaml_path`` is set to ``"<unknown>"`` and
    overwritten by :func:`_attach_paths` once the parse tree is fully
    built, so resolution failures can name the offending YAML line.
    """
    if not isinstance(node, yaml.ScalarNode):
        raise ConfigurationError(
            f"!envvar expects a scalar variable name, got {type(node).__name__}"
        )
    name = loader.construct_scalar(node)
    return LazyEnv(name, yaml_path="<unknown>")


_FraisierYamlLoader.add_constructor("!envvar", _construct_envvar)


def _attach_paths(obj: Any, prefix: str = "") -> None:
    """Stamp every ``LazyEnv`` reachable from *obj* with its YAML key path.

    Walks the structure produced by ``yaml.load`` once after parsing.
    The ``LazyEnv.yaml_path`` is mutated in place so deferred resolution
    failures can name the offending YAML location.

    First-seen wins for YAML anchors / aliases that share a single
    ``LazyEnv`` instance across multiple locations.
    """
    if isinstance(obj, LazyEnv):
        if obj.yaml_path in (None, "<unknown>"):
            obj.yaml_path = prefix or "<root>"
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            _attach_paths(value, child)
        return
    if isinstance(obj, list):
        for i, value in enumerate(obj):
            _attach_paths(value, f"{prefix}[{i}]")


class FraisierConfig:
    """Load and manage deployment configuration from fraises.yaml.

    Supports hierarchical structure:
        fraises:
          <fraise_name>:
            type: api|etl|scheduled|backup
            environments:
              <env_name>:
                <config>
    """

    def __init__(self, config_path: Path | str | None = None):
        """Initialize configuration.

        Args:
            config_path: Path to fraises.yaml. If None, uses default locations.
        """
        self.config_path = self._resolve_config_path(config_path)
        self._config: dict[str, Any] = {}
        self._load()

    def _resolve_config_path(self, config_path: Path | str | None) -> Path:
        """Resolve configuration file path."""
        if config_path:
            return Path(config_path)

        # Check FRAISIER_CONFIG environment variable
        env_path = os.environ.get("FRAISIER_CONFIG")
        if env_path:
            return Path(env_path)

        # Check standard locations (CWD first, then system-wide)
        locations = _config_search_locations()
        for loc in locations:
            if loc.exists():
                return loc

        locations_str = [str(p) for p in locations]
        raise FileNotFoundError(f"fraises.yaml not found in any of: {locations_str}")

    def _load(self) -> None:
        """Load configuration from YAML file.

        Stage 1 only: cheap, contentless structural checks. Deep section
        validators (``fraises``, ``notifications``, ``hooks``) run lazily
        on first access of the matching property.
        """
        with Path(self.config_path).open() as f:
            self._config = yaml.load(f, Loader=_FraisierYamlLoader)
        _attach_paths(self._config)
        # Stage 1: cross-reference + shape checks only.
        validate_servers(self._config.get("servers", {}))
        validate_branch_mapping(
            self._config.get("branch_mapping", {}),
            self._config.get("fraises", {}),
        )
        validate_service_manager(self._config.get("service_manager"))
        # Drop any cached Stage-2 results from a prior load.
        for prop in ("notifications", "hooks"):
            self.__dict__.pop(prop, None)
        self._get_validated_env.cache_clear()

    def _validate_then_return(
        self,
        validator: Callable[[Any], None],
        raw: Any,
    ) -> Any:
        """Run a Stage-2 validator, then return the raw value unchanged.

        Used by ``@cached_property`` accessors so a section's deep
        validation runs on first access and is memoized via the standard
        ``functools`` cache (which does NOT cache exceptions — failed
        validations re-raise on every subsequent access).
        """
        validator(raw)
        return raw

    @functools.cached_property
    def notifications(self) -> dict[str, Any]:
        """Notifications configuration, validated on first access."""
        return self._validate_then_return(
            validate_notifications,
            self._config.get("notifications", {}),
        )

    @functools.cached_property
    def hooks(self) -> dict[str, Any]:
        """Lifecycle hooks configuration, validated on first access."""
        return self._validate_then_return(
            validate_hooks,
            self._config.get("hooks", {}),
        )

    def reload(self) -> None:
        """Reload configuration from file."""
        self._load()

    @property
    def deployment(self) -> DeploymentConfig:
        """Get parsed deployment configuration with validation."""
        raw = self._config.get("deployment", {}) or {}
        strategies = raw.get("strategies", {}) or {}
        for env, strat in strategies.items():
            if strat not in _VALID_STRATEGIES:
                valid = ", ".join(sorted(_VALID_STRATEGIES))
                raise ValidationError(
                    f"Invalid strategy '{strat}' for {env}. Valid: {valid}",
                )
        lock_backend = raw.get("lock_backend", "file")
        if lock_backend not in _VALID_LOCK_BACKENDS:
            valid = ", ".join(sorted(_VALID_LOCK_BACKENDS))
            raise ValidationError(
                f"Invalid lock_backend '{lock_backend}'. Valid: {valid}",
            )

        return DeploymentConfig(
            lock_dir=raw.get("lock_dir", "/run/fraisier"),
            lock_backend=lock_backend,
            lock_db_path=raw.get("lock_db_path", "/var/lib/fraisier/locks.db"),
            status_file=raw.get("status_file", "deployment_status.json"),
            deploy_user=raw.get("deploy_user", "fraisier"),
            strategies=strategies,
            timeouts=raw.get("timeouts", {}) or {},
        )

    @property
    def scaffold(self) -> ScaffoldConfig:
        """Get parsed scaffold configuration with defaults."""
        raw = self._config.get("scaffold", {}) or {}
        raw_systemd = raw.get("systemd", {}) or {}
        raw_nginx = raw.get("nginx", {}) or {}
        raw_gh = raw.get("github_actions", {}) or {}
        raw_pg = raw.get("postgresql", {}) or {}

        # Fallback deploy_user: scaffold -> top-level -> deployment -> "fraisier"
        deploy_user = raw.get("deploy_user")
        if not deploy_user:
            deploy_user = self._config.get("deploy_user")
        if not deploy_user:
            dep_raw = self._config.get("deployment", {}) or {}
            deploy_user = dep_raw.get("deploy_user", "fraisier")

        return ScaffoldConfig(
            output_dir=raw.get("output_dir", "scripts/generated"),
            state_dir=raw.get("state_dir", ""),
            deploy_user=deploy_user,
            config_path=raw.get("config_path", "/opt/fraisier/fraises.yaml"),
            deploy_environment_file=raw.get("deploy_environment_file"),
            template_dir=raw.get("template_dir"),
            systemd=SystemdScaffoldConfig(
                security_hardening=raw_systemd.get("security_hardening", True),
                memory_max_default=raw_systemd.get("memory_max_default", "4G"),
            ),
            nginx=NginxScaffoldConfig(
                ssl_provider=raw_nginx.get("ssl_provider", "letsencrypt"),
                cors_origins=raw_nginx.get("cors_origins", []),
                rate_limit=raw_nginx.get("rate_limit", "10r/s"),
                restricted_paths=raw_nginx.get("restricted_paths", []),
                webhook_port=raw_nginx.get("webhook_port", 8080),
                gateway_fraise=raw_nginx.get("gateway_fraise"),
            ),
            github_actions=GithubActionsScaffoldConfig(
                python_versions=raw_gh.get("python_versions", ["3.12"]),
                test_command=raw_gh.get("test_command", "uv run pytest"),
                lint_command=raw_gh.get("lint_command", "uv run ruff check"),
                format_command=raw_gh.get(
                    "format_command", "uv run ruff format --check"
                ),
            ),
            postgresql=PostgresLoggingConfig(
                log_min_duration_statement=raw_pg.get("log_min_duration_statement"),
                log_statement=raw_pg.get("log_statement"),
                log_connections=raw_pg.get("log_connections"),
                log_line_prefix=raw_pg.get("log_line_prefix", "'%m [%p] %q%u@%d '"),
                log_min_error_statement=raw_pg.get("log_min_error_statement", "error"),
                log_error_verbosity=raw_pg.get("log_error_verbosity", "default"),
                deadlock_timeout=raw_pg.get("deadlock_timeout", "1s"),
                log_lock_waits=raw_pg.get("log_lock_waits", True),
                log_rotation_age=raw_pg.get("log_rotation_age", "1d"),
                log_rotation_size=raw_pg.get("log_rotation_size", "100MB"),
            ),
            sync=[
                SyncPair(
                    source=p["source"],
                    target=p["target"],
                    prefer_source=bool(p.get("prefer_source", False)),
                )
                for p in (raw.get("sync") or [])
                if isinstance(p, dict) and p.get("source") and p.get("target")
            ],
        )

    @property
    def health(self) -> HealthConfig:
        """Get parsed health configuration with defaults."""
        raw = self._config.get("health", {}) or {}
        raw_response = raw.get("response", {}) or {}
        return HealthConfig(
            startup_timeout_seconds=raw.get("startup_timeout_seconds", 120),
            deploy_poll_interval_seconds=raw.get("deploy_poll_interval_seconds", 5),
            endpoints=raw.get("endpoints", ["/health"]),
            response=HealthResponseConfig(
                include_version=raw_response.get("include_version", True),
                include_schema_hash=raw_response.get("include_schema_hash", True),
                include_response_time=raw_response.get("include_response_time", True),
                include_database=raw_response.get("include_database", False),
                include_environment=raw_response.get("include_environment", False),
                include_commit=raw_response.get("include_commit", False),
            ),
            version_field=raw.get("version_field", "version"),
            migration_field=raw.get("migration_field", "migration"),
        )

    @property
    def ship(self) -> ShipConfig:
        """Get parsed ship pipeline configuration."""
        raw = self._config.get("ship", {}) or {}
        raw_checks = raw.get("checks", []) or []
        valid_phases = {"fix", "validate", "test"}
        checks = []
        for c in raw_checks:
            phase = c.get("phase", "validate")
            if phase not in valid_phases:
                raise ValidationError(
                    f"Invalid ship check phase '{phase}' for "
                    f"'{c.get('name', '?')}'. "
                    f"Valid: {', '.join(sorted(valid_phases))}",
                )
            checks.append(
                ShipCheckConfig(
                    name=c["name"],
                    command=c.get("command", []),
                    phase=phase,
                    triggers=c.get("triggers"),
                    timeout=c.get("timeout", 60),
                )
            )
        return ShipConfig(
            checks=checks,
            pr_base=raw.get("pr_base"),
            parallel=raw.get("parallel", True),
            auto_merge=raw.get("auto_merge", False),
            merge_method=raw.get("merge_method", "squash"),
        )

    @property
    def project_name(self) -> str:
        """Project name used to prefix generated service names.

        Resolution order:
        1. Explicit ``name`` field in fraises.yaml
        2. Git repository basename
        3. Current working directory basename
        """
        name = self._config.get("name")
        if name:
            return str(name)

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )
            return Path(result.stdout.strip()).name
        except (subprocess.CalledProcessError, FileNotFoundError):
            logging.getLogger(__name__).debug(
                "Could not determine git repo name, using cwd"
            )

        return Path.cwd().name

    @property
    def scaffold_state_dir(self) -> str:
        """Absolute, project-level directory holding the server-side scaffold tree.

        This is the single source of truth for where the deploy path renders,
        installs, and reads the generated units — decoupled from the per-env
        ``app_path`` and from the CWD-relative ``scaffold.output_dir`` (which
        remains a local render/review concern only).

        Defaults to ``/var/lib/fraisier/{project}/scaffold``: already writable
        by every ``ProtectSystem=strict`` deploy unit and readable by the
        root-privileged install helper, so no ``ReadWritePaths`` changes are
        needed. Override with ``scaffold.state_dir`` (e.g. ``/opt/{project}``,
        which additionally requires whitelisting that path in the deploy units).
        """
        explicit = self.scaffold.state_dir
        if explicit:
            return explicit
        return f"/var/lib/fraisier/{self.project_name}/scaffold"

    @property
    def fraises(self) -> dict[str, dict[str, Any]]:
        """Get all fraise configurations."""
        return self._config.get("fraises", {})

    @property
    def environments(self) -> dict[str, dict[str, Any]]:
        """Get global environment configurations."""
        return self._config.get("environments", {})

    @property
    def branch_mapping(self) -> dict[str, list[dict[str, str]]]:
        """Get branch to fraise/environment mapping.

        Normalizes both single-dict and list-of-dicts syntax to always
        return lists, enabling monorepo workflows where one branch
        deploys multiple fraises.
        """
        raw = self._config.get("branch_mapping", {})
        result: dict[str, list[dict[str, str]]] = {}
        for branch, mapping in raw.items():
            if isinstance(mapping, dict):
                result[branch] = [mapping]
            elif isinstance(mapping, list):
                result[branch] = mapping
            else:
                result[branch] = []
        return result

    @property
    def webhook(self) -> dict[str, Any]:
        """Get global webhook configuration."""
        return self._config.get("webhook", {})

    @property
    def servers(self) -> dict[str, list[str]]:
        """Map logical server hostname → list of machine hostnames.

        Example:
            servers:
              prod.example.com:
                machine_hostnames: [backend-prod-01, backend-prod-02]
        """
        raw = self._config.get("servers", {}) or {}
        return {
            logical: (
                entry.get("machine_hostnames", []) if isinstance(entry, dict) else []
            )
            for logical, entry in raw.items()
        }

    def get_fraise(self, fraise_name: str) -> dict[str, Any] | None:
        """Get configuration for a fraise (all environments)."""
        return self.fraises.get(fraise_name)

    def get_fraise_environment(
        self, fraise_name: str, environment: str
    ) -> dict[str, Any] | None:
        """Get configuration for a specific fraise + environment.

        Triggers Stage-2 validation of that env on first access and
        memoizes the result keyed on ``(fraise_name, environment)``.

        Args:
            fraise_name: e.g., "my_api", "etl", "backup"
            environment: e.g., "development", "staging", "production"

        Returns:
            Merged config with fraise-level and environment-level settings
        """
        return self._get_validated_env(fraise_name, environment)

    @functools.cache  # noqa: B019  — cleared explicitly by _load(); FraisierConfig is hashable
    def _get_validated_env(
        self, fraise_name: str, environment: str
    ) -> dict[str, Any] | None:
        """Validated, merged env config. Memoized per ``(fraise, env)``."""
        fraise = self._config.get("fraises", {}).get(fraise_name)
        if not isinstance(fraise, dict):
            return None

        env_config = fraise.get("environments", {}).get(environment)
        if not isinstance(env_config, dict) or not env_config:
            return None

        validate_one_fraise_environment(fraise_name, environment, env_config)

        merged = {
            "fraise_name": fraise_name,
            "environment": environment,
            "type": fraise.get("type"),
            "description": fraise.get("description"),
            **env_config,
        }
        if "install" not in merged and "install" in fraise:
            merged["install"] = fraise["install"]
        return merged

    def get_fraises_for_branch(self, branch: str) -> list[dict[str, Any]]:
        """Get fraise configurations for a git branch (webhook routing).

        Supports monorepo workflows where one branch maps to multiple fraises.

        Args:
            branch: Git branch name (e.g., "dev", "main")

        Returns:
            List of fraise+environment configs for the branch
        """
        mappings = self.branch_mapping.get(branch)
        if not mappings:
            return []

        results = []
        for mapping in mappings:
            fraise_name = mapping.get("fraise") or mapping.get("fraise_name")
            environment = mapping.get("environment")
            if not fraise_name or not environment:
                continue
            config = self.get_fraise_environment(fraise_name, environment)
            if config:
                results.append(config)
        return results

    def get_fraise_for_branch(self, branch: str) -> dict[str, Any] | None:
        """Get fraise configuration for a git branch (webhook routing).

        .. deprecated::
            Use :meth:`get_fraises_for_branch` for multi-fraise support.

        Returns:
            Full fraise+environment config for the first mapped fraise
        """
        results = self.get_fraises_for_branch(branch)
        return results[0] if results else None

    def list_fraises(self) -> list[str]:
        """List all fraise names.

        Returns:
            List of fraise name strings
        """
        return list(self.fraises.keys())

    def list_all_deployments(self) -> list[dict[str, Any]]:
        """List all fraise+environment combinations (deployable targets).

        Returns:
            List of all deployable targets
        """
        result = []
        for fraise_name, fraise in self.fraises.items():
            fraise_type = fraise.get("type", "unknown")
            description = fraise.get("description", "")

            for env_name, env_config in fraise.get("environments", {}).items():
                # Handle fraises with nested jobs (backup, statistics)
                if "jobs" in env_config:
                    for job_name, job_config in env_config["jobs"].items():
                        result.append(
                            {
                                "fraise": fraise_name,
                                "environment": env_name,
                                "job": job_name,
                                "type": fraise_type,
                                "name": job_config.get("name", job_name),
                                "description": job_config.get(
                                    "description", description
                                ),
                            }
                        )
                else:
                    result.append(
                        {
                            "fraise": fraise_name,
                            "environment": env_name,
                            "job": None,
                            "type": fraise_type,
                            "name": env_config.get("name", fraise_name),
                            "description": description,
                        }
                    )
        return result

    def get_deployments_by_type(self, fraise_type: str) -> list[dict[str, Any]]:
        """Get all deployments of a specific type."""
        return [d for d in self.list_all_deployments() if d["type"] == fraise_type]

    def get_deployments_by_environment(self, environment: str) -> list[dict[str, Any]]:
        """Get all deployments for a specific environment."""
        return [
            d for d in self.list_all_deployments() if d["environment"] == environment
        ]

    def get_environment(
        self, fraise_name: str, environment: str
    ) -> dict[str, Any] | None:
        """Get environment config for a fraise. Alias for get_fraise_environment."""
        return self.get_fraise_environment(fraise_name, environment)

    def get_deploy_user(self, fraise_name: str, environment: str) -> str:
        """Resolve effective deploy_user for a fraise/environment pair.

        Priority: environment-level deploy_user > scaffold.deploy_user.
        """
        env = self.get_fraise_environment(fraise_name, environment)
        if env and env.get("deploy_user"):
            return env["deploy_user"]
        return self.scaffold.deploy_user

    def get_git_provider_config(self) -> dict[str, Any]:
        """Get git provider configuration."""
        return self._config.get("git", {})

    def list_environments(self, fraise_name: str) -> list[str]:
        """List environment names for a fraise."""
        fraise = self.fraises.get(fraise_name)
        if not fraise:
            return []
        return list(fraise.get("environments", {}).keys())

    def iter_environment_servers(self) -> Iterator[tuple[str | None, str, str | None]]:
        """Yield ``(owning_fraise, env_name, declared_server)`` per declaration site.

        ``server:`` may be written in the global ``environments:`` section or
        under ``fraises.<name>.environments.<env>``, and a config may use
        either or both. This is the single walk over both sites; every
        consumer that needs to know where an environment lives — the
        renderer's server set, :meth:`get_environments_for_server`, and
        :meth:`get_machine_scope_map`, which bakes install.sh's host gating —
        derives from it.

        Keeping them on one walk is the point: while the renderer read only
        the global section and the installer read both, a config declaring
        ``server:`` only per-fraise looked server-less to the renderer (one
        webhook unit carrying every host's trees) and correctly filtered to
        the installer — the #62 least-privilege leak reached by a second
        route, and one of the three routes into #325.

        The owner is what the global branch cannot have and the per-fraise
        branch used to throw away (#336). ``None`` means *every fraise using
        this name*, which is the correct reading of a global declaration and
        the reason a config written that way is unaffected by the fix. A
        fraise name binds the declaration to that fraise alone, so two
        fraises putting the same environment name on different servers no
        longer make each host see the other's units as local.

        The same environment name may be yielded more than once, with a
        different owner and a different (or absent) server each time; callers
        decide how to fold.
        """
        for env_name, env_config in self.environments.items():
            if isinstance(env_config, dict):
                yield None, env_name, env_config.get("server")

        for fraise_name, fraise in self.fraises.items():
            for env_name, env_config in (fraise.get("environments") or {}).items():
                if isinstance(env_config, dict):
                    yield fraise_name, env_name, env_config.get("server")

    def declared_servers(self) -> list[str]:
        """Unique logical servers named by any environment, in declaration order.

        Empty when no environment declares a ``server:`` — which is exactly
        the condition for single-host mode in the scaffold renderer. Note this
        is a property of the *config*, never of a ``--server`` filter.
        """
        seen: dict[str, None] = {}
        for _, _env, server in self.iter_environment_servers():
            if server:
                seen[str(server)] = None
        return list(seen)

    def environments_without_a_server(self) -> list[str]:
        """Environment names used by a fraise that name no logical server.

        Only meaningful alongside :meth:`declared_servers`: in a multi-host
        config such an environment resolves to *no* host, so its ``git_repo``
        and ``app_path`` reach no webhook unit's ``ReadWritePaths=`` and every
        deploy of it fails on a read-only filesystem. Ordered by first
        appearance so the resulting diagnostic is stable.
        """
        hosted: set[str] = set()
        candidates: dict[str, None] = {}
        for _fraise, env_name, server in self.iter_environment_servers():
            if server:
                hosted.add(env_name)
        for fraise in self.fraises.values():
            for env_name in fraise.get("environments") or {}:
                if env_name not in hosted:
                    candidates[env_name] = None
        return list(candidates)

    def get_scopes_for_server(self, server: str) -> list[tuple[str | None, str]]:
        """Return ``(owning_fraise, env_name)`` pairs declared on *server*.

        The fraise-aware form of :meth:`get_environments_for_server`, and the
        one the host gate is built from (#336). A ``None`` owner is a global
        ``environments:`` declaration and means *every fraise using this
        name*; a name binds the pair to that fraise alone.

        Returns an empty list when no declaration names that server.
        """
        matched: dict[tuple[str | None, str], None] = {}
        for fraise_name, env_name, declared in self.iter_environment_servers():
            if declared == server:
                matched[(fraise_name, env_name)] = None
        return list(matched)

    def get_environments_for_server(self, server: str) -> list[str]:
        """Return environment names whose ``server`` field matches *server*.

        Checks both the global ``environments`` section and per-fraise
        environment configs, deduplicating the result.
        Returns an empty list when no environment declares that server.

        The owner-discarding view of :meth:`get_scopes_for_server`, kept for
        the consumers that genuinely ask an environment-name question —
        ``doctor``'s and ``setup``'s host summaries, ``--server`` filtering in
        ``cli/_info``. The host gate does *not* read this: discarding the
        owner there is #336.
        """
        matched: dict[str, None] = {}
        for _fraise, env_name in self.get_scopes_for_server(server):
            matched[env_name] = None
        return list(matched)

    def get_machine_scope_map(self) -> dict[str, list[tuple[str | None, str]]]:
        """Build reverse map: machine_hostname → [(owning_fraise, env), ...].

        For each machine in the servers: section, collect every
        ``(fraise, environment)`` pair assigned to its logical server. This is
        what ``install.sh``'s host gate is baked from, so the pair — not the
        environment name alone — is what decides whether a machine installs an
        artifact.

        Returns empty dict if servers: section is not configured.
        """
        result: dict[str, list[tuple[str | None, str]]] = {}
        for logical_server, machines in self.servers.items():
            scopes = self.get_scopes_for_server(logical_server)
            for machine in machines:
                result.setdefault(machine, []).extend(scopes)
        return result

    def get_machine_environment_map(self) -> dict[str, list[str]]:
        """Build reverse map: machine_hostname → [env_name, ...].

        The owner-discarding view of :meth:`get_machine_scope_map`, for
        callers that report which environments a machine carries rather than
        deciding what it installs.

        Returns empty dict if servers: section is not configured.
        """
        result: dict[str, list[str]] = {}
        for machine, scopes in self.get_machine_scope_map().items():
            envs: dict[str, None] = {}
            for _fraise, env_name in scopes:
                envs[env_name] = None
            result[machine] = list(envs)
        return result

    def list_fraises_detailed(self) -> list[dict[str, Any]]:
        """List all fraises with detailed info (type, description, environments)."""
        result = []
        for fraise_name, fraise in self.fraises.items():
            environments = list(fraise.get("environments", {}).keys())
            result.append(
                {
                    "name": fraise_name,
                    "type": fraise.get("type", "unknown"),
                    "description": fraise.get("description", ""),
                    "environments": environments,
                }
            )
        return result


# Global config instance (lazy loaded, thread-safe)
_config: FraisierConfig | None = None
_config_mtime: float | None = None
_config_lock = threading.Lock()


def _safe_mtime(path: Path | str) -> float | None:
    """Return ``path``'s mtime, or ``None`` when it can't be stat-ed.

    Powers the cheap staleness check in :func:`get_config`; a file that is
    briefly absent mid-atomic-replace must never crash config access.
    """
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def _load_and_stamp(config_path: Path | str | None) -> FraisierConfig:
    """Build a config and record the mtime of the file it read, together.

    Must be called with ``_config_lock`` held. Assigns ``_config`` and
    ``_config_mtime`` as a pair so a concurrent staleness check never pairs
    a fresh config with a stale mtime (or vice versa).
    """
    global _config, _config_mtime
    cfg = FraisierConfig(config_path)
    _config = cfg
    _config_mtime = _safe_mtime(cfg.config_path)
    return cfg


def get_config(config_path: Path | str | None = None) -> FraisierConfig:
    """Get or create the global configuration instance.

    A long-running process (e.g. the webhook) picks up an on-disk
    ``fraises.yaml`` change without a restart: each call cheaply ``stat()``s
    the resolved config path and rebuilds the singleton only when the mtime
    moves (#278). An explicit ``config_path`` always (re)builds.

    Resilience: a rebuild that fails — torn, removed, or invalid file —
    keeps the last-good singleton, stamps the offending mtime so it does not
    re-raise on every subsequent call, and logs a warning. A bad config sync
    must not take down a running webhook.
    """
    global _config_mtime
    if config_path:
        with _config_lock:
            return _load_and_stamp(config_path)

    # Capture once: a concurrent reset_config() may null the global.
    cfg = _config
    if cfg is None:
        with _config_lock:
            if _config is None:
                return _load_and_stamp(None)
            return _config

    current = _safe_mtime(cfg.config_path)
    if current is None or current == _config_mtime:
        return cfg

    with _config_lock:
        if _config is None:
            return _load_and_stamp(None)
        # Double-check under the lock: another thread may have reloaded
        # already, or the file may have changed again.
        if _safe_mtime(_config.config_path) == _config_mtime:
            return _config
        prev = _config
        try:
            return _load_and_stamp(_config.config_path)
        except Exception:
            _config_mtime = current
            logger.warning(
                "Reload of %s failed; keeping previous configuration",
                prev.config_path,
                exc_info=True,
            )
            return prev


def reset_config() -> None:
    """Reset the global configuration singleton.

    Next call to ``get_config()`` will re-read from disk.
    """
    global _config, _config_mtime
    with _config_lock:
        _config = None
        _config_mtime = None
