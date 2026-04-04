"""Preflight checks — validate server prerequisites before bootstrap."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fraisier.runners import SSHRunner


@dataclass
class CheckResult:
    """Outcome of a single preflight check."""

    name: str
    passed: bool
    message: str = ""
    fix_hint: str = ""


@dataclass
class PreflightResult:
    """Aggregate result of all preflight checks."""

    server: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed)


class PreflightChecker:
    """Run read-only checks on a remote server before bootstrap."""

    def __init__(self, runner: SSHRunner, deploy_user: str = "fraisier") -> None:
        self.runner = runner
        self.deploy_user = deploy_user

    def run_all(self) -> PreflightResult:
        """Run all preflight checks, collecting every result."""
        result = PreflightResult(server=self.runner.host)
        result.checks.append(self.check_ssh())
        result.checks.append(self.check_sudo())
        result.checks.append(self.check_sudo_passwordless())
        for pkg in ("git", "nginx", "psql"):
            result.checks.append(self.check_package(pkg))
        result.checks.append(self.check_disk_space())
        return result

    def check_ssh(self) -> CheckResult:
        """Check SSH connectivity."""
        try:
            self.runner.run(["echo", "ok"], timeout=15)
            return CheckResult(
                name="SSH connectivity",
                passed=True,
                message=f"port {self.runner.port}, user {self.runner.user}",
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name="SSH connectivity",
                passed=False,
                message="Connection timed out",
            )
        except subprocess.CalledProcessError as e:
            return CheckResult(
                name="SSH connectivity",
                passed=False,
                message=e.stderr or str(e),
            )

    def check_sudo(self) -> CheckResult:
        """Check whether sudo is available for the SSH user."""
        try:
            self.runner.run(["sudo", "-n", "true"], timeout=10)
            return CheckResult(name="sudo available", passed=True)
        except subprocess.CalledProcessError:
            return CheckResult(
                name="sudo available",
                passed=False,
                fix_hint=f"Grant sudo to {self.runner.user} or use --ssh-user root",
            )

    def check_sudo_passwordless(self) -> CheckResult:
        """Check whether sudo is passwordless (no prompt required)."""
        try:
            self.runner.run(["sudo", "-n", "true"], timeout=10)
            return CheckResult(name="sudo is passwordless", passed=True)
        except subprocess.CalledProcessError:
            return CheckResult(
                name="sudo is passwordless",
                passed=False,
                fix_hint=(
                    f"sudo visudo -f /etc/sudoers.d/{self.deploy_user}-bootstrap"
                ),
            )

    def check_package(self, name: str) -> CheckResult:
        """Check whether a package binary is available on PATH."""
        try:
            self.runner.run(["which", name], timeout=10)
            return CheckResult(name=f"{name} installed", passed=True)
        except subprocess.CalledProcessError:
            return CheckResult(
                name=f"{name} installed",
                passed=False,
                fix_hint=f"apt install {name}",
            )

    def check_disk_space(self, min_gb: int = 2) -> CheckResult:
        """Check available disk space on /opt (or /)."""
        try:
            out = self.runner.run(["df", "-BG", "/opt"], timeout=10)
            return self._parse_disk_space(out.stdout, min_gb)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return CheckResult(
                name="Disk space",
                passed=False,
                message="Could not check disk space",
            )

    def check_ports(self, ports: list[int]) -> CheckResult:
        """Check whether the given ports are already in use."""
        try:
            out = self.runner.run(["ss", "-tlnp"], timeout=10)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return CheckResult(
                name="Port availability",
                passed=False,
                message="Could not check ports",
            )

        in_use = [port for port in ports if re.search(rf":({port})\b", out.stdout)]

        if in_use:
            return CheckResult(
                name="Port availability",
                passed=False,
                message=f"Ports already in use: {', '.join(str(p) for p in in_use)}",
            )
        return CheckResult(name="Port availability", passed=True)

    @staticmethod
    def _parse_disk_space(output: str, min_gb: int) -> CheckResult:
        """Parse df -BG output and check available space."""
        lines = output.strip().splitlines()
        if len(lines) < 2:
            return CheckResult(
                name="Disk space",
                passed=False,
                message="Could not parse df output",
            )
        parts = lines[1].split()
        if len(parts) < 4:
            return CheckResult(
                name="Disk space",
                passed=False,
                message="Could not parse df output",
            )
        avail_str = parts[3]
        match = re.match(r"(\d+)", avail_str)
        if not match:
            return CheckResult(
                name="Disk space",
                passed=False,
                message="Could not parse df output",
            )
        avail_gb = int(match.group(1))
        if avail_gb < min_gb:
            return CheckResult(
                name="Disk space",
                passed=False,
                message=f"{avail_gb}G free on /opt (minimum {min_gb}G)",
            )
        return CheckResult(
            name="Disk space",
            passed=True,
            message=f"{avail_gb}G free on /opt",
        )
