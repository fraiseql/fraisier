"""Bootstrap command implementation — provision a virgin server end-to-end."""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig
    from fraisier.runners import SSHRunner


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

    Runs 10 ordered, idempotent steps:
      1  Create deploy user
      2  Add deploy user to www-data
      3  Install uv for deploy user
      4  Install fraisier for deploy user
      5  Create project directories
      6  Upload fraises.yaml
      7  Upload scaffold files
      8  Run install.sh --standalone
      9  Enable and start deploy socket
      10 Validate setup

    Every step is idempotent: re-running bootstrap on a partially-set-up
    server is safe.  Steps that find the work already done report
    ``already_done=True`` in verbose output.
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
        uv_path = f"/home/{self.deploy_user}/.local/bin/uv"
        return self._run_remote(
            "Install fraisier for deploy user",
            [
                "sudo",
                "-u",
                self.deploy_user,
                "-H",
                "bash",
                "-c",
                f"{uv_path} tool install fraisier",
            ],
            already_done_cmd=[
                "sudo",
                "-u",
                self.deploy_user,
                "-H",
                "bash",
                "-c",
                f"{uv_path} tool list 2>/dev/null | grep -q fraisier",
            ],
        )

    def _create_directories(self) -> StepResult:
        project_dir = f"/opt/{self.project_name}"
        return self._run_remote(
            "Create directories",
            [
                "bash",
                "-c",
                f"mkdir -p {project_dir} /opt/fraisier /run/fraisier"
                f" && chown {self.deploy_user}:{self.deploy_user} {project_dir}",
            ],
        )

    def _upload_config(self) -> StepResult:
        name = "Upload fraises.yaml"
        if self.dry_run:
            dst = self._FRAISIER_CONFIG_PATH
            return StepResult(
                name=name,
                success=True,
                command=f"scp {self.fraises_yaml_path} ...:{dst}",
            )
        try:
            self.runner.run(["mkdir", "-p", "/opt/fraisier"])
            self.runner.upload(self.fraises_yaml_path, self._FRAISIER_CONFIG_PATH)
            return StepResult(name=name, success=True)
        except subprocess.CalledProcessError as e:
            return StepResult(name=name, success=False, error=e.stderr or str(e))

    def _upload_scaffold_files(self) -> tuple[StepResult, str]:
        name = "Upload scaffold files"
        remote_dir = "/tmp/fraisier-bootstrap"

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

            with tempfile.TemporaryDirectory() as local_dir:
                renderer = ScaffoldRenderer(self.config)
                renderer.output_dir = Path(local_dir)
                renderer.render()
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
        socket_name = f"fraisier-{self.project_name}-{self.environment}-deploy.socket"
        return self._run_remote(
            "Enable and start deploy socket",
            ["systemctl", "enable", "--now", socket_name],
        )

    def _validate(self) -> StepResult:
        fraisier_bin = f"/home/{self.deploy_user}/.local/bin/fraisier"
        return self._run_remote(
            "Validate setup",
            [
                "sudo",
                "-u",
                self.deploy_user,
                "-H",
                "bash",
                "-c",
                f"{fraisier_bin} --config {self._FRAISIER_CONFIG_PATH} validate-setup",
            ],
        )

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
        """Remove the temporary scaffold directory from the remote server."""
        if self.dry_run:
            return
        with contextlib.suppress(Exception):
            self.runner.run(["rm", "-rf", remote_scaffold_dir], check=False)
