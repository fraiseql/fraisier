"""Two-stage Jinja2 scaffold renderer.

Stage 1: Core templates (systemd, nginx, sudoers, install, shell scripts)
Stage 2: Provider-specific templates (GitHub Actions, confiture)

Templates are rendered with the full fraises.yaml context and written
to the configured output_dir.
"""

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jinja2

from fraisier.config import (
    SECURITY_DIRECTIVE_MAP,
    FraisierConfig,
    NginxEnvConfig,
    ServiceConfig,
)

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


_ADMIN_STRATEGIES = {"rebuild", "restore_migrate"}

# Mapping of command names to their absolute paths
_COMMAND_PATH_MAP = {
    "uv": "/usr/local/bin/uv",
    "systemctl": "/usr/bin/systemctl",
    "curl": "/usr/bin/curl",
    "tar": "/usr/bin/tar",
    "gunzip": "/usr/bin/gunzip",
    "psql": "/usr/bin/psql",
}


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

    first_word = parts[0]
    absolute = _COMMAND_PATH_MAP.get(first_word, first_word)

    if len(parts) == 1:
        return absolute
    return f"{absolute} {parts[1]}"


def _collect_pg_allowed_databases(fraises_list: list[dict[str, Any]]) -> list[str]:
    """Collect database names that need admin access (rebuild/restore_migrate).

    Returns the allowlist including template-prefixed variants.
    """
    allowed: dict[str, None] = {}
    for fraise in fraises_list:
        for env_config in fraise.get("environments", {}).values():
            db = env_config.get("database") or {}
            if not isinstance(db, dict):
                continue
            strategy = db.get("strategy", "")
            if strategy not in _ADMIN_STRATEGIES:
                continue
            db_name = db.get("name", "")
            if not db_name:
                continue
            allowed[db_name] = None
            # Default template name
            allowed[f"template_{db_name}"] = None
            # Custom template name from restore config
            restore = db.get("restore") or {}
            if isinstance(restore, dict) and restore.get("template_name"):
                allowed[restore["template_name"]] = None
    return list(allowed)


def _resolve_service_base(
    project_name: str,
    fraise_name: str,
    env_name: str,
    env_config: dict[str, Any],
) -> str:
    """Return the systemd service unit base name (without .service suffix).

    Resolution order:
    1. ``systemd_service`` at the environment top level (validated at config load)
    2. ``service.service_name`` (nested under the service: key)
    3. Default: ``{project}_{fraise}_{env}``
    """
    systemd_service = env_config.get("systemd_service")
    if systemd_service:
        base = str(systemd_service)
        return base.removesuffix(".service")

    override = (env_config.get("service") or {}).get("service_name")
    if override:
        return override

    return f"{project_name}_{fraise_name}_{env_name}"


