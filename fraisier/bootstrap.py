"""Bootstrap command implementation — provision a virgin server end-to-end."""

from __future__ import annotations

import contextlib
import logging
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from importlib.metadata import version as importlib_version
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig
    from fraisier.runners import SSHRunner


def resolve_become_password(command: str) -> str:
    """Run a shell command and capture its stdout as the sudo password.

    The command output is stripped of trailing whitespace.  If the command
    exits with a non-zero status, a ``RuntimeError`` is raised.

    **Security**: the returned value must never be logged.
    ``become_password_command`` runs with ``shell=True`` to support pipe
    commands common in password managers (e.g. ``pass show foo | head -1``).
    Because of this, ``fraises.yaml`` must be treated as a trusted file and
    its access restricted to the deploy user (mode 0600 or equivalent).
    """
    result = subprocess.run(
        command,
        shell=True,  # intentional — fraises.yaml is operator-controlled
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = "become_password_command failed (check become_password_command config)"
        raise RuntimeError(msg)
    return result.stdout.strip()


logger = logging.getLogger("fraisier")


@dataclass
class StepResult:
    """Outcome of a single bootstrap step."""

    name: str
    success: bool
    already_done: bool = False
    output: str = ""
    error: str = ""
    command: str = ""


@dataclass
class BootstrapResult:
    """Aggregate result of the full bootstrap run."""

    steps: list[StepResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return all(s.success for s in self.steps)

    @property
    def failed_step(self) -> StepResult | None:
        for step in self.steps:
            if not step.success:
                return step
        return None


class ServerBootstrapper:
    """Provision a virgin server end-to-end via SSH.

    Runs 11 ordered, idempotent steps:
      1  Create deploy user
      2  Add deploy user to www-data
      3  Install uv for deploy user
      4  Install fraisier for deploy user
      5  Restart webhook service (if running, so new fraisier takes effect)
      6  Create project directories
      7  Upload fraises.yaml
      8  Upload scaffold files
      9  Run install.sh --standalone
      10 Enable and start deploy socket
      11 Validate setup

    Every step is idempotent: re-running bootstrap on a partially-set-up
    server is safe.  Steps that find the work already done report
    ``already_done=True`` in verbose output.

    The generated deploy service unit sets
    ``GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new`` so the
    first ``git fetch`` succeeds without needing ``known_hosts`` pre-seeded
    for the git host.
    """

    _FRAISIER_CONFIG_PATH = "/opt/fraisier/fraises.yaml"

    def __init__(
        self,
        config: FraisierConfig,
        environment: str,
        runner: SSHRunner,
        fraises_yaml_path: Path,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> None:
        self.config = config
        self.environment = environment
        self.runner = runner
        self.fraises_yaml_path = fraises_yaml_path
        self.dry_run = dry_run
        self.verbose = verbose
        self.deploy_user = config.scaffold.deploy_user
        self.project_name = config.project_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def bootstrap(self) -> BootstrapResult:
        """Run all provisioning steps in order, aborting on the first failure."""
        result = BootstrapResult()
        remote_scaffold_dir = "/tmp/fraisier-bootstrap"

        for step_fn in (
            self._create_deploy_user,
            self._add_to_www_data,
            self._install_uv,
            self._install_fraisier,
            self._restart_webhook_if_running,
            self._create_directories,
        ):
            step = step_fn()
            result.steps.append(step)
            if not step.success:
                return result

        upload_config = self._upload_config()
        result.steps.append(upload_config)
        if not upload_config.success:
            return result

        upload_scaffold, remote_scaffold_dir = self._upload_scaffold_files()
        result.steps.append(upload_scaffold)
        if not upload_scaffold.success:
            return result

        for step_fn2 in (
            lambda: self._run_install(remote_scaffold_dir),
            self._enable_sockets,
            self._validate,
        ):
            step = step_fn2()
            result.steps.append(step)
            if not step.success:
                self._cleanup(remote_scaffold_dir)
                return result

        self._cleanup(remote_scaffold_dir)
        return result

    # ------------------------------------------------------------------
    # Individual steps
    # ------------------------------------------------------------------

    def _create_deploy_user(self) -> StepResult:
        return self._run_remote(
            "Create deploy user",
            [
                "useradd",
                "--system",
                "--create-home",
                "--shell",
                "/usr/sbin/nologin",
                self.deploy_user,
            ],
            already_done_cmd=["id", "-u", self.deploy_user],
        )

    def _add_to_www_data(self) -> StepResult:
        return self._run_remote(
            "Add deploy user to www-data",
            ["usermod", "-aG", "www-data", self.deploy_user],
        )

    def _install_uv(self) -> StepResult:
        uv_path = f"/home/{self.deploy_user}/.local/bin/uv"
        return self._run_remote(
            "Install uv for deploy user",
            [
                "sudo",
                "-n",
                "-u",
                self.deploy_user,
                "-H",
                "bash",
                "-c",
                "curl -LsSf https://astral.sh/uv/install.sh | sh",
            ],
            already_done_cmd=["test", "-f", uv_path],
        )

    def _install_fraisier(self) -> StepResult:
        client_version = importlib_version("fraisier")
        uv_path = f"/home/{self.deploy_user}/.local/bin/uv"
        return self._run_remote(
            "Install fraisier for deploy user",
            [
                "sudo",
                "-n",
                "-u",
                self.deploy_user,
                "-H",
                "bash",
                "-c",
                (
                    f"{uv_path} tool install --force"
                    f" --refresh-package fraisier fraisier=={client_version}"
                ),
            ],
        )

    def _restart_webhook_if_running(self) -> StepResult:
        """Restart the webhook service after a fraisier upgrade.

        If the service is not yet running (fresh install), this is a no-op.
        """
        name = "Restart webhook service"
        webhook_svc = f"fraisier-{self.project_name}-webhook.service"

        if self.dry_run:
            return StepResult(
                name=name,
                success=True,
                command=(
                    f"systemctl is-active {webhook_svc}"
                    f" && systemctl restart {webhook_svc}"
                ),
            )

        try:
            self.runner.run(["systemctl", "is-active", "--quiet", webhook_svc])
        except subprocess.CalledProcessError:
            # Service not running (fresh install or stopped) — nothing to do.
            return StepResult(name=name, success=True, already_done=True)

        try:
            self.runner.run(["systemctl", "restart", webhook_svc])
            return StepResult(name=name, success=True)
        except subprocess.CalledProcessError as e:
            return StepResult(name=name, success=False, error=e.stderr or str(e))

    def _create_directories(self) -> StepResult:
        project_dir = f"/opt/{self.project_name}"
        # The persistent scaffold state tree (#283): the deploy renders here and
        # the socket helper reads its baked install.sh from here. Owned by
        # deploy_user so deploy-time regeneration (which runs as that user) can
        # refresh it.
        state_dir = self.config.scaffold_state_dir
        return self._run_remote(
            "Create directories",
            [
                "bash",
                "-c",
                f"mkdir -p {project_dir} {state_dir} /opt/fraisier /run/fraisier"
                f" && chown {self.deploy_user}:{self.deploy_user}"
                f" {project_dir} {state_dir}",
            ],
        )

    def _relative_template_dir(self) -> Path | None:
        """The configured ``scaffold.template_dir``, if it is relative and present.

        Absolute paths are skipped deliberately: they name a location the
        operator manages on the server, and uploading over one would be
        surprising. Mirrors ``GitDeployMixin._sync_template_dir`` (#312).
        """
        configured = getattr(self.config.scaffold, "template_dir", None)
        if not configured:
            return None
        rel = Path(configured)
        if rel.is_absolute():
            return None
        return rel

    def _upload_config(self) -> StepResult:
        name = "Upload fraises.yaml"
        rel_templates = self._relative_template_dir()

        if self.dry_run:
            dst = self._FRAISIER_CONFIG_PATH
            command = f"scp {self.fraises_yaml_path} ...:{dst}"
            if rel_templates is not None:
                command += (
                    f" + scp -r {rel_templates} ...:/opt/fraisier/{rel_templates}"
                )
            return StepResult(name=name, success=True, command=command)

        try:
            self.runner.run(["mkdir", "-p", "/opt/fraisier"])
            self.runner.upload(self.fraises_yaml_path, self._FRAISIER_CONFIG_PATH)
        except subprocess.CalledProcessError as e:
            return StepResult(name=name, success=False, error=e.stderr or str(e))

        self._upload_template_dir(rel_templates)
        return StepResult(name=name, success=True)

    def _upload_template_dir(self, rel_templates: Path | None) -> None:
        """Carry ``scaffold.template_dir`` to the server config dir (#318).

        A relative ``template_dir`` resolves against the *config* directory, so
        on the server it means ``/opt/fraisier/<template_dir>``. Bootstrap
        uploaded only fraises.yaml, leaving that path dangling until the first
        deploy's config sync created it (#312). Bootstrap's own scaffold is
        rendered locally, so the initial tree was correct — but any server-side
        render in the meantime silently fell back to the built-ins.

        Best-effort, matching the deploy path: provisioning that would
        otherwise succeed must not fail over templates, but the failure is
        logged loudly because the consequence is a host rendering built-ins
        while the repo says otherwise.
        """
        if rel_templates is None:
            return

        source = Path(self.fraises_yaml_path).parent / rel_templates
        dest = f"/opt/fraisier/{rel_templates}"
        if not source.is_dir():
            logger.warning(
                "scaffold.template_dir is set but %s does not exist locally — "
                "the server will render with built-in templates",
                source,
            )
            return

        try:
            # Replace wholesale: a template deleted upstream must not survive
            # on the server, where it would keep shadowing the built-in.
            self.runner.run(["rm", "-rf", dest])
            self.runner.run(["mkdir", "-p", dest])
            self.runner.upload_tree(source, dest)
        except Exception:
            logger.warning(
                "Failed to upload scaffold.template_dir %s -> %s; the server "
                "may render with built-in templates",
                source,
                dest,
                exc_info=True,
            )

    def _upload_scaffold_files(self) -> tuple[StepResult, str]:
        name = "Upload scaffold files"
        # Persist the rendered tree at the state_dir the socket helper reads
        # from, so the helper is valid immediately after bootstrap (#283) rather
        # than only after the first config-changing deploy.
        remote_dir = self.config.scaffold_state_dir

        if self.dry_run:
            return (
                StepResult(
                    name=name,
                    success=True,
                    command=f"tar+ssh scaffold → {remote_dir}",
                ),
                remote_dir,
            )

        try:
            from fraisier.scaffold.renderer import ScaffoldRenderer

            # Determine the server hosting this environment so we only upload
            # configs relevant to it (nginx, systemd units, etc.).
            server = self.config.environments.get(self.environment, {}).get("server")
            if server is None:
                for fraise_cfg in self.config.fraises.values():
                    env_cfg = fraise_cfg.get("environments", {}).get(self.environment)
                    if isinstance(env_cfg, dict):
                        server = env_cfg.get("server")
                        if server:
                            break

            with tempfile.TemporaryDirectory() as local_dir:
                renderer = ScaffoldRenderer(self.config, server=server)
                renderer.output_dir = Path(local_dir)
                renderer.render()
                # Detect placeholder files written for missing templates so we
                # fail fast here rather than uploading broken scaffold files
                # that produce confusing errors on the remote server.
                for p in Path(local_dir).rglob("*"):
                    if p.is_file():
                        first_line = p.read_text(errors="replace").splitlines()[:1]
                        if first_line and first_line[0].startswith("# Placeholder:"):
                            msg = f"Scaffold template missing: {first_line[0]}"
                            raise RuntimeError(msg)
                self.runner.run(["mkdir", "-p", remote_dir])
                self.runner.upload_tree(Path(local_dir), remote_dir)

            return StepResult(name=name, success=True), remote_dir
        except subprocess.CalledProcessError as e:
            return (
                StepResult(name=name, success=False, error=e.stderr or str(e)),
                remote_dir,
            )
        except Exception as e:
            return StepResult(name=name, success=False, error=str(e)), remote_dir

    def _run_install(self, remote_scaffold_dir: str) -> StepResult:
        cmd = [
            "bash",
            f"{remote_scaffold_dir}/install.sh",
            "--standalone",
            "--scaffold-dir",
            remote_scaffold_dir,
        ]
        if self.verbose:
            cmd.append("--verbose")
        return self._run_remote("Run install.sh --standalone", cmd)

    def _enable_sockets(self) -> StepResult:
        from fraisier.naming import deploy_socket_name

        sockets = [
            deploy_socket_name(env_config, self.environment, fraise_name)
            for fraise_name, fraise_config in self.config.fraises.items()
            for env_config in [
                fraise_config.get("environments", {}).get(self.environment)
            ]
            if env_config is not None
        ]

        if not sockets:
            return StepResult(
                name="Enable and start deploy socket",
                success=False,
                error=f"No fraises found for environment '{self.environment}'",
            )

        return self._run_remote(
            "Enable and start deploy socket",
            ["systemctl", "enable", "--now", *sockets],
        )

    def _validate(self) -> StepResult:
        fraisier_bin = f"/home/{self.deploy_user}/.local/bin/fraisier"

        fraises = [
            name
            for name, fraise_config in self.config.fraises.items()
            if self.environment in fraise_config.get("environments", {})
        ]

        if not fraises:
            return StepResult(
                name="Validate setup",
                success=False,
                error=f"No fraises found for environment '{self.environment}'",
            )

        for fraise in fraises:
            result = self._run_remote(
                f"Validate setup ({fraise})",
                [
                    "sudo",
                    "-n",
                    "-u",
                    self.deploy_user,
                    "-H",
                    "bash",
                    "-c",
                    f"{fraisier_bin} --config {self._FRAISIER_CONFIG_PATH}"
                    f" validate-setup {shlex.quote(fraise)}"
                    f" {shlex.quote(self.environment)}",
                ],
            )
            if not result.success:
                return result

        return StepResult(name="Validate setup", success=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_remote(
        self,
        name: str,
        cmd: list[str],
        already_done_cmd: list[str] | None = None,
    ) -> StepResult:
        """Run a remote command via the runner.

        If *already_done_cmd* is given, run it first.  If it succeeds the
        work is already done and we skip the main command.

        In dry-run mode neither command is executed.
        """
        if self.dry_run:
            return StepResult(name=name, success=True, command=" ".join(cmd))

        if already_done_cmd is not None:
            try:
                self.runner.run(already_done_cmd)
                return StepResult(name=name, success=True, already_done=True)
            except subprocess.CalledProcessError:
                pass  # Not done yet — fall through to main command

        try:
            result = self.runner.run(cmd)
            return StepResult(name=name, success=True, output=result.stdout)
        except subprocess.CalledProcessError as e:
            return StepResult(
                name=name,
                success=False,
                error=e.stderr or str(e),
                command=" ".join(cmd),
            )

    def _cleanup(self, remote_scaffold_dir: str) -> None:
        """Remove the temporary scaffold directory from the remote server.

        Never removes the persistent scaffold ``state_dir`` (#283): the socket
        helper reads its baked install.sh from there, so it must survive
        bootstrap (including a late-step failure).
        """
        if self.dry_run:
            return
        if remote_scaffold_dir == self.config.scaffold_state_dir:
            return
        with contextlib.suppress(Exception):
            self.runner.run(["rm", "-rf", remote_scaffold_dir], check=False)
