"""Server setup — provisions infrastructure from fraises.yaml.

Creates directories, symlinks bare repos, installs systemd services,
generates webhook env files, installs nginx vhosts, and validates.
"""

from __future__ import annotations

import secrets
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fraisier.config import FraisierConfig, NginxEnvConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fraisier.runners import CommandRunner


@dataclass
class SetupAction:
    """A single provisioning step."""

    description: str
    command: list[str]
    category: str
    check: list[str] | None = None


class ServerSetup:
    """Provisions server-side infrastructure from fraises.yaml."""

    def __init__(
        self,
        config: FraisierConfig,
        runner: CommandRunner,
        *,
        environment: str | None = None,
        server: str | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.environment = environment
        self.server = server
        self._renderer = ScaffoldRenderer(config)

    def plan(self) -> list[SetupAction]:
        """Build the full ordered list of actions without side effects."""
        actions: list[SetupAction] = []
        actions.extend(self._plan_directories())
        actions.extend(self._plan_symlinks())
        actions.extend(self._plan_sudoers())
        actions.extend(self._plan_app_services())
        actions.extend(self._plan_webhook_service())
        actions.extend(self._plan_env_files())
        actions.extend(self._plan_nginx())
        actions.extend(self._plan_systemd_reload())
        actions.extend(self._plan_validation())
        return actions

    def execute(self) -> list[tuple[SetupAction, bool]]:
        """Run scaffold, write env file, then execute all plan actions.

        Returns list of (action, success) tuples.
        """
        self._renderer.render()
        self._write_env_file()

        actions = self.plan()
        results: list[tuple[SetupAction, bool]] = []

        for action in actions:
            if action.check:
                try:
                    self.runner.run(action.check, check=True)
                    results.append((action, True))
                    continue
                except subprocess.CalledProcessError:
                    pass

            try:
                self.runner.run(action.command, check=True)
                results.append((action, True))
            except subprocess.CalledProcessError:
                results.append((action, False))

        return results

    # ------------------------------------------------------------------
    # Planning methods
    # ------------------------------------------------------------------

    def _plan_directories(self) -> list[SetupAction]:
        deploy_user = self.config.scaffold.deploy_user
        managed_dirs = [
            ("/var/lib/fraisier", deploy_user),
            ("/var/lib/fraisier/repos", deploy_user),
            ("/run/fraisier", deploy_user),
        ]
        root_dirs = [
            ("/etc/fraisier", None),
        ]

        actions: list[SetupAction] = []
        for path, owner in [*managed_dirs, *root_dirs]:
            actions.append(
                SetupAction(
                    description=f"Create {path}",
                    command=["sudo", "mkdir", "-p", path],
                    check=["test", "-d", path],
                    category="directory",
                )
            )
            if owner:
                actions.append(
                    SetupAction(
                        description=f"Set ownership of {path} to {owner}",
                        command=["sudo", "chown", f"{owner}:{owner}", path],
                        category="directory",
                    )
                )
        return actions

    def _plan_symlinks(self) -> list[SetupAction]:
        actions: list[SetupAction] = []
        seen: set[str] = set()
        project = self.config.project_name
        for fraise_name, env_name, env_config in self._iter_fraise_environments():
            git_repo = env_config.get("git_repo")
            if not git_repo:
                continue
            repo_name = f"{project}_{fraise_name}_{env_name}"
            link_name = f"/var/lib/fraisier/repos/{repo_name}.git"
            if link_name in seen:
                continue
            seen.add(link_name)
            actions.append(
                SetupAction(
                    description=f"Symlink {git_repo} -> {link_name}",
                    command=["sudo", "ln", "-sfn", git_repo, link_name],
                    category="symlink",
                )
            )
        return actions

    def _plan_sudoers(self) -> list[SetupAction]:
        output_dir = self.config.scaffold.output_dir
        project = self.config.project_name
        src = f"{output_dir}/sudoers"
        dst = f"/etc/sudoers.d/{project}"
        return [
            SetupAction(
                description="Install sudoers fragment for deploy user",
                command=["sudo", "install", "-m", "0440", src, dst],
                category="sudoers",
            ),
        ]

    def _plan_app_services(self) -> list[SetupAction]:
        output_dir = self.config.scaffold.output_dir
        project = self.config.project_name
        actions: list[SetupAction] = []
        for fraise_name, env_name, _ in self._iter_fraise_environments():
            svc = f"{project}_{fraise_name}_{env_name}.service"
            src = f"{output_dir}/systemd/{svc}"
            dst = f"/etc/systemd/system/{svc}"
            actions.append(
                SetupAction(
                    description=f"Install {svc}",
                    command=["sudo", "cp", src, dst],
                    category="systemd",
                )
            )
        return actions

    def _plan_webhook_service(self) -> list[SetupAction]:
        output_dir = self.config.scaffold.output_dir
        src = f"{output_dir}/fraisier-webhook.service"
        dst = "/etc/systemd/system/fraisier-webhook.service"
        return [
            SetupAction(
                description="Install fraisier-webhook.service",
                command=["sudo", "cp", src, dst],
                category="systemd",
            )
        ]

    def _plan_env_files(self) -> list[SetupAction]:
        output_dir = self.config.scaffold.output_dir
        src = f"{output_dir}/fraisier-webhook.env"
        dst = "/etc/fraisier/webhook.env"
        return [
            SetupAction(
                description="Install webhook env file",
                command=["sudo", "install", "-m", "0640", src, dst],
                check=["test", "-f", dst],
                category="env",
            )
        ]

    def _plan_nginx(self) -> list[SetupAction]:
        output_dir = self.config.scaffold.output_dir
        actions: list[SetupAction] = []

        # Gateway config
        project_name = self._infer_project_name()
        gw_src = f"{output_dir}/nginx/gateway.conf"
        gw_dst = f"/etc/nginx/sites-available/{project_name}"
        actions.append(
            SetupAction(
                description="Install nginx gateway config",
                command=["sudo", "cp", gw_src, gw_dst],
                category="nginx",
            )
        )
        actions.append(
            SetupAction(
                description="Enable nginx gateway config",
                command=[
                    "sudo",
                    "ln",
                    "-sfn",
                    gw_dst,
                    f"/etc/nginx/sites-enabled/{project_name}",
                ],
                category="nginx",
            )
        )

        # Per-environment configs
        project = self.config.project_name
        for fraise_name, env_name, env_config in self._iter_fraise_environments():
            nginx_config = NginxEnvConfig.from_env_dict(env_config)
            if nginx_config is None:
                continue
            conf_name = f"{project}_{fraise_name}_{env_name}"
            src = f"{output_dir}/nginx/{conf_name}.conf"
            dst = f"/etc/nginx/sites-available/{conf_name}"
            actions.append(
                SetupAction(
                    description=f"Install nginx config for {conf_name}",
                    command=["sudo", "cp", src, dst],
                    category="nginx",
                )
            )
            actions.append(
                SetupAction(
                    description=f"Enable nginx config for {conf_name}",
                    command=[
                        "sudo",
                        "ln",
                        "-sfn",
                        dst,
                        f"/etc/nginx/sites-enabled/{conf_name}",
                    ],
                    category="nginx",
                )
            )

        return actions

    def _plan_systemd_reload(self) -> list[SetupAction]:
        actions = [
            SetupAction(
                description="Reload systemd daemon",
                command=["sudo", "systemctl", "daemon-reload"],
                category="systemd",
            ),
            SetupAction(
                description="Enable fraisier-webhook.service",
                command=["sudo", "systemctl", "enable", "fraisier-webhook.service"],
                category="systemd",
            ),
        ]
        project = self.config.project_name
        for fraise_name, env_name, _ in self._iter_fraise_environments():
            svc = f"{project}_{fraise_name}_{env_name}.service"
            actions.append(
                SetupAction(
                    description=f"Enable {svc}",
                    command=["sudo", "systemctl", "enable", svc],
                    category="systemd",
                )
            )
        return actions

    def _plan_validation(self) -> list[SetupAction]:
        actions = [
            SetupAction(
                description="Test nginx configuration",
                command=["sudo", "nginx", "-t"],
                category="validate",
            ),
        ]
        for _, _, env_config in self._iter_fraise_environments():
            git_repo = env_config.get("git_repo")
            if git_repo:
                actions.append(
                    SetupAction(
                        description=f"Verify bare repo exists: {git_repo}",
                        command=["test", "-d", git_repo],
                        category="validate",
                    )
                )
        return actions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_allowed_environments(self) -> set[str] | None:
        """Determine which environments to provision.

        Resolution order:
        1. Explicit ``--environment`` → single environment.
        2. Explicit ``--server`` → environments whose ``server`` field matches.
        3. Auto-detect via hostname/FQDN → environments whose ``server`` matches.
        4. No match → ``None`` (provision everything, backwards-compatible).
        """
        if self.environment:
            return {self.environment}

        if self.server:
            envs = self.config.get_environments_for_server(self.server)
            return set(envs) if envs else None

        # Auto-detect: try FQDN first, then short hostname.
        for hostname in dict.fromkeys([socket.getfqdn(), socket.gethostname()]):
            envs = self.config.get_environments_for_server(hostname)
            if envs:
                return set(envs)

        return None

    def _iter_fraise_environments(
        self,
    ) -> Iterator[tuple[str, str, dict[str, Any]]]:
        """Yield (fraise_name, env_name, env_config), filtered by server/environment."""
        allowed = self._resolve_allowed_environments()
        for fraise_name in self.config.list_fraises():
            fraise = self.config.get_fraise(fraise_name)
            if not fraise:
                continue
            for env_name in fraise.get("environments", {}):
                if allowed is not None and env_name not in allowed:
                    continue
                env_config = self.config.get_fraise_environment(fraise_name, env_name)
                if env_config:
                    yield fraise_name, env_name, env_config

    def _infer_project_name(self) -> str:
        return self.config.project_name

    def _write_env_file(self) -> None:
        """Write webhook env file to scaffold output dir."""
        secret = secrets.token_urlsafe(32)
        config_path = self.config.config_path

        content = (
            "# Fraisier webhook environment\n"
            "# Generated by fraisier setup — edit secrets as needed\n"
            f"FRAISIER_WEBHOOK_SECRET={secret}\n"
            f"FRAISIER_CONFIG={config_path}\n"
            "FRAISIER_PORT=8080\n"
        )
        output = Path(self.config.scaffold.output_dir) / "fraisier-webhook.env"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content)