def _collect_allowed_services(
    project_name: str, fraises_list: list[dict[str, Any]]
) -> list[str]:
    """Collect all systemd service names from fraises and environments.

    Returns fully-qualified service names (e.g., 'project_fraise_env.service').
    """
    services = []
    for fraise in fraises_list:
        fraise_name = fraise.get("name", "")
        if not fraise_name:
            continue
        for env_name, env_config in fraise.get("environments", {}).items():
            base = _resolve_service_base(
                project_name, fraise_name, env_name, env_config or {}
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
        for env_name, env_config in fraise.get("environments", {}).items():
            if not isinstance(env_config, dict):
                continue

            deploy_user = env_config.get("deploy_user", config.scaffold.deploy_user)
            install = env_config.get("install") or {}

            if isinstance(install, dict):
                install_user = install.get("user")
                install_cmd = install.get("command", [])

                if install_user and install_cmd:
                    # Resolve command to absolute path
                    cmd_str = " ".join(install_cmd)
                    abs_cmd = _resolve_command_path(cmd_str)

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
            # Enrich each env_config with the precomputed service_base so
            # templates can use it directly without duplicating the resolution logic.
            enriched = {}
            for env_name, env_config in entry.get("environments", {}).items():
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
    else:
        local_fraises = fraises_list

    return {
        "scaffold": config.scaffold,
        "deployment": config.deployment,
        "health": config.health,
        "webhook": config.webhook,
        "fraises": fraises_list,
        "local_fraises": local_fraises,
        "fraise_names": config.list_fraises(),
        "project_name": project_name,
        "multi_fraise": len(config.list_fraises()) > 1,
        "pg_allowed_databases": _collect_pg_allowed_databases(fraises_list),
        "has_database": _any_fraise_has_database(fraises_list),
        "allowed_services": _collect_allowed_services(project_name, fraises_list),
        "deploy_users": _collect_deploy_users(config, fraises_list),
        "sudoers_rules": _collect_deduplicated_sudoers_rules(config, fraises_list),
    }


def _infer_project_name(config: FraisierConfig) -> str:
    """Return the project name from config (used for naming prefixes)."""
    return config.project_name


def _collect_unique_servers(config: FraisierConfig) -> list[str]:
    """Return unique server values from environments config, preserving order."""
    seen: dict[str, None] = {}
    for env_config in config.environments.values():
        if isinstance(env_config, dict):
            server = env_config.get("server")
            if server:
                seen[server] = None
    return list(seen)


def _server_slug(server: str) -> str:
    """Convert a server identifier to a safe filename component.

    Example: ``prod.myserver.com`` → ``prod-myserver-com``
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", server).lower().strip("-")
    return slug


class ScaffoldRenderer:
    """Renders Jinja2 templates using fraises.yaml context."""

    def __init__(self, config: FraisierConfig, server: str | None = None):
        self.config = config
        self.server = server
        self.output_dir = Path(config.scaffold.output_dir)
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
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
            fraise_name = fraise["name"]
            for env_name in fraise.get("environments", {}):
                # Deploy socket and service
                socket_name = (
                    f"fraisier-{project_name}-{fraise_name}-{env_name}-deploy.socket"
                )
                service_name = (
                    f"fraisier-{project_name}-{fraise_name}-{env_name}-deploy@.service"
                )

                mapping[f"systemd/{socket_name}"] = Path(
                    f"/etc/systemd/system/{socket_name}"
                )
                mapping[f"systemd/{service_name}"] = Path(
                    f"/etc/systemd/system/{service_name}"
                )

                # Service unit (if exists)
                env_config = fraise["environments"][env_name]
                if "service_base" in env_config:
                    svc = env_config["service_base"]
                    mapping[f"systemd/{svc}.service"] = Path(
                        f"/etc/systemd/system/{svc}.service"
                    )

        # Standard systemd units
        for unit in ["deploy-checker.timer", "backup.timer", "poll-deploy.service"]:
            if unit in ["deploy-checker.timer", "backup.timer"]:
                mapping[f"systemd/{unit}"] = Path(f"/etc/systemd/system/{unit}")
            else:
                mapping[f"systemd/{unit}"] = Path(f"/etc/systemd/system/{unit}")

        # Nginx config
        mapping["nginx/gateway.conf"] = Path(
            f"/etc/nginx/sites-available/{project_name}"
        )

        # Sudoers
        mapping["sudoers"] = Path(f"/etc/sudoers.d/{project_name}")

        return mapping

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

        # PostgreSQL admin wrapper (only when admin strategies are configured)
        if self.context["pg_allowed_databases"]:
            pg_out = "pg-wrapper.sh"
            rendered_files.append(pg_out)
            if not dry_run:
                self._render_template("core/pg-wrapper.sh.j2", pg_out)

        # systemd service wrapper (always when there are services)
        if self.context["allowed_services"]:
            systemctl_out = "systemctl-wrapper.sh"
            rendered_files.append(systemctl_out)
            if not dry_run:
                self._render_template("core/systemctl-wrapper.sh.j2", systemctl_out)

        # Webhook service(s) — rendered dynamically to include project name
        rendered_files.extend(self._render_webhook_services(dry_run))

        # Socket-activated deploy units — per project-environment
        rendered_files.extend(self._render_deploy_socket_services(dry_run))

        # PostgreSQL logging config (one per unique environment with a database)
        rendered_files.extend(self._collect_pg_logging(dry_run))

        # Per-fraise systemd service templates
        project = self.context["project_name"]
        for fraise in self.context["fraises"]:
            name = fraise["name"]
            for env_name, env_config in fraise.get("environments", {}).items():
                base = _resolve_service_base(project, name, env_name, env_config or {})
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
        for timer_tpl, timer_out in [
            ("core/deploy-checker.timer.j2", "systemd/deploy-checker.timer"),
            ("core/backup.timer.j2", "systemd/backup.timer"),
            ("core/backup.service.j2", "systemd/backup.service"),
        ]:
            rendered_files.append(timer_out)
            if not dry_run:
                self._render_template(timer_tpl, timer_out)

        return rendered_files

    def _webhook_service_name(self, server_slug: str | None = None) -> str:
        """Return the output filename for a webhook service unit.

        With no slug: ``fraisier-{project}-webhook.service`` (single-server).
        With a slug: ``fraisier-{project}-webhook-{slug}.service`` (per-server).
        """
        project = self.context["project_name"]
        if server_slug:
            return f"fraisier-{project}-webhook-{server_slug}.service"
        return f"fraisier-{project}-webhook.service"

    def _render_webhook_services(self, dry_run: bool) -> list[str]:
        """Render webhook service unit(s) with project-specific naming.

        Behaviour:
        - ``--server`` given → one file, context already filtered by server
        - No ``--server``, no ``environments.server`` config → one file, all paths
        - No ``--server``, ``environments.server`` configured → one file per server,
          each with only that server's paths
        """
        servers = _collect_unique_servers(self.config)

        if self.server is not None or not servers:
            # Single file: either explicit server filter or no server config
            out_name = self._webhook_service_name()
            if not dry_run:
                self._render_template("core/fraisier-webhook.service.j2", out_name)
            return [out_name]

        # Auto per-server: one file per unique server
        rendered: list[str] = []
        for server in servers:
            slug = _server_slug(server)
            out_name = self._webhook_service_name(slug)
            if not dry_run:
                server_context = _build_context(self.config, server)
                try:
                    template = self.env.get_template("core/fraisier-webhook.service.j2")
                    content = template.render(**server_context)
                except jinja2.TemplateNotFound:
                    content = "# Placeholder: fraisier-webhook.service.j2\n"
                self._write_output(out_name, content)
            rendered.append(out_name)
        return rendered

    def _render_deploy_socket_services(self, dry_run: bool) -> list[str]:
        """Render socket-activated deploy units for each fraise-environment combo."""
        rendered: list[str] = []
        project = self.context["project_name"]

        for fraise in self.context["fraises"]:
            fraise_name = fraise["name"]
            for env_name in fraise.get("environments", {}):
                # Socket unit — one per fraise+env to avoid filename collisions
                prefix = f"fraisier-{project}-{fraise_name}-{env_name}-deploy"
                socket_name = f"systemd/{prefix}.socket"
                rendered.append(socket_name)
                if not dry_run:
                    self._render_deploy_socket(fraise_name, env_name, socket_name)

                # Service unit (template unit: @.service required by Accept=yes)
                service_name = f"systemd/{prefix}@.service"
                rendered.append(service_name)
                if not dry_run:
                    self._render_deploy_service(fraise_name, env_name, service_name)

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

        # Update context with environment-specific values
        socket_context = dict(self.context)
        socket_context.update(
            {
                "fraise_name": fraise_name,
                "environment": env_name,
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

        # Update context with environment-specific values
        service_context = dict(self.context)
        service_context.update(
            {
                "fraise_name": fraise_name,
                "environment": env_name,
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
                out_name = f"nginx/{project}_{name}_{env_name}.conf"
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

        ctx = {
            **self.context,
            "fraise": fraise,
            "env_name": env_name,
            "nginx_config": nginx_config,
            "port": port,
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

    def _write_output(self, rel_path: str, content: str) -> None:
        """Write rendered content to output_dir/rel_path."""
        out = self.output_dir / rel_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
