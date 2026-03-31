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
    "core/fraisier-webhook.service.j2",
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


def _build_context(config: FraisierConfig) -> dict[str, Any]:
    """Build the Jinja2 template context from config."""
    fraises_list = []
    for name in config.list_fraises():
        fraise = config.get_fraise(name)
        if fraise:
            entry = {"name": name, **fraise}
            entry["port"] = _resolve_fraise_port(entry)
            # Resolve server_name from routing config if present
            entry.setdefault("server_name", None)
            entry.setdefault("location", None)
            fraises_list.append(entry)

    return {
        "scaffold": config.scaffold,
        "deployment": config.deployment,
        "fraises": fraises_list,
        "fraise_names": config.list_fraises(),
        "project_name": _infer_project_name(config),
        "multi_fraise": len(config.list_fraises()) > 1,
        "pg_allowed_databases": _collect_pg_allowed_databases(fraises_list),
    }


def _infer_project_name(config: FraisierConfig) -> str:
    """Return the project name from config (used for naming prefixes)."""
    return config.project_name


class ScaffoldRenderer:
    """Renders Jinja2 templates using fraises.yaml context."""

    def __init__(self, config: FraisierConfig):
        self.config = config
        self.output_dir = Path(config.scaffold.output_dir)
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.context = _build_context(config)

    def get_core_template_paths(self) -> list[str]:
        """Return output file paths for core templates."""
        return [t.replace(".j2", "").replace("core/", "") for t in _CORE_TEMPLATES]

    def get_provider_template_paths(self) -> list[str]:
        """Return output file paths for provider templates."""
        return [
            t.replace(".j2", "").replace("provider/", "") for t in _PROVIDER_TEMPLATES
        ]

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

        # Per-fraise systemd service templates
        project = self.context["project_name"]
        for fraise in self.context["fraises"]:
            name = fraise["name"]
            for env_name in fraise.get("environments", {}):
                svc_name = f"systemd/{project}_{name}_{env_name}.service"
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
        exec_command = service.exec or fraise.get("exec_command")

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
