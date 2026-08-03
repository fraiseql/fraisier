"""Two-stage Jinja2 scaffold renderer.

Stage 1: Core templates (systemd, nginx, sudoers, install, shell scripts)
Stage 2: Provider-specific templates (GitHub Actions, confiture)

Templates are rendered with the full fraises.yaml context and written
to the configured output_dir.
"""

import logging
import re
import shutil
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jinja2

from fraisier.config import (
    SECURITY_DIRECTIVE_MAP,
    FraisierConfig,
    NginxEnvConfig,
    ServiceConfig,
    ValidationError,
)
from fraisier.dbops._validation import validate_service_name
from fraisier.manifest import build_manifest
from fraisier.naming import app_service_name, deploy_socket_name

logger = logging.getLogger("fraisier")

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Core template filenames (rendered for every project)
_CORE_TEMPLATES = [
    "core/sudoers.j2",
    "core/install.sh.j2",
    "core/confiture.yaml.j2",
    "core/backup.sh.j2",
    "core/db_reset.sh.j2",
    "core/db_deploy.sh.j2",
    "core/poll-deploy.service.j2",
]

# Provider-specific templates
_PROVIDER_TEMPLATES = [
    "provider/deploy.yml.j2",
]


def _format_security_value(value: str | bool) -> str:
    """Format a security directive value for systemd.

    Booleans become lowercase 'true'/'false', strings pass through.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _extract_port(health_check_url: str) -> int | None:
    """Extract port from a health check URL.

    Returns None if no explicit port is found.
    """
    try:
        parsed = urlparse(health_check_url)
        return parsed.port
    except (ValueError, AttributeError):
        return None


def _resolve_fraise_port(fraise: dict[str, Any]) -> int:
    """Resolve the port for a fraise from its first environment's health_check.url.

    Falls back to 8000 if no health_check URL is configured.
    """
    for env_config in fraise.get("environments", {}).values():
        hc = env_config.get("health_check", {})
        if isinstance(hc, dict):
            url = hc.get("url", "")
            if url:
                port = _extract_port(url)
                if port:
                    return port
    return 8000


# Mapping of command names to their absolute paths
_COMMAND_PATH_MAP = {
    "uv": "/usr/local/bin/uv",
    "systemctl": "/usr/bin/systemctl",
    "curl": "/usr/bin/curl",
    "tar": "/usr/bin/tar",
    "gunzip": "/usr/bin/gunzip",
    "psql": "/usr/bin/psql",
}


# Where to look for a command when it is in neither _COMMAND_PATH_MAP nor the
# scaffolding host's PATH. These are the target *server's* likely locations —
# scaffold often runs on a dev box or in a container whose PATH bears no
# relation to the box the sudoers rule will be installed on.
_COMMAND_SEARCH_DIRS = (
    "/usr/bin",
    "/bin",
    "/usr/local/bin",
    "/usr/sbin",
    "/sbin",
)


def _is_executable_file(path: Path) -> bool:
    """Return True when *path* is an existing regular file (seam for tests)."""
    return path.is_file()


def _absolute_command(token: str) -> str:
    """Resolve a single command token to an absolute path.

    sudoers requires a fully-qualified path in the ``Cmnd`` position; a bare
    token makes the parser reject the whole fragment (#287).

    Resolution order matters. ``_COMMAND_PATH_MAP`` wins over ``shutil.which``
    because ``fraisier scaffold`` frequently runs on a machine that is *not* the
    target server, where ``which`` would return a dev-box path (e.g. a per-user
    ``~/.local/bin/uv``) that is wrong for the box the rule is installed on.

    Returns the token unchanged when nothing resolves it; callers decide how
    loudly to complain.
    """
    if token.startswith("/"):
        return token
    if token in _COMMAND_PATH_MAP:
        return _COMMAND_PATH_MAP[token]

    found = shutil.which(token)
    if found:
        return found

    for directory in _COMMAND_SEARCH_DIRS:
        candidate = Path(directory) / token
        if _is_executable_file(candidate):
            return str(candidate)

    return token


def _resolve_command_path(cmd: str) -> str:
    """Resolve a command to its absolute path.

    Args:
        cmd: Command name or partial command (e.g., 'uv', 'uv sync --frozen')

    Returns:
        Command with absolute path for the first word, or original if not found.
    """
    parts = cmd.split(None, 1)
    if not parts:
        return cmd

    absolute = _absolute_command(parts[0])

    if len(parts) == 1:
        return absolute
    return f"{absolute} {parts[1]}"


def _collect_install_helper_sockets(
    project_name: str,
    local_fraises: list[dict[str, Any]],
    deploy_user: str,
) -> list[dict[str, str]]:
    """Return one entry per fraise+env that needs a separate install user.

    Each entry contains all fields needed by the webhook template and the
    install-helper renderer (fraise_name, env_name, install_user, app_path,
    socket_path, env_var, socket_unit, service_unit).
    """
    result: list[dict[str, Any]] = []
    for fraise in local_fraises:
        fraise_name = fraise["name"]
        fraise_install = fraise.get("install")
        for env_name, env_config in fraise.get("environments", {}).items():
            install = env_config.get("install") or (
                fraise_install if isinstance(fraise_install, dict) else None
            )
            if not isinstance(install, dict):
                continue
            install_user = install.get("user")
            if not install_user or install_user == deploy_user:
                continue
            app_path = env_config.get("app_path", "")
            if not app_path:
                continue
            safe_fraise = fraise_name.upper().replace("-", "_")
            safe_env = env_name.upper().replace("-", "_")
            result.append(
                {
                    "fraise_name": fraise_name,
                    "env_name": env_name,
                    "install_user": install_user,
                    "app_path": str(app_path),
                    "install_command": install.get("command") or [],
                    "socket_path": (
                        f"/run/fraisier/install-{project_name}-{fraise_name}-{env_name}.sock"
                    ),
                    "env_var": f"FRAISIER_INSTALL_SOCKET_{safe_fraise}_{safe_env}",
                    "socket_unit": (
                        f"fraisier-{project_name}-{fraise_name}-{env_name}"
                        "-install-helper.socket"
                    ),
                    "service_unit": (
                        f"fraisier-{project_name}-{fraise_name}-{env_name}"
                        "-install-helper.service"
                    ),
                }
            )
    return result


def _collect_unit_installer_envs(
    fraises_list: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Return ``{env_name: [allow_pair, ...]}`` covering every type:scheduled env.

    Each allow_pair is ``"<src_prefix>:<dest_prefix>"`` baked at scaffold time
    for the unit-installer helper's argv. Multiple fraises sharing one env get
    merged: one helper per env (Phase 0 decision #2 of #240). The dest prefix
    is always ``/etc/systemd/system/``.
    """
    envs: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for fraise in fraises_list:
        if fraise.get("type") != "scheduled":
            continue
        for env_name, raw_env_config in fraise.get("environments", {}).items():
            env_config = raw_env_config or {}
            app_path = env_config.get("app_path")
            if not app_path:
                continue
            src_prefix = f"{str(app_path).rstrip('/')}/scripts/systemd/"
            pair = f"{src_prefix}:/etc/systemd/system/"
            envs.setdefault(env_name, [])
            seen.setdefault(env_name, set())
            if pair not in seen[env_name]:
                envs[env_name].append(pair)
                seen[env_name].add(pair)
    return envs


def _resolve_service_base(
    project_name: str,
    fraise_name: str,
    env_name: str,
    env_config: dict[str, Any],
) -> str:
    """Return the systemd service unit base name (without .service suffix)."""
    return app_service_name(
        project_name, fraise_name, env_name, env_config
    ).removesuffix(".service")


def _collect_allowed_services(
    project_name: str, fraises_list: list[dict[str, Any]]
) -> list[str]:
    """Collect all systemd service names from fraises and environments.

    Returns fully-qualified service names (e.g., 'project_fraise_env.service').
    The webhook's own service unit is included so the #162 self-upgrade path
    can restart the webhook via the systemctl-helper socket.

    For ``type: scheduled`` fraises, also walks ``jobs.*`` and includes each
    job's ``systemd_service`` and ``systemd_timer`` so the webhook-driven
    ``ScheduledDeployer`` can enable/restart these units via the helper
    socket on each deploy (#239). This is symmetric in shape to the webhook
    fix in v0.22.2 but covers a separately-discovered gap.
    """
    services = [f"fraisier-{project_name}-webhook.service"]
    for fraise in fraises_list:
        fraise_name = fraise.get("name", "")
        if not fraise_name:
            continue
        fraise_type = fraise.get("type")
        for env_name, raw_env_config in fraise.get("environments", {}).items():
            env_config = raw_env_config or {}
            if fraise_type == "scheduled":
                # Folded 06 (#240): for type:scheduled fraises the synthesised
                # `<project>_<fraise>_<env>.service` is a phantom — no such
                # unit exists. Only emit the real per-job entries below.
                for job in (env_config.get("jobs") or {}).values():
                    for field in ("systemd_service", "systemd_timer"):
                        unit = job.get(field)
                        if not unit:
                            continue
                        validate_service_name(unit)
                        services.append(unit)
            else:
                base = _resolve_service_base(
                    project_name, fraise_name, env_name, env_config
                )
                services.append(f"{base}.service")
    return services


def _collect_deploy_users(
    config: FraisierConfig, fraises_list: list[dict[str, Any]]
) -> list[str]:
    """Collect unique deploy users from all environments.

    Returns a list of unique deploy usernames, preserving order of first appearance.
    """
    users: dict[str, None] = {}
    for fraise in fraises_list:
        for env_config in fraise.get("environments", {}).values():
            user = env_config.get("deploy_user", config.scaffold.deploy_user)
            users[user] = None
    return list(users.keys())


def _any_fraise_has_database(fraises_list: list[dict[str, Any]]) -> bool:
    """Return True if any fraise environment has a database section."""
    for fraise in fraises_list:
        for env_config in fraise.get("environments", {}).values():
            if isinstance(env_config, dict) and env_config.get("database"):
                return True
    return False


def _assert_absolute_cmnd(abs_cmd: str, *, fraise_name: str, env_name: str) -> None:
    """Fail before emitting a sudoers rule the parser will reject (#287).

    sudoers requires a fully-qualified path in the ``Cmnd`` position. Writing a
    bare token there makes ``visudo`` reject the entire fragment, which aborts
    ``scaffold-install`` — taking down the systemd units, the per-fraise
    install-helper socket, nginx and the PostgreSQL config with it. Failing here
    costs one clear message instead.
    """
    token = abs_cmd.split(None, 1)[0]
    if token.startswith("/"):
        return
    searched = ", ".join(_COMMAND_SEARCH_DIRS)
    raise ValidationError(
        f"install.command[0] must resolve to an absolute path so the sudoers "
        f"rule is valid: got {token!r} for fraise {fraise_name!r} "
        f"({env_name}), which was not found on PATH or in {searched}. "
        f"Set an absolute path in fraises.yaml, e.g. '/usr/bin/{token}'."
    )


def _collect_deduplicated_sudoers_rules(
    config: FraisierConfig, fraises_list: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Collect and deduplicate sudoers rules across all environments.

    Returns list of unique rules, each with:
    - from_user: user who runs the command
    - as_user: user the command runs as
    - cmd: absolute path command
    - environments: list of environments using this rule
    - description: human-readable description
    """
    rules_dict: dict[tuple[str, str, str], dict[str, Any]] = {}

    for fraise in fraises_list:
        # install config may be at fraise level or env level; env level takes precedence
        fraise_install = fraise.get("install") or {}
        for env_name, env_config in fraise.get("environments", {}).items():
            if not isinstance(env_config, dict):
                continue

            deploy_user = env_config.get("deploy_user", config.scaffold.deploy_user)
            install = env_config.get("install") or fraise_install

            if isinstance(install, dict):
                install_user = install.get("user")
                install_cmd = install.get("command", [])

                if install_user and install_cmd:
                    # Resolve command to absolute path
                    cmd_str = " ".join(install_cmd)
                    abs_cmd = _resolve_command_path(cmd_str)
                    _assert_absolute_cmnd(
                        abs_cmd, fraise_name=fraise["name"], env_name=env_name
                    )

                    rule_key = (deploy_user, install_user, abs_cmd)
                    if rule_key not in rules_dict:
                        rules_dict[rule_key] = {
                            "from_user": deploy_user,
                            "as_user": install_user,
                            "cmd": abs_cmd,
                            "environments": [],
                            "description": "Dependency install",
                        }
                    rules_dict[rule_key]["environments"].append(env_name)

    return list(rules_dict.values())


def _build_context(config: FraisierConfig, server: str | None = None) -> dict[str, Any]:
    """Build the Jinja2 template context from config."""
    project_name = _infer_project_name(config)
    fraises_list = []
    for name in config.list_fraises():
        fraise = config.get_fraise(name)
        if fraise:
            entry = {"name": name, **fraise}
            entry["port"] = _resolve_fraise_port(entry)
            # Resolve server_name from routing config if present
            entry.setdefault("server_name", None)
            entry.setdefault("location", None)
            # Aggregate server_name from per-env nginx when not set at fraise level.
            # Needed so gateway.conf.j2 `has_server_names` is True and the catch-all
            # SSL block (server_name _) is suppressed.  First env wins — this is only
            # used for the catch-all gate, not for routing.
            if entry["server_name"] is None:
                _raw_envs = entry.get("environments") or {}
                if isinstance(_raw_envs, dict):
                    for _ec in _raw_envs.values():
                        if isinstance(_ec, dict) and isinstance(_ec.get("nginx"), dict):
                            _sn = _ec["nginx"].get("server_name")
                            if _sn:
                                entry["server_name"] = _sn
                                break
            # Enrich each env_config with the precomputed service_base so
            # templates can use it directly without duplicating the resolution logic.
            enriched = {}
            environments = entry.get("environments")
            env_dict: dict[str, Any] = (
                environments if isinstance(environments, dict) else {}
            )
            for env_name, env_config in env_dict.items():
                ec = dict(env_config or {})
                ec["service_base"] = _resolve_service_base(
                    project_name, name, env_name, ec
                )
                enriched[env_name] = ec
            if enriched:
                entry["environments"] = enriched
            fraises_list.append(entry)

    # Build local_fraises: filtered to only environments on the given server
    if server is not None:
        allowed_envs = set(config.get_environments_for_server(server))
        local_fraises = [
            {
                **f,
                "environments": {
                    k: v
                    for k, v in f.get("environments", {}).items()
                    if k in allowed_envs
                },
            }
            for f in fraises_list
        ]
        # Re-derive server_name from server-local environments only.
        # The fraises_list aggregation above used all environments (first wins),
        # which gives the wrong server_name when environments on different servers
        # have distinct nginx.server_name values.
        for lf in local_fraises:
            local_sn: str | None = None
            for ec in lf.get("environments", {}).values():
                if isinstance(ec, dict) and isinstance(ec.get("nginx"), dict):
                    sn = ec["nginx"].get("server_name")
                    if sn:
                        local_sn = sn
                        break
            lf["server_name"] = local_sn
    else:
        local_fraises = fraises_list

    # Build machine_env_map: filter to only that server's machines when --server given
    full_machine_env_map = config.get_machine_environment_map()
    full_machine_webhook_map = webhook_machine_map(config)
    if server is not None:
        # Get machines for the specified server
        machines_for_server = config.servers.get(server, [])
        machine_env_map = {
            m: full_machine_env_map[m]
            for m in machines_for_server
            if m in full_machine_env_map
        }
        machine_webhook_map = {
            m: full_machine_webhook_map[m]
            for m in machines_for_server
            if m in full_machine_webhook_map
        }
    else:
        machine_env_map = full_machine_env_map
        machine_webhook_map = full_machine_webhook_map

    deploy_user = config.scaffold.deploy_user
    install_helper_sockets = _collect_install_helper_sockets(
        project_name, local_fraises, deploy_user
    )

    gateway_fraise = _resolve_gateway_fraise(
        config.scaffold.nginx.gateway_fraise,
        local_fraises,
        has_restricted_paths=bool(config.scaffold.nginx.restricted_paths),
    )

    # Detect whether per-environment nginx configs exist.  When they do,
    # each gateway_env.conf is a self-contained virtual host and gateway.conf
    # should only contain shared directives (limit_req_zone, HTTP catch-all)
    # so it is safe to install unconditionally on every machine (#197).
    has_per_env_nginx = any(
        isinstance((env_cfg or {}), dict) and isinstance(env_cfg.get("nginx"), dict)
        for f in local_fraises
        for env_cfg in (f.get("environments") or {}).values()
    )

    return {
        "manifest": build_manifest(config),
        "scaffold": config.scaffold,
        "deployment": config.deployment,
        "health": config.health,
        "webhook": config.webhook,
        "fraises": fraises_list,
        "local_fraises": local_fraises,
        "fraise_names": config.list_fraises(),
        "project_name": project_name,
        "multi_fraise": len(config.list_fraises()) > 1,
        "has_database": _any_fraise_has_database(local_fraises),
        "allowed_services": _collect_allowed_services(project_name, local_fraises),
        "deploy_users": _collect_deploy_users(config, fraises_list),
        "sudoers_rules": _collect_deduplicated_sudoers_rules(config, fraises_list),
        "machine_env_map": machine_env_map,
        "machine_webhook_map": machine_webhook_map,
        "install_helper_sockets": install_helper_sockets,
        "gateway_fraise": gateway_fraise,
        "has_per_env_nginx": has_per_env_nginx,
    }


def _resolve_gateway_fraise(
    explicit: str | None,
    local_fraises: list[dict[str, Any]],
    has_restricted_paths: bool,
) -> str | None:
    """Return the fraise name that the nginx gateway proxies restricted paths to.

    If ``explicit`` is set, it is used directly.  With exactly one API fraise
    the name is inferred automatically.  Multiple API fraises without an explicit
    value is an error only when ``restricted_paths`` are configured, since that
    is the only place ``gateway_fraise`` is referenced in the template.
    """
    if explicit:
        return explicit
    api_names = [f["name"] for f in local_fraises if f.get("type") == "api"]
    if len(api_names) == 1:
        return api_names[0]
    if len(api_names) > 1 and has_restricted_paths:
        raise ValidationError(
            "scaffold.nginx.gateway_fraise must be set when multiple API fraises "
            f"share a server (found: {', '.join(api_names)})"
        )
    return None


def _infer_project_name(config: FraisierConfig) -> str:
    """Return the project name from config (used for naming prefixes)."""
    return config.project_name


def _collect_unique_servers(config: FraisierConfig) -> list[str]:
    """Return the logical servers any environment declares, in declaration order.

    Delegates to :meth:`FraisierConfig.declared_servers` rather than walking
    the config itself: this function used to read only the global
    ``environments:`` section while the installer's host gating read the
    per-fraise configs too, so the two disagreed for any config that declared
    ``server:`` only under ``fraises.*`` (#325, claim 4).
    """
    return config.declared_servers()


def _server_slug(server: str) -> str:
    """Convert a server identifier to a safe filename component.

    Example: ``prod.myserver.com`` → ``prod-myserver-com``
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", server).lower().strip("-")
    return slug


def webhook_unit_name(project_name: str, server_slug: str | None = None) -> str:
    """The one place a webhook unit filename is built.

    With a slug this is a *source* filename inside the scaffold tree, one per
    logical server. Without one it is either the single-host source or — on
    every host, in every mode — the **destination** under
    ``/etc/systemd/system/``. The destination name never carries the host:
    only the source does, so the fix needs no unit rename, no
    ``systemctl disable``/``enable`` dance and no ``[Install]`` change.

    Nothing else may build this string. Three call sites each built their own
    copy of the unslugged name and all three asked for a file the deploy-path
    render never wrote (#325).
    """
    if server_slug:
        return f"fraisier-{project_name}-webhook-{server_slug}.service"
    return f"fraisier-{project_name}-webhook.service"


def _unknown_server_message(server: str, known: list[str]) -> str:
    """Diagnostic for a ``--server`` value no environment declares."""
    if known:
        return (
            f"No environment declares server {server!r}. Known servers: "
            f"{', '.join(known)}. Rendering it anyway would emit a webhook "
            f"unit with no application paths, which installs cleanly and then "
            f"fails every deploy on a read-only filesystem."
        )
    return (
        f"--server {server!r} was given but no environment declares a "
        f"'server:' field, so there is nothing to filter by. Remove --server, "
        f"or add 'server: {server}' to the environments hosted there."
    )


def webhook_machine_map(config: FraisierConfig) -> dict[str, str]:
    """Map each machine hostname to the webhook unit filename it installs.

    The inversion of ``servers:``. Empty in single-host mode — no environment
    declares a ``server:``, so no slugged unit exists and there is nothing to
    select between.

    ``validate_servers`` already rejects a machine listed under two logical
    servers, so the inversion is unambiguous; this does not re-validate it.
    """
    if not config.declared_servers():
        return {}
    project = config.project_name
    return {
        machine: webhook_unit_name(project, _server_slug(logical))
        for logical, machines in config.servers.items()
        for machine in machines
    }


def webhook_source_for_server(config: FraisierConfig, server: str) -> str:
    """Return the webhook unit filename rendered for logical *server*."""
    known = config.declared_servers()
    if server not in known:
        raise ValidationError(_unknown_server_message(server, known))
    return webhook_unit_name(config.project_name, _server_slug(server))


def _is_under_any(path: str, allowed: list[str]) -> bool:
    """Return True when *path* is one of *allowed* or nested inside one.

    Prefix containment on path components, matching the comparison the #317
    doctor check uses — ``ReadWritePaths=/var/www`` does grant
    ``/var/www/api``, and ``/var/wwwroot`` is not a match for ``/var/www``.
    """
    return any(path == a or path.startswith(a.rstrip("/") + "/") for a in allowed)


def local_hostnames() -> list[str]:
    """Candidate names for this machine, longest-lived first, deduplicated."""
    names = [socket.gethostname(), socket.getfqdn()]
    names += [n.split(".", 1)[0] for n in names if n]
    return list(dict.fromkeys(n for n in names if n))


def resolve_local_server(config: FraisierConfig) -> str | None:
    """Return the logical server this machine belongs to, or None.

    Resolution order mirrors the generated ``install.sh``: the machine
    hostname is looked up in the inversion of ``servers:`` first, then — for
    configs that name their logical servers after the machines they run on and
    carry no ``servers:`` section — against the declared server names
    directly, the way ``ServerSetup`` has always read them.

    None means "cannot tell", never "everywhere": callers decide whether that
    is fatal (installing) or merely unfiltered (rendering).
    """
    known = config.declared_servers()
    if not known:
        return None

    machine_to_server = {
        machine: logical
        for logical, machines in config.servers.items()
        for machine in machines
    }
    hostnames = local_hostnames()
    for candidate in hostnames:
        if candidate in machine_to_server:
            return machine_to_server[candidate]
    for candidate in hostnames:
        if candidate in known:
            return candidate
    return None


def local_webhook_source(config: FraisierConfig, server: str | None = None) -> str:
    """Return the webhook unit filename *this machine* must install.

    One resolver for every installer. The generated ``install.sh`` selects
    from the same map keyed on ``hostname -s``; ``get_install_mapping`` and
    ``ServerSetup._plan_webhook_service`` call this. They therefore cannot
    pick different files for the same box, which is what let ``scaffold-diff``
    report a phantom missing unit while the shell installer silently skipped
    the copy (#325).

    Raises rather than guessing. In multi-host mode there is no safe default:
    installing another host's unit is precisely the failure being closed, and
    reaching for the unslugged name is invariant (N)'s forbidden fallback.
    """
    if not config.declared_servers():
        return webhook_unit_name(config.project_name)

    resolved = server if server is not None else resolve_local_server(config)
    if resolved is None:
        raise ValidationError(
            f"Cannot tell which webhook unit this machine installs: none of "
            f"{', '.join(local_hostnames())} is registered under 'servers:' "
            f"({', '.join(sorted(webhook_machine_map(config))) or 'no machines listed'}"
            f") or named as a logical server "
            f"({', '.join(config.declared_servers())}). Add this machine under "
            f"'servers:', or pass --server explicitly."
        )
    return webhook_source_for_server(config, resolved)


class ScaffoldRenderer:
    """Renders Jinja2 templates using fraises.yaml context."""

    def __init__(self, config: FraisierConfig, server: str | None = None):
        self.config = config
        self.server = server
        self.output_dir = Path(config.scaffold.output_dir)

        loaders: list[jinja2.BaseLoader] = []
        template_dir = config.scaffold.template_dir
        if template_dir:
            custom_path = Path(template_dir)
            if not custom_path.is_absolute():
                custom_path = Path(config.config_path).parent / custom_path
            if not custom_path.is_dir():
                # ChoiceLoader falls through to the built-ins with no error, so
                # a customised template silently does not apply — on the server
                # a relative template_dir resolves against /opt/fraisier, where
                # nothing puts it. Warn rather than raise: existing deploys are
                # already running on built-ins and must not start failing (#312).
                logger.warning(
                    "scaffold.template_dir is set but %s does not exist — "
                    "rendering with built-in templates only. Any customised "
                    "template in %r is being ignored.",
                    custom_path,
                    template_dir,
                )
            loaders.append(jinja2.FileSystemLoader(str(custom_path)))
        loaders.append(jinja2.FileSystemLoader(str(_TEMPLATES_DIR)))

        self.env = jinja2.Environment(
            loader=jinja2.ChoiceLoader(loaders),
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.globals["deploy_socket_name"] = deploy_socket_name  # ty: ignore[invalid-assignment]
        self.context = _build_context(config, server)

    def get_core_template_paths(self) -> list[str]:
        """Return output file paths for core templates."""
        return [t.replace(".j2", "").replace("core/", "") for t in _CORE_TEMPLATES]

    def get_provider_template_paths(self) -> list[str]:
        """Return output file paths for provider templates."""
        return [
            t.replace(".j2", "").replace("provider/", "") for t in _PROVIDER_TEMPLATES
        ]

    def get_install_mapping(self) -> dict[str, Path]:
        """Map scaffold output paths to system install paths.

        Returns:
            Dict mapping relative scaffold paths to absolute system paths.
        """
        mapping: dict[str, Path] = {}

        project_name = self.context["project_name"]

        # Systemd units
        for fraise in self.context["fraises"]:
            for env_name, env_config in fraise.get("environments", {}).items():
                # Deploy socket and service
                socket_unit = deploy_socket_name(env_config, env_name, fraise["name"])
                socket_stem = socket_unit.removesuffix(".socket")
                service_unit = f"{socket_stem}@.service"

                mapping[f"systemd/{socket_unit}"] = Path(
                    f"/etc/systemd/system/{socket_unit}"
                )
                mapping[f"systemd/{service_unit}"] = Path(
                    f"/etc/systemd/system/{service_unit}"
                )

                # Service unit (if exists)
                if "service_base" in env_config:
                    svc = env_config["service_base"]
                    mapping[f"systemd/{svc}.service"] = Path(
                        f"/etc/systemd/system/{svc}.service"
                    )

        # Webhook service unit: the source carries the host, the destination
        # never does. Resolved through the same helper install.sh selects with,
        # so scaffold-diff compares the file this box actually installs instead
        # of reporting a phantom missing one (#325).
        webhook_src = self._local_webhook_source()
        if webhook_src is not None:
            mapping[webhook_src] = Path(
                f"/etc/systemd/system/{webhook_unit_name(project_name)}"
            )

        # Systemctl helper service + socket
        if self.context["allowed_services"]:
            helper_svc = f"fraisier-{project_name}-systemctl-helper.service"
            helper_sock = f"fraisier-{project_name}-systemctl-helper.socket"
            mapping[f"systemd/{helper_svc}"] = Path(f"/etc/systemd/system/{helper_svc}")
            mapping[f"systemd/{helper_sock}"] = Path(
                f"/etc/systemd/system/{helper_sock}"
            )

        # Install helper socket + service units
        for entry in self.context["install_helper_sockets"]:
            mapping[f"systemd/{entry['socket_unit']}"] = Path(
                f"/etc/systemd/system/{entry['socket_unit']}"
            )
            mapping[f"systemd/{entry['service_unit']}"] = Path(
                f"/etc/systemd/system/{entry['service_unit']}"
            )

        # Standard systemd units
        for unit in [
            "deploy-checker.timer",
            "backup.timer",
            "poll-deploy.service",
        ]:
            mapping[f"systemd/{unit}"] = Path(f"/etc/systemd/system/{unit}")

        # Restore-staging units (only if there are restore_migrate fraises)
        if self._has_restore_migrate_fraise():
            for unit in ["restore-staging.service", "restore-staging.timer"]:
                mapping[f"systemd/{unit}"] = Path(f"/etc/systemd/system/{unit}")

        # Nginx config
        mapping["nginx/gateway.conf"] = Path(
            f"/etc/nginx/sites-available/{project_name}"
        )

        # Sudoers
        mapping["sudoers"] = Path(f"/etc/sudoers.d/{project_name}")

        return mapping

    def _local_webhook_source(self) -> str | None:
        """The webhook unit this box installs, or None when it cannot be told.

        ``scaffold-diff`` is a diagnostic that also runs off-server, where no
        local hostname matches any machine in ``servers:``. Omitting the entry
        there is deliberate: a diff cannot say whether the installed unit is
        the right one, and naming an arbitrary host's unit would answer a
        question nobody asked. Reporting nothing beats reporting a phantom.
        """
        try:
            return local_webhook_source(self.config, self.server)
        except ValidationError:
            return None

    def _validate_names(self) -> None:
        """Validate fraise and environment names before rendering.

        Raises:
            ValueError: If any name contains unsafe characters.
        """
        for fraise in self.context["fraises"]:
            name = fraise["name"]
            if not _SAFE_NAME_RE.match(name):
                msg = f"Invalid fraise name: {name!r} — must match [a-zA-Z0-9_-]+"
                raise ValueError(msg)
            for env_name in fraise.get("environments", {}):
                if not _SAFE_NAME_RE.match(env_name):
                    msg = (
                        f"Invalid environment name: {env_name!r}"
                        " — must match [a-zA-Z0-9_-]+"
                    )
                    raise ValueError(msg)

    def render(self, dry_run: bool = False) -> list[str]:
        """Render all templates.

        Args:
            dry_run: If True, return paths without writing files.

        Returns:
            List of output file paths (relative to output_dir).

        Raises:
            ValueError: If a fraise or environment name contains unsafe characters.
        """
        self._validate_names()

        rendered_files: list[str] = []

        # Stage 1: Core templates
        for template_path in _CORE_TEMPLATES:
            out_name = template_path.replace(".j2", "").replace("core/", "")
            rendered_files.append(out_name)
            if not dry_run:
                self._render_template(template_path, out_name)

        # Stage 2: Provider-specific templates
        for template_path in _PROVIDER_TEMPLATES:
            out_name = template_path.replace(".j2", "").replace("provider/", "")
            rendered_files.append(out_name)
            if not dry_run:
                self._render_template(template_path, out_name)

        # systemd service wrapper (always when there are services)
        if self.context["allowed_services"]:
            systemctl_out = "systemctl-wrapper.sh"
            rendered_files.append(systemctl_out)
            if not dry_run:
                self._render_template("core/systemctl-wrapper.sh.j2", systemctl_out)

        # Systemctl helper service + socket (always when there are services)
        if self.context["allowed_services"]:
            rendered_files.extend(self._render_systemctl_helper(dry_run))

        # Scaffold-install-helper: root-privileged socket service (always generated)
        rendered_files.extend(self._render_scaffold_install_helper(dry_run))

        # Install helper units: socket+service per fraise+env with separate install user
        rendered_files.extend(self._render_install_helper_units(dry_run))

        # Unit-installer helper (#240 Phase 5): per-env socket+service when at
        # least one type:scheduled fraise is declared.
        rendered_files.extend(self._render_unit_installer_helper_units(dry_run))

        # Webhook service(s) — rendered dynamically to include project name
        rendered_files.extend(self._render_webhook_services(dry_run))

        # Socket-activated deploy units — per project-environment
        rendered_files.extend(self._render_deploy_socket_services(dry_run))

        # PostgreSQL logging config (one per unique environment with a database)
        rendered_files.extend(self._collect_pg_logging(dry_run))

        # Remove stale deploy socket/service files left by previous scaffold runs
        if not dry_run:
            self._remove_stale_deploy_units(rendered_files)
            self._remove_stale_webhook_units(rendered_files)

        # Per-fraise service templates (systemd or rc.d based on service_manager)
        service_manager = self.config._config.get("service_manager", "systemd")
        project = self.context["project_name"]
        for fraise in self.context["local_fraises"]:
            name = fraise["name"]
            for env_name, env_config in fraise.get("environments", {}).items():
                base = _resolve_service_base(project, name, env_name, env_config or {})
                if service_manager == "rc":
                    svc_name = f"rc.d/{base}"
                    rendered_files.append(svc_name)
                    if not dry_run:
                        self._render_rcd_service(fraise, env_name, svc_name)
                else:
                    svc_name = f"systemd/{base}.service"
                    rendered_files.append(svc_name)
                    if not dry_run:
                        self._render_systemd_service(fraise, env_name, svc_name)

        # Nginx: shared gateway.conf (always generated)
        nginx_out = "nginx/gateway.conf"
        rendered_files.append(nginx_out)
        if not dry_run:
            self._render_template("core/gateway.conf.j2", nginx_out)

        # Nginx: per-environment configs (only when nginx: key is present)
        rendered_files.extend(self._collect_per_env_nginx(dry_run))

        # Systemd timer and backup service templates
        project_name = self.context.get("project_name", "fraisier")
        for timer_tpl, timer_out in [
            ("core/deploy-checker.timer.j2", "systemd/deploy-checker.timer"),
            ("core/backup.timer.j2", "systemd/backup.timer"),
            ("core/backup.service.j2", "systemd/backup.service"),
            (
                "core/backup-alert@.service.j2",
                f"systemd/fraisier-{project_name}-backup-alert@.service",
            ),
        ]:
            rendered_files.append(timer_out)
            if not dry_run:
                self._render_template(timer_tpl, timer_out)

        # Restore-staging only if there are fraises with restore_migrate strategy
        if self._has_restore_migrate_fraise():
            for timer_tpl, timer_out in [
                ("core/restore-staging.timer.j2", "systemd/restore-staging.timer"),
                ("core/restore-staging.service.j2", "systemd/restore-staging.service"),
            ]:
                rendered_files.append(timer_out)
                if not dry_run:
                    self._render_template(timer_tpl, timer_out)

        return rendered_files

    def _has_restore_migrate_fraise(self) -> bool:
        """Check if any fraise in local_fraises has restore_migrate strategy.

        This determines whether to render the restore-staging service/timer units.
        """
        for fraise in self.context.get("local_fraises", []):
            for env_config in fraise.get("environments", {}).values():
                db_cfg = env_config.get("database", {})
                if db_cfg.get("strategy") == "restore_migrate":
                    return True
        return False

    def _render_install_helper_units(self, dry_run: bool) -> list[str]:
        """Render install-helper .socket and .service units for each fraise+env."""
        rendered: list[str] = []
        for entry in self.context["install_helper_sockets"]:
            socket_rel = f"systemd/{entry['socket_unit']}"
            service_rel = f"systemd/{entry['service_unit']}"
            rendered.extend([socket_rel, service_rel])
            if not dry_run:
                unit_context = {
                    **self.context,
                    "fraise_name": entry["fraise_name"],
                    "env_name": entry["env_name"],
                    "install_user": entry["install_user"],
                    "app_path": entry["app_path"],
                    "install_command": entry.get("install_command", []),
                }
                try:
                    tpl = self.env.get_template("core/install-helper.socket.j2")
                    self._write_output(socket_rel, tpl.render(**unit_context))
                except jinja2.TemplateNotFound:
                    self._write_output(
                        socket_rel, "# Placeholder: install-helper.socket.j2\n"
                    )

                try:
                    tpl = self.env.get_template("core/install-helper.service.j2")
                    self._write_output(service_rel, tpl.render(**unit_context))
                except jinja2.TemplateNotFound:
                    self._write_output(
                        service_rel, "# Placeholder: install-helper.service.j2\n"
                    )
        return rendered

    def _render_unit_installer_helper_units(self, dry_run: bool) -> list[str]:
        """Render the unit-installer helper for each env with type:scheduled fraises.

        Phase 0 decision #2 (#240): one helper per (project, env). Allowlist
        baked at render time as ``--allow <src_prefix>:<dest_prefix>`` pairs in
        ExecStart, with ``src_prefix = env.app_path / scripts/systemd/`` and
        ``dest_prefix = /etc/systemd/system/``.
        """
        envs = _collect_unit_installer_envs(self.context["local_fraises"])
        if not envs:
            return []

        rendered: list[str] = []
        project = self.context["project_name"]
        for env_name, allow_pairs in envs.items():
            service_out = (
                f"systemd/fraisier-{project}-{env_name}-unit-installer.service"
            )
            socket_out = f"systemd/fraisier-{project}-{env_name}-unit-installer.socket"
            rendered.extend([service_out, socket_out])
            if not dry_run:
                self.context["env_name"] = env_name
                self.context["unit_installer_allow_pairs"] = allow_pairs
                try:
                    self._render_template("core/unit-installer.service.j2", service_out)
                    self._render_template("core/unit-installer.socket.j2", socket_out)
                finally:
                    del self.context["env_name"]
                    del self.context["unit_installer_allow_pairs"]
        return rendered

    def _render_systemctl_helper(self, dry_run: bool) -> list[str]:
        """Render the systemctl helper .service and .socket units."""
        project = self.context["project_name"]
        service_out = f"systemd/fraisier-{project}-systemctl-helper.service"
        socket_out = f"systemd/fraisier-{project}-systemctl-helper.socket"

        if not dry_run:
            self._render_template("core/systemctl-helper.service.j2", service_out)
            self._render_template("core/systemctl-helper.socket.j2", socket_out)

        return [service_out, socket_out]

    def _render_scaffold_install_helper(self, dry_run: bool) -> list[str]:
        """Render the scaffold-install-helper .service and .socket units."""
        project = self.context["project_name"]
        service_out = f"systemd/fraisier-{project}-scaffold-install-helper.service"
        socket_out = f"systemd/fraisier-{project}-scaffold-install-helper.socket"

        if not dry_run:
            # _render_template() does template.render(**self.context) with no
            # extra_context support.  Inject scaffold_install_script temporarily.
            # The helper's allowed_script is the install.sh in the single
            # server-side scaffold state tree (#283), NOT app_path / /opt/{project}.
            self.context["scaffold_install_script"] = (
                f"{self.config.scaffold_state_dir}/install.sh"
            )
            try:
                self._render_template(
                    "core/scaffold-install-helper.service.j2", service_out
                )
            finally:
                del self.context["scaffold_install_script"]
            self._render_template("core/scaffold-install-helper.socket.j2", socket_out)

        return [service_out, socket_out]

    def _render_webhook_services(self, dry_run: bool) -> list[str]:
        """Render the webhook service unit(s), addressed by host.

        The rule, stated once:

            When any environment declares a ``server:``, the tree contains
            **only** slugged ``fraisier-{project}-webhook-{slug}.service``
            files and the installer resolves the slug from the machine
            hostname. When no environment declares a ``server:``, the tree
            contains the single unslugged file. There is no fallback from the
            first mode to the second.

        Mode is a function of the config alone (invariant **M**). ``--server``
        narrows *which* slugged units this render emits; it never flips the
        mode, so a multi-host config can never produce the host-agnostic
        filename whose content depends on whoever rendered it last — the
        property that produced #325.

        Raises:
            ValidationError: ``--server`` names a server no environment
                declares. Rendering it anyway yields a valid-looking unit with
                no application paths (the old behaviour), which installs
                cleanly and then fails every deploy.
            ValueError: an environment resolves to no host, or a rendered unit
                omits a tree of an environment its host runs (invariant **C**).
        """
        servers = _collect_unique_servers(self.config)

        if not servers:
            if self.server is not None:
                raise ValidationError(_unknown_server_message(self.server, servers))
            # Single-host mode: "every environment" and "this host's
            # environments" are the same set, so there is nothing to leak.
            out_name = webhook_unit_name(self.context["project_name"])
            if not dry_run:
                self._render_template("core/fraisier-webhook.service.j2", out_name)
                self._assert_hosted_trees_are_writable(
                    None, (self.output_dir / out_name).read_text()
                )
            return [out_name]

        self._assert_every_environment_has_a_host(servers)

        if self.server is not None:
            if self.server not in servers:
                raise ValidationError(_unknown_server_message(self.server, servers))
            targets = [self.server]
        else:
            targets = servers

        rendered: list[str] = []
        for server in targets:
            out_name = webhook_unit_name(
                self.context["project_name"], _server_slug(server)
            )
            if not dry_run:
                server_context = _build_context(self.config, server)
                try:
                    template = self.env.get_template("core/fraisier-webhook.service.j2")
                    content = template.render(**server_context)
                except jinja2.TemplateNotFound:
                    content = "# Placeholder: fraisier-webhook.service.j2\n"
                self._write_output(out_name, content)
                self._assert_hosted_trees_are_writable(server, content)
            rendered.append(out_name)
        return rendered

    def _assert_every_environment_has_a_host(self, servers: list[str]) -> None:
        """Invariant (C), first half: no environment may resolve to zero hosts.

        ``get_environments_for_server`` matches on exact equality, so in a
        multi-host config an environment that declares no ``server:`` belongs
        to no logical server and its ``git_repo``/``app_path`` are rendered
        into **no** webhook unit at all — the #325 failure reached from a
        config that reads like a partial migration rather than a mistake.

        Rejected rather than treated as hosted everywhere: "everywhere"
        re-creates the #62 least-privilege leak by default and makes the
        permissive reading of a half-migrated config the safe-looking one.
        """
        orphans = self.config.environments_without_a_server()
        if not orphans:
            return
        raise ValueError(
            f"Environment(s) {', '.join(orphans)} declare no 'server:' while "
            f"{', '.join(servers)} do. In a multi-server config an environment "
            f"with no server belongs to no machine, so its git_repo and "
            f"app_path reach no webhook unit's ReadWritePaths= and every "
            f"deploy of it fails on a read-only filesystem. Add "
            f"'server: <one of {', '.join(servers)}>' to each."
        )

    def _assert_hosted_trees_are_writable(
        self, server: str | None, content: str
    ) -> None:
        """Invariant (C), second half: a host's unit carries all its own trees.

        Checked against the *rendered text*, at the point of rendering, so it
        holds independently of how the filtering was reached — a template
        regression, a context bug or a new filtering route all trip it. The
        mirror of #62 (too many paths) is this one's opposite direction (too
        few); one check catches both failures of the same invariant.
        """
        if "ProtectSystem=strict" not in content:
            return

        allowed = [
            ln.split("=", 1)[1].strip()
            for ln in content.splitlines()
            if ln.startswith("ReadWritePaths=")
        ]
        hosted = (
            set(self.config.get_environments_for_server(server))
            if server is not None
            else None
        )

        missing: list[str] = []
        for fraise in self.context["fraises"]:
            for env_name, env_config in fraise.get("environments", {}).items():
                if hosted is not None and env_name not in hosted:
                    continue
                for key in ("git_repo", "app_path"):
                    path = env_config.get(key)
                    if path and not _is_under_any(str(path), allowed):
                        missing.append(f"{fraise['name']}/{env_name} {key}={path}")

        if missing:
            where = f"server {server!r}" if server else "this host"
            raise ValueError(
                f"The webhook unit rendered for {where} is ProtectSystem=strict "
                f"but does not allow writes to: {', '.join(missing)}. Those "
                f"environments are hosted there, so every deploy of them would "
                f"fail on a read-only filesystem. Rendered ReadWritePaths: "
                f"{', '.join(allowed) or '(none)'}."
            )

    def _render_deploy_socket_services(self, dry_run: bool) -> list[str]:
        """Render socket-activated deploy units for each fraise-environment combo."""
        rendered: list[str] = []

        for fraise in self.context["local_fraises"]:
            fraise_name = fraise["name"]
            for env_name, env_config in fraise.get("environments", {}).items():
                socket_unit = deploy_socket_name(env_config, env_name, fraise_name)
                socket_stem = socket_unit.removesuffix(".socket")

                socket_rel = f"systemd/{socket_unit}"
                rendered.append(socket_rel)
                if not dry_run:
                    self._render_deploy_socket(fraise_name, env_name, socket_rel)

                # Service unit (template unit: @.service required by Accept=yes)
                service_rel = f"systemd/{socket_stem}@.service"
                rendered.append(service_rel)
                if not dry_run:
                    self._render_deploy_service(fraise_name, env_name, service_rel)

        return rendered

    def _render_deploy_socket(
        self, fraise_name: str, env_name: str, out_name: str
    ) -> None:
        """Render a deploy socket unit."""
        # Get webhook config from fraise environment
        fraise_config = None
        for f in self.context["fraises"]:
            if f["name"] == fraise_name:
                fraise_config = f
                break

        if not fraise_config:
            return

        env_config = fraise_config.get("environments", {}).get(env_name, {})
        socket_unit = deploy_socket_name(env_config, env_name, fraise_name)
        socket_stem = socket_unit.removesuffix(".socket")

        # Update context with environment-specific values
        socket_context = dict(self.context)
        socket_context.update(
            {
                "fraise_name": fraise_name,
                "environment": env_name,
                "socket_unit_name": socket_unit,
                "socket_stem": socket_stem,
            }
        )

        try:
            template = self.env.get_template("core/deploy-socket.j2")
            content = template.render(**socket_context)
        except jinja2.TemplateNotFound:
            content = "# Placeholder: core/deploy-socket.j2\n"

        self._write_output(out_name, content)

    def _render_deploy_service(
        self, fraise_name: str, env_name: str, out_name: str
    ) -> None:
        """Render a deploy service unit."""
        # Get webhook config from fraise environment
        fraise_config = None
        for f in self.context["fraises"]:
            if f["name"] == fraise_name:
                fraise_config = f
                break

        if not fraise_config:
            return

        env_config = fraise_config.get("environments", {}).get(env_name, {})
        socket_unit = deploy_socket_name(env_config, env_name, fraise_name)
        socket_stem = socket_unit.removesuffix(".socket")

        # Update context with environment-specific values
        service_context = dict(self.context)
        service_context.update(
            {
                "fraise_name": fraise_name,
                "environment": env_name,
                "env_config": env_config,
                "socket_unit_name": socket_unit,
                "socket_stem": socket_stem,
            }
        )

        try:
            template = self.env.get_template("core/deploy-service.j2")
            content = template.render(**service_context)
        except jinja2.TemplateNotFound:
            content = "# Placeholder: core/deploy-service.j2\n"

        self._write_output(out_name, content)

    def _render_template(self, template_path: str, out_name: str) -> None:
        """Render a single template to output_dir."""
        try:
            template = self.env.get_template(template_path)
        except jinja2.TemplateNotFound:
            # Template not yet created — write placeholder
            self._write_output(out_name, f"# Placeholder: {template_path}\n")
            return

        content = template.render(**self.context)
        self._write_output(out_name, content)

    def _render_rcd_service(
        self,
        fraise: dict[str, Any],
        env_name: str,
        out_name: str,
    ) -> None:
        """Render a per-fraise rc.d service script."""
        env_config = fraise.get("environments", {}).get(env_name, {})
        service = ServiceConfig.from_env_dict(env_config)

        # Resolve app_path: env_config > fallback /opt/<name>
        app_path = env_config.get("app_path", f"/opt/{fraise['name']}")

        # Resolve exec_command: service.exec > fraise-level > manage.py for Django-like
        exec_command = (
            service.exec or fraise.get("exec_command") or f"{app_path}/manage.py"
        )

        # For rc.d, command is the executable, command_args are the args
        # For API, assume gunicorn or similar
        if fraise.get("type") == "api":
            # Assume gunicorn for API
            command = "gunicorn"  # or whatever
            command_args = f"--chdir {app_path} myapp.wsgi:application"  # placeholder
        else:
            command = exec_command
            command_args = ""

        # PID file
        pidfile = f"/var/run/{fraise['name']}/{env_name}.pid"

        # Environment variables
        env_vars = env_config.get("env", {})

        # Service name
        project = self.context["project_name"]
        service_name = _resolve_service_base(
            project, fraise["name"], env_name, env_config or {}
        )

        context = {
            "service_name": service_name,
            "command": command,
            "command_args": command_args,
            "pidfile": pidfile,
            "env": env_vars,
        }

        try:
            template = self.env.get_template("provider/rc.d.j2")
            content = template.render(**context)
        except jinja2.TemplateNotFound:
            content = "# Placeholder: rc.d.j2\n"
        self._write_output(out_name, content)

    def _render_systemd_service(
        self,
        fraise: dict[str, Any],
        env_name: str,
        out_name: str,
    ) -> None:
        """Render a per-fraise systemd service unit."""
        env_config = fraise.get("environments", {}).get(env_name, {})
        service = ServiceConfig.from_env_dict(env_config)

        # Extract port from health_check.url if available
        hc = env_config.get("health_check", {})
        hc_url = hc.get("url", "") if isinstance(hc, dict) else ""
        hc_port = _extract_port(hc_url) if hc_url else None

        # Port resolution: service.port > health_check URL > 8000
        port = service.port or hc_port or 8000

        # Resolve app_path: env_config > fallback /opt/<name>
        app_path = env_config.get("app_path", f"/opt/{fraise['name']}")

        # Resolve exec_command: service.exec > fraise-level > None (template default)
        # Prepend app_path when the executable is a relative path so systemd
        # gets the absolute path it requires (see #90).
        exec_command = service.exec or fraise.get("exec_command")
        if exec_command and not exec_command.startswith("/"):
            exec_command = f"{app_path}/{exec_command}"

        # Resolve memory_max: service > scaffold default
        memory_max = (
            service.memory_max or self.config.scaffold.systemd.memory_max_default
        )

        # Build resolved security directives for template
        security_directives = {
            SECURITY_DIRECTIVE_MAP[k]: _format_security_value(v)
            for k, v in service.resolved_security.items()
            if k in SECURITY_DIRECTIVE_MAP
        }

        ctx = {
            **self.context,
            "fraise": fraise,
            "env_name": env_name,
            "env_config": env_config,
            "service": service,
            "worker_count": service.workers,
            "memory_max": memory_max,
            "app_path": app_path,
            "port": port,
            "exec_command": exec_command,
            "security_directives": security_directives,
        }
        try:
            template = self.env.get_template("core/service.j2")
            content = template.render(**ctx)
        except jinja2.TemplateNotFound:
            content = f"# Placeholder: core/service.j2 for {fraise['name']}\n"

        self._write_output(out_name, content)

    def _collect_pg_logging(self, dry_run: bool) -> list[str]:
        """Discover and render per-environment PostgreSQL logging configs.

        Returns list of rendered file paths.
        """
        if not self.context["has_database"]:
            return []

        env_names: set[str] = set()
        for fraise in self.context["fraises"]:
            for env_name, env_config in fraise.get("environments", {}).items():
                if isinstance(env_config, dict) and env_config.get("database"):
                    env_names.add(env_name)

        files: list[str] = []
        for env_name in sorted(env_names):
            pg_conf_out = f"postgresql/fraisier_{env_name}.conf"
            files.append(pg_conf_out)
            if not dry_run:
                self._render_pg_logging(env_name, pg_conf_out)
        return files

    def _render_pg_logging(self, env_name: str, out_name: str) -> None:
        """Render a per-environment PostgreSQL logging config."""
        from fraisier.config import PG_LOG_ENV_DEFAULTS

        defaults = PG_LOG_ENV_DEFAULTS.get(env_name, PG_LOG_ENV_DEFAULTS["production"])
        ctx = {
            **self.context,
            "env_name": env_name,
            "pg_defaults": defaults,
        }
        try:
            template = self.env.get_template("core/postgresql-logging.conf.j2")
            content = template.render(**ctx)
        except jinja2.TemplateNotFound:
            content = f"# Placeholder: postgresql logging for {env_name}\n"

        self._write_output(out_name, content)

    def _collect_per_env_nginx(self, dry_run: bool) -> list[str]:
        """Discover and render per-environment nginx configs.

        Always generates configs for ALL servers/environments so that
        ``scripts/generated/nginx/`` is a complete, committable artifact.
        Each server's ``install.sh`` uses ``_env_active()`` to install only
        the configs that belong to its own environments (#148).

        Returns list of rendered file paths.
        """
        files: list[str] = []
        project = self.context["project_name"]
        for fraise in self.context["fraises"]:
            name = fraise["name"]
            for env_name, env_config in fraise.get("environments", {}).items():
                if not isinstance(env_config, dict):
                    continue
                nginx_config = NginxEnvConfig.from_env_dict(env_config)
                if nginx_config is None:
                    continue
                nginx_stem = nginx_config.server_name or f"{project}_{name}_{env_name}"
                out_name = f"nginx/{nginx_stem}.conf"
                files.append(out_name)
                if not dry_run:
                    self._render_nginx_env(
                        fraise, env_name, env_config, nginx_config, out_name
                    )
        return files

    def _render_nginx_env(
        self,
        fraise: dict[str, Any],
        env_name: str,
        env_config: dict[str, Any],
        nginx_config: NginxEnvConfig,
        out_name: str,
    ) -> None:
        """Render a per-environment nginx config file."""
        service = ServiceConfig.from_env_dict(env_config)

        # Resolve port: service.port > health_check URL > 8000
        hc = env_config.get("health_check", {})
        hc_url = hc.get("url", "") if isinstance(hc, dict) else ""
        hc_port = _extract_port(hc_url) if hc_url else None
        port = service.port or hc_port or 8000

        socket_unit = deploy_socket_name(env_config, env_name, fraise["name"])
        socket_stem = socket_unit.removesuffix(".socket")

        ctx = {
            **self.context,
            "fraise": fraise,
            "env_name": env_name,
            "nginx_config": nginx_config,
            "port": port,
            "socket_stem": socket_stem,
        }
        try:
            template = self.env.get_template("core/gateway_env.conf.j2")
            content = template.render(**ctx)
        except jinja2.TemplateNotFound:
            content = (
                f"# Placeholder: core/gateway_env.conf.j2"
                f" for {fraise['name']} ({env_name})\n"
            )

        self._write_output(out_name, content)

    def _remove_stale_deploy_units(self, rendered_files: list[str]) -> None:
        """Remove deploy socket/service files not in the current render set.

        Cleans up legacy files left by older scaffold runs (e.g. pre-0.7.1
        generic ``fraisier-{env}.socket`` files replaced by fraise-specific
        ``fraisier-{fraise}-{env}.socket`` names).
        """
        systemd_dir = self.output_dir / "systemd"
        if not systemd_dir.exists():
            return
        rendered_set = set(rendered_files)
        for path in systemd_dir.iterdir():
            if not path.is_file():
                continue
            rel = f"systemd/{path.name}"
            if rel in rendered_set:
                continue
            name = path.name
            if (name.startswith("fraisier-") and name.endswith(".socket")) or (
                name.startswith("fraisier-") and name.endswith("@.service")
            ):
                path.unlink()

    def _remove_stale_webhook_units(self, rendered_files: list[str]) -> None:
        """Delete webhook units in the tree that no host can install (#325).

        A file that nothing writes and nothing deletes is the substrate of the
        bug: the installer used to reach for exactly such a leftover, whose
        content was frozen from whatever server context last wrote it. Both
        directions are swept here — a slugged unit for a server dropped from
        the config, and the legacy unslugged unit that multi-host mode no
        longer produces.

        Only on an unfiltered render. A ``--server`` render holds one host's
        share of the truth; letting it delete the units it was not asked to
        produce would leave the shared state tree valid for one machine and
        broken for the rest, which is the failure mode this whole change
        exists to remove.
        """
        if self.server is not None or not self.output_dir.exists():
            return

        prefix = f"fraisier-{self.context['project_name']}-webhook"
        rendered_set = set(rendered_files)
        for path in self.output_dir.iterdir():
            if not path.is_file():
                continue
            name = path.name
            if not name.startswith(prefix) or not name.endswith(".service"):
                continue
            if name not in rendered_set:
                path.unlink()

    def _write_output(self, rel_path: str, content: str) -> None:
        """Write rendered content to output_dir/rel_path."""
        out = self.output_dir / rel_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
