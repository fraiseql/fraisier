"""Remote deployment readiness validation via SSH."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fraisier.validation import ValidationCheckResult

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig
    from fraisier.manifest import PathManifest
    from fraisier.runners import SSHRunner


@dataclass
class RepairResult:
    """Result of applying a single fix remotely."""

    check_name: str
    fix_command: str
    applied: bool
    stdout: str = field(default="")
    stderr: str = field(default="")


class RemoteDeploymentValidator:
    """Validate a fraise/environment deployment target via SSH.

    Runs read-only checks on the remote host to verify it is ready to receive
    a deployment: bare git repo ownership, app path, systemd units, wrapper
    scripts, sudoers fragment, and the health endpoint.
    """

    def __init__(
        self,
        fraise_config: dict[str, Any],
        runner: SSHRunner,
        config: FraisierConfig,
    ) -> None:
        self.fraise_config = fraise_config
        self.runner = runner
        self.config = config
        self.fraise_name = fraise_config.get("fraise_name", "unknown")
        self.environment = fraise_config.get("environment", "unknown")
        self.project_name = config.project_name
        self.deploy_user = config.get_deploy_user(self.fraise_name, self.environment)

    def check_manifest_paths(
        self, manifest: PathManifest
    ) -> list[ValidationCheckResult]:
        """Check all managed paths from manifest on remote host.

        For each path in the manifest with create_if_missing=True, verify:
        1. Path exists
        2. Path is owned by the declared owner:group
        3. Path has the declared mode

        Returns a ValidationCheckResult per path with a derived fix_command
        if the path needs fixing.

        Args:
            manifest: PathManifest containing all managed paths

        Returns:
            List of ValidationCheckResult instances, one per path to check
        """
        results = []
        for mp in manifest.all_paths():
            if not mp.create_if_missing:
                continue  # systemd-managed paths — skip
            result = self._check_single_path(mp)
            results.append(result)
        return results

    def _check_single_path(self, mp: Any) -> ValidationCheckResult:  # ManagedPath
        """Check a single managed path on remote host.

        Args:
            mp: ManagedPath to check

        Returns:
            ValidationCheckResult with passed=True or False, and fix_command if needed
        """
        # Use stat to check if path exists and get owner:group
        stat_cmd = f"stat -c '%U %G' {shlex.quote(str(mp.path))} 2>/dev/null"
        proc = self._remote_shell(stat_cmd)

        if proc.returncode != 0 or not proc.stdout.strip():
            # Path doesn't exist
            return ValidationCheckResult(
                name=f"path:{mp.path}",
                passed=False,
                message=f"{mp.path} does not exist",
                severity="error",
                fix_command=self._fix_command_for_path(mp, missing=True),
            )

        # Path exists — check owner:group
        actual_owner, actual_group = proc.stdout.strip().split()
        if actual_owner == mp.owner and actual_group == mp.group:
            # Correct ownership
            return ValidationCheckResult(name=f"path:{mp.path}", passed=True)

        # Wrong ownership
        return ValidationCheckResult(
            name=f"path:{mp.path}",
            passed=False,
            message=(
                f"{mp.path} owned by {actual_owner}:{actual_group}, "
                f"expected {mp.owner}:{mp.group}"
            ),
            severity="error",
            fix_command=self._fix_command_for_path(mp, missing=False),
        )

    def _fix_command_for_path(self, mp: Any, missing: bool) -> str:  # ManagedPath
        """Generate a fix command for a path.

        Args:
            mp: ManagedPath to generate command for
            missing: True if path doesn't exist, False if ownership is wrong

        Returns:
            Fix command with sudo and quoting
        """
        path_quoted = shlex.quote(str(mp.path))
        if missing:
            return (
                f"sudo mkdir -p {path_quoted} && "
                f"sudo chown {mp.owner}:{mp.group} {path_quoted} && "
                f"sudo chmod {mp.mode:o} {path_quoted}"
            )
        # Path exists but wrong owner
        return f"sudo chown {mp.owner}:{mp.group} {path_quoted}"

    def run_all(self) -> list[ValidationCheckResult]:
        """Run all remote deployment readiness checks."""
        results: list[ValidationCheckResult] = []

        ssh_result = self._check_ssh()
        results.append(ssh_result)
        if not ssh_result.passed:
            return results

        for check in [
            self._check_git_repo,
            self._check_app_path,
            self._check_systemd_service,
            self._check_systemd_socket,
            self._check_wrapper_scripts,
            self._check_sudoers,
            self._check_health_endpoint,
            self._check_webhook_service,
        ]:
            result = check()
            if isinstance(result, list):
                results.extend(result)  # ty: ignore[invalid-argument-type]
            else:
                results.append(result)

        return results

    def repair_all(self, results: list[ValidationCheckResult]) -> list[RepairResult]:
        """Apply fixes for all failed checks that have a fix_command."""
        repair_results = []
        for result in results:
            if result.passed or result.fix_command is None:
                continue
            proc = self._remote_shell(result.fix_command)
            repair_results.append(
                RepairResult(
                    check_name=result.name,
                    fix_command=result.fix_command,
                    applied=proc.returncode == 0,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
            )
        return repair_results

    def _remote(
        self, cmd: list[str], *, timeout: int = 15
    ) -> subprocess.CompletedProcess[str]:
        return self.runner.run(cmd, timeout=timeout, check=False)

    def _remote_shell(
        self, cmd: str, *, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        return self.runner.run(["sh", "-c", cmd], timeout=timeout, check=False)

    def _check_ssh(self) -> ValidationCheckResult:
        try:
            self.runner.run(["echo", "ok"], timeout=15)
            return ValidationCheckResult(
                name="ssh_connectivity",
                passed=True,
                message=f"{self.runner.user}@{self.runner.host}:{self.runner.port}",
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError) as e:
            return ValidationCheckResult(
                name="ssh_connectivity",
                passed=False,
                message=str(e),
                severity="error",
            )

    def _check_git_repo(self) -> ValidationCheckResult:
        git_repo = self.fraise_config.get("git_repo")
        if not git_repo:
            return ValidationCheckResult(
                name="git_repo",
                passed=True,
                message="not configured (skipped)",
            )

        if self._remote(["test", "-d", git_repo]).returncode != 0:
            return ValidationCheckResult(
                name="git_repo",
                passed=False,
                message=(
                    f"Bare git repo not found: {git_repo}. "
                    f"Fix: sudo git init --bare {git_repo} && "
                    f"sudo chown -R {self.deploy_user} {git_repo}"
                ),
                severity="error",
            )

        owner = self._remote(["stat", "-c", "%U", git_repo]).stdout.strip()
        if owner != self.deploy_user:
            return ValidationCheckResult(
                name="git_repo",
                passed=False,
                message=(
                    f"{git_repo} owned by '{owner}', expected '{self.deploy_user}'. "
                    f"Fix: sudo chown -R {self.deploy_user} {git_repo}"
                ),
                severity="error",
                fix_command=f"chown -R {self.deploy_user} {git_repo}",
            )

        return ValidationCheckResult(
            name="git_repo",
            passed=True,
            message=f"{git_repo} (owner: {owner})",
        )

    def _check_app_path(self) -> ValidationCheckResult:
        app_path = self.fraise_config.get("app_path")
        if not app_path:
            return ValidationCheckResult(
                name="app_path",
                passed=False,
                message="app_path not configured",
                severity="error",
            )

        if self._remote(["test", "-d", app_path]).returncode != 0:
            return ValidationCheckResult(
                name="app_path",
                passed=False,
                message=(
                    f"{app_path} does not exist. "
                    f"Fix: sudo mkdir -p {app_path} && "
                    f"sudo chown {self.deploy_user} {app_path}"
                ),
                severity="error",
                fix_command=(
                    f"mkdir -p {app_path} && chown {self.deploy_user} {app_path}"
                ),
            )

        owner = self._remote(["stat", "-c", "%U", app_path]).stdout.strip()
        if owner != self.deploy_user:
            return ValidationCheckResult(
                name="app_path",
                passed=False,
                message=(
                    f"{app_path} owned by '{owner}', expected '{self.deploy_user}'. "
                    f"Fix: sudo chown {self.deploy_user} {app_path}"
                ),
                severity="error",
                fix_command=f"chown {self.deploy_user} {app_path}",
            )

        return ValidationCheckResult(
            name="app_path",
            passed=True,
            message=f"{app_path} (owner: {owner})",
        )

    def _check_systemd_service(self) -> ValidationCheckResult:
        service_name = self.fraise_config.get("systemd_service")
        if not service_name:
            return ValidationCheckResult(
                name="systemd_service",
                passed=True,
                message="not configured (skipped)",
            )

        status = self._remote(["systemctl", "is-active", service_name]).stdout.strip()
        if status == "active":
            return ValidationCheckResult(
                name="systemd_service",
                passed=True,
                message=f"{service_name} is active",
            )
        return ValidationCheckResult(
            name="systemd_service",
            passed=False,
            message=(
                f"{service_name} is {status or 'not installed'}. "
                f"Fix: sudo systemctl enable --now {service_name}"
            ),
            severity="error",
            fix_command=f"systemctl enable --now {service_name}",
        )

    def _check_systemd_socket(self) -> ValidationCheckResult:
        from fraisier.naming import deploy_socket_name

        socket_name = deploy_socket_name(self.fraise_config, self.environment)
        status = self._remote(["systemctl", "is-active", socket_name]).stdout.strip()
        if status == "active":
            return ValidationCheckResult(
                name="systemd_socket",
                passed=True,
                message=f"{socket_name} is active",
            )
        return ValidationCheckResult(
            name="systemd_socket",
            passed=False,
            message=(
                f"{socket_name} is {status or 'not installed'}. "
                f"Fix: sudo systemctl enable --now {socket_name}"
            ),
            severity="error",
            fix_command=f"systemctl enable --now {socket_name}",
        )

    def _check_wrapper_scripts(self) -> list[ValidationCheckResult]:
        results: list[ValidationCheckResult] = []
        libexec = "/usr/local/libexec/fraisier"
        wrappers = [
            ("systemctl_wrapper", f"{libexec}/systemctl-{self.project_name}"),
            ("pg_wrapper", f"{libexec}/pgadmin-{self.project_name}"),
        ]
        for label, path in wrappers:
            if self._remote(["test", "-x", path]).returncode == 0:
                results.append(
                    ValidationCheckResult(name=label, passed=True, message=path)
                )
            elif self._remote(["test", "-f", path]).returncode == 0:
                results.append(
                    ValidationCheckResult(
                        name=label,
                        passed=False,
                        message=(
                            f"{path} exists but is not executable. "
                            f"Fix: chmod 755 {path}"
                        ),
                        severity="error",
                        fix_command=f"chmod 755 {path}",
                    )
                )
            else:
                results.append(
                    ValidationCheckResult(
                        name=label,
                        passed=False,
                        message=(
                            f"{path} not found. "
                            "Fix: run 'fraisier scaffold-install' to deploy "
                            "wrapper scripts"
                        ),
                        severity="error",
                    )
                )
        return results

    def _check_sudoers(self) -> ValidationCheckResult:
        path = f"/etc/sudoers.d/{self.project_name}"
        if self._remote(["test", "-f", path]).returncode == 0:
            return ValidationCheckResult(
                name="sudoers",
                passed=True,
                message=f"Installed: {path}",
            )
        return ValidationCheckResult(
            name="sudoers",
            passed=False,
            message=(
                f"Sudoers file not found: {path}. "
                "Fix: run 'fraisier scaffold-install' to install sudoers rules"
            ),
            severity="warning",
        )

    def _check_health_endpoint(self) -> ValidationCheckResult:
        from fraisier.health_check import HTTPHealthChecker

        health_config = self.fraise_config.get("health_check")
        if not health_config:
            return ValidationCheckResult(
                name="health_endpoint",
                passed=True,
                message="not configured (skipped)",
            )

        url = health_config.get("url") if isinstance(health_config, dict) else None
        if not url:
            return ValidationCheckResult(
                name="health_endpoint",
                passed=True,
                message="URL not configured (skipped)",
            )

        checker = HTTPHealthChecker(url)
        result = checker.check(timeout=5.0)
        if result.success:
            return ValidationCheckResult(
                name="health_endpoint",
                passed=True,
                message=f"Healthy: {result.message}",
            )
        return ValidationCheckResult(
            name="health_endpoint",
            passed=False,
            message=(
                f"Health check failed: {result.message or 'no response'}. Check: {url}"
            ),
            severity="error",
        )

    def _check_webhook_service(self) -> ValidationCheckResult:
        service_name = f"fraisier-{self.project_name}-webhook.service"
        status = self._remote(["systemctl", "is-active", service_name]).stdout.strip()
        if status == "active":
            return ValidationCheckResult(
                name="webhook_service",
                passed=True,
                message=f"{service_name} is active",
            )
        return ValidationCheckResult(
            name="webhook_service",
            passed=False,
            message=(
                f"{service_name} is {status or 'not installed'}. "
                f"Fix: sudo systemctl enable --now {service_name}"
            ),
            severity="error",
            fix_command=f"systemctl enable --now {service_name}",
        )
