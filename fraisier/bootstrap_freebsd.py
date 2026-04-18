"""FreeBSD bootstrap implementation — provision a FreeBSD server end-to-end."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fraisier.bootstrap import ServerBootstrapper, StepResult

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig
    from fraisier.runners import SSHRunner


class FreebsdBootstrapper(ServerBootstrapper):
    """Provision a FreeBSD server end-to-end via SSH.

    Similar to ServerBootstrapper but uses FreeBSD-specific tools:
    - pkg instead of apt
    - pw instead of useradd
    - rc.d instead of systemd
    """

    def __init__(
        self,
        config: FraisierConfig,
        environment: str,
        runner: SSHRunner,
        fraises_yaml_path: str,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> None:
        super().__init__(
            config, environment, runner, fraises_yaml_path, dry_run, verbose
        )
        self.os_name = "FreeBSD"

    def _install_python(self) -> StepResult:
        """Install Python 3.11+ using pkg."""
        return self._run_remote(
            "Install Python",
            ["sudo", "pkg", "install", "-y", "python311"],
        )

    def _install_postgres_client(self) -> StepResult:
        """Install PostgreSQL client using pkg."""
        return self._run_remote(
            "Install PostgreSQL client",
            ["sudo", "pkg", "install", "-y", "postgresql15-client"],
        )

    def _install_git(self) -> StepResult:
        """Install Git using pkg."""
        return self._run_remote(
            "Install Git",
            ["sudo", "pkg", "install", "-y", "git"],
        )

    def _create_deploy_user(self) -> StepResult:
        """Create deploy user using pw."""
        return self._run_remote(
            "Create deploy user",
            ["sudo", "pw", "useradd", self.deploy_user, "-m", "-s", "/bin/sh"],
        )

    def _setup_rc_infrastructure(self) -> StepResult:
        """Setup rc.d service infrastructure."""
        return self._run_remote(
            "Setup rc.d infrastructure",
            ["sudo", "sysrc", "fraisier_enable=YES"],
        )

    # Override other methods as needed for FreeBSD specifics
    # For now, inherit the rest from ServerBootstrapper
