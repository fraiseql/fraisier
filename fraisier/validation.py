"""Pre-deploy validation checks and drift detection.

Provides a registry of validation checks that verify the project
is ready for deployment: config validity, provider availability,
deploy user existence, etc.

Also provides drift detection for scaffolded files.
"""

import hashlib
import logging
import os
import pwd
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fraisier.config import FraisierConfig

logger = logging.getLogger(__name__)

VALID_STRATEGIES = {"rebuild", "migrate", "apply", "restore_migrate"}


@dataclass
class ValidationCheckResult:
    """Result of a single validation check."""

    name: str
    passed: bool
    message: str | None = None
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        d: dict[str, Any] = {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
        }
        if self.message:
            d["message"] = self.message
        return d


class ValidationRunner:
    """Run all registered validation checks against a config."""

    def __init__(self, config: FraisierConfig):
        self.config = config

    def run_all(self) -> list[ValidationCheckResult]:
        """Execute all checks and return results."""
        results: list[ValidationCheckResult] = []
        basic_checks = [
            self._check_config_valid,
            self._check_deploy_user,
            self._check_fraises_have_environments,
            self._check_required_fields,
            self._check_health_check_urls,
            self._check_database_strategies,
            self._check_missing_health_checks,
        ]
        for check in basic_checks:
            outcome = check()
            if isinstance(outcome, list):
                results.extend(outcome)
            else:
                results.append(outcome)
        return results

    def _check_config_valid(self) -> ValidationCheckResult:
        """Check that config loads and has fraises."""
        try:
            fraises = self.config.list_fraises()
            if not fraises:
                return ValidationCheckResult(
                    name="config_valid",
                    passed=False,
                    message="No fraises defined in config",
                )
            return ValidationCheckResult(name="config_valid", passed=True)
        except Exception as e:
            return ValidationCheckResult(
                name="config_valid",
                passed=False,
                message=str(e),
            )

    def _check_deploy_user(self) -> list[ValidationCheckResult]:
        """Check that deploy users and app users exist on the system."""
        results: list[ValidationCheckResult] = []
        checked: set[str] = set()

        def _check_user(user: str, label: str) -> None:
            if user in checked:
                return
            checked.add(user)
            try:
                pwd.getpwnam(user)
                results.append(
                    ValidationCheckResult(
                        name=f"user_{user}",
                        passed=True,
                        severity="error",
                    )
                )
            except KeyError:
                results.append(
                    ValidationCheckResult(
                        name=f"user_{user}",
                        passed=False,
                        message=(
                            f"{label} '{user}' does not exist. "
                            f"Fix: sudo useradd -r -s /bin/bash {user}"
                        ),
                        severity="error",
                    )
                )

        # Global deploy user
        _check_user(self.config.scaffold.deploy_user, "Deploy user")

        # Per-environment users
        for fraise_name in self.config.list_fraises():
            for env_name in self.config.list_environments(fraise_name):
                env = self.config.get_fraise_environment(fraise_name, env_name)
                if not env:
                    continue
                env_deploy = env.get("deploy_user")
                if env_deploy:
                    _check_user(env_deploy, f"Deploy user ({env_name})")
                svc = env.get("service", {})
                app_user = svc.get("user") if isinstance(svc, dict) else None
                if app_user:
                    _check_user(app_user, f"App user ({fraise_name}/{env_name})")

        return results

    def _check_fraises_have_environments(self) -> ValidationCheckResult:
        """Check that every fraise has at least one environment."""
        for name in self.config.list_fraises():
            envs = self.config.list_environments(name)
            if not envs:
                return ValidationCheckResult(
                    name="fraises_have_environments",
                    passed=False,
                    message=f"Fraise '{name}' has no environments",
                )
        return ValidationCheckResult(name="fraises_have_environments", passed=True)

    def _check_required_fields(self) -> list[ValidationCheckResult]:
        """Check that each fraise has required fields (type, app_path)."""
        results: list[ValidationCheckResult] = []
        for name in self.config.list_fraises():
            fraise = self.config.get_fraise(name) or {}
            if not fraise.get("type"):
                results.append(
                    ValidationCheckResult(
                        name="required_fields",
                        passed=False,
                        message=(
                            f"Fraise '{name}' is missing 'type'. "
                            "Fix: add 'type: api' (or etl, scheduled, backup)"
                        ),
                        severity="error",
                    )
                )
            for env_name in self.config.list_environments(name):
                env = self.config.get_environment(name, env_name) or {}
                if not env.get("app_path"):
                    results.append(
                        ValidationCheckResult(
                            name="required_fields",
                            passed=False,
                            message=(
                                f"Fraise '{name}' environment '{env_name}' "
                                "is missing 'app_path'. "
                                "Fix: add 'app_path: /var/www/my-app'"
                            ),
                            severity="error",
                        )
                    )
        if not results:
            results.append(ValidationCheckResult(name="required_fields", passed=True))
        return results

    def _check_health_check_urls(self) -> list[ValidationCheckResult]:
        """Check that health check URLs are well-formed."""
        from urllib.parse import urlparse

        results: list[ValidationCheckResult] = []
        for name in self.config.list_fraises():
            for env_name in self.config.list_environments(name):
                env = self.config.get_environment(name, env_name) or {}
                hc = env.get("health_check")
                if not hc:
                    continue
                url = hc.get("url", "")
                parsed = urlparse(url)
                if not parsed.scheme or not parsed.netloc:
                    results.append(
                        ValidationCheckResult(
                            name="health_check_urls",
                            passed=False,
                            message=(
                                f"Fraise '{name}' environment '{env_name}': "
                                f"invalid health check URL '{url}'. "
                                "Fix: use a full URL like 'http://localhost:8000/health'"
                            ),
                            severity="error",
                        )
                    )
        if not results:
            results.append(ValidationCheckResult(name="health_check_urls", passed=True))
        return results

    def _check_database_strategies(self) -> list[ValidationCheckResult]:
        """Check that database strategies are valid."""
        results: list[ValidationCheckResult] = []
        for name in self.config.list_fraises():
            for env_name in self.config.list_environments(name):
                env = self.config.get_environment(name, env_name) or {}
                db = env.get("database")
                if not db:
                    continue
                strategy = db.get("strategy", "")
                if strategy and strategy not in VALID_STRATEGIES:
                    valid = ", ".join(sorted(VALID_STRATEGIES))
                    results.append(
                        ValidationCheckResult(
                            name="database_strategy",
                            passed=False,
                            message=(
                                f"Fraise '{name}' environment '{env_name}': "
                                f"unknown strategy '{strategy}'. "
                                f"Fix: use one of: {valid}"
                            ),
                            severity="error",
                        )
                    )
        if not results:
            results.append(ValidationCheckResult(name="database_strategy", passed=True))
        return results

    def run_operational(
        self,
        *,
        skip_ssh: bool = False,
        skip_db: bool = False,
        skip_git: bool = False,
    ) -> list[ValidationCheckResult]:
        """Run operational pre-flight checks (SSH, git, DB, etc.)."""
        results: list[ValidationCheckResult] = []

        if not skip_git:
            results.extend(self._check_git_reachability())
        if not skip_ssh:
            results.extend(self._check_ssh_connectivity())
        if not skip_db:
            results.extend(self._check_db_connectivity())

        return results

    def _check_git_reachability(self) -> list[ValidationCheckResult]:
        """Check that clone_urls are reachable via git ls-remote."""
        results: list[ValidationCheckResult] = []
        seen_urls: set[str] = set()
        for name in self.config.list_fraises():
            for env_name in self.config.list_environments(name):
                env = self.config.get_environment(name, env_name) or {}
                url = env.get("clone_url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                try:
                    subprocess.run(
                        ["git", "ls-remote", "--exit-code", url],
                        capture_output=True,
                        timeout=15,
                        check=True,
                    )
                    results.append(
                        ValidationCheckResult(
                            name="git_reachability",
                            passed=True,
                            message=f"Git repo reachable: {url}",
                        )
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    results.append(
                        ValidationCheckResult(
                            name="git_reachability",
                            passed=False,
                            message=(
                                f"Cannot reach git repo: {url}. "
                                "Check the URL, credentials, and network."
                            ),
                        )
                    )
        return results

    def _check_ssh_connectivity(self) -> list[ValidationCheckResult]:
        """Check SSH connectivity to configured hosts."""
        results: list[ValidationCheckResult] = []
        seen_hosts: set[str] = set()
        for name in self.config.list_fraises():
            for env_name in self.config.list_environments(name):
                env = self.config.get_environment(name, env_name) or {}
                host = env.get("ssh_host")
                if not host or host in seen_hosts:
                    continue
                seen_hosts.add(host)
                user = env.get("ssh_user", "fraisier")
                port = env.get("ssh_port", 22)
                try:
                    subprocess.run(
                        [
                            "ssh",
                            "-o",
                            "BatchMode=yes",
                            "-o",
                            "ConnectTimeout=5",
                            "-p",
                            str(port),
                            f"{user}@{host}",
                            "true",
                        ],
                        capture_output=True,
                        timeout=10,
                        check=True,
                    )
                    results.append(
                        ValidationCheckResult(
                            name="ssh_connectivity",
                            passed=True,
                            message=f"SSH to {user}@{host}:{port} OK",
                        )
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    results.append(
                        ValidationCheckResult(
                            name="ssh_connectivity",
                            passed=False,
                            message=(
                                f"Cannot SSH to {user}@{host}:{port}. "
                                "Check host, key, and firewall."
                            ),
                        )
                    )
        return results

    def _check_db_connectivity(self) -> list[ValidationCheckResult]:
        """Check database connectivity for fraises with database config."""
        results: list[ValidationCheckResult] = []
        for name in self.config.list_fraises():
            for env_name in self.config.list_environments(name):
                env = self.config.get_environment(name, env_name) or {}
                db = env.get("database")
                if not db:
                    continue
                db_host = db.get("host", "localhost")
                db_port = db.get("port", 5432)
                try:
                    subprocess.run(
                        ["pg_isready", "-h", str(db_host), "-p", str(db_port)],
                        capture_output=True,
                        timeout=10,
                        check=True,
                    )
                    results.append(
                        ValidationCheckResult(
                            name="db_connectivity",
                            passed=True,
                            message=f"DB at {db_host}:{db_port} OK",
                        )
                    )
                except (
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                    FileNotFoundError,
                ):
                    results.append(
                        ValidationCheckResult(
                            name="db_connectivity",
                            passed=False,
                            message=(
                                f"Cannot reach DB at {db_host}:{db_port}. "
                                "Check that PostgreSQL is running."
                            ),
                        )
                    )
        return results

    def _check_missing_health_checks(self) -> list[ValidationCheckResult]:
        """Warn when API fraises have no health check configured."""
        results: list[ValidationCheckResult] = []
        for name in self.config.list_fraises():
            fraise = self.config.get_fraise(name) or {}
            if fraise.get("type") != "api":
                continue
            for env_name in self.config.list_environments(name):
                env = self.config.get_environment(name, env_name) or {}
                if not env.get("health_check"):
                    results.append(
                        ValidationCheckResult(
                            name="missing_health_check",
                            passed=False,
                            message=(
                                f"Fraise '{name}' environment '{env_name}': "
                                "no health check configured. "
                                "Fix: add a health_check section with url and timeout"
                            ),
                            severity="warning",
                        )
                    )
        if not results:
            results.append(
                ValidationCheckResult(
                    name="missing_health_check", passed=True, severity="warning"
                )
            )
        return results


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


@dataclass
class DriftResult:
    """Result of checking a single file for drift."""

    name: str
    drifted: bool
    message: str = ""


def _hash_file(path: Path) -> str:
    """Compute sha256 hash of a file."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def detect_drift(
    output_dir: Path,
    template_hashes: dict[str, str],
    ignore: set[str] | None = None,
) -> list[DriftResult]:
    """Detect files that have drifted from their scaffolded templates.

    Args:
        output_dir: Directory containing generated files.
        template_hashes: Mapping of filename -> expected "sha256:..." hash.
        ignore: Set of filenames to skip (opt-out per file).

    Returns:
        List of DriftResult for files that have drifted.
    """
    ignore = ignore or set()
    drifted: list[DriftResult] = []

    for filename, expected_hash in template_hashes.items():
        if filename in ignore:
            continue

        file_path = output_dir / filename
        if not file_path.exists():
            drifted.append(
                DriftResult(
                    name=filename,
                    drifted=True,
                    message=f"Missing: {filename} not found in {output_dir}",
                )
            )
            continue

        actual_hash = _hash_file(file_path)
        if actual_hash != expected_hash:
            drifted.append(
                DriftResult(
                    name=filename,
                    drifted=True,
                    message=f"Modified: {filename} differs from template",
                )
            )

    return drifted


# ---------------------------------------------------------------------------
# Deployment readiness validation
# ---------------------------------------------------------------------------


class DeploymentReadinessValidator:
    """Validates a single fraise/environment pair is ready for deployment."""

    def __init__(self, fraise_config: dict[str, Any]):
        """Initialize with fraise+env config dict.

        Args:
            fraise_config: Merged config dict from get_fraise_environment(),
                includes fraise_name, environment, type, etc.
        """
        self.fraise_config = fraise_config
        self.fraise_name = fraise_config.get("fraise_name", "unknown")
        self.environment = fraise_config.get("environment", "unknown")

    def run_all(self) -> list[ValidationCheckResult]:
        """Run all deployment readiness checks."""
        results: list[ValidationCheckResult] = []

        checks = [
            self._check_config_accessible,
            self._check_git_repo_accessible,
            self._check_app_path_writable,
            self._check_database_config_complete,
            self._check_systemd_service_exists,
            self._check_wrapper_scripts_valid,
            self._check_sudoers_installed,
            self._check_health_check_reachable,
            self._check_install_command_available,
        ]

        for check in checks:
            result = check()
            if isinstance(result, list):
                results.extend(result)
            else:
                results.append(result)

        return results

    def _check_config_accessible(self) -> ValidationCheckResult:
        """Check that fraise_config was resolved and has required keys."""
        if not self.fraise_config:
            return ValidationCheckResult(
                name="config_accessible",
                passed=False,
                message=(
                    f"Fraise '{self.fraise_name}' environment '{self.environment}' "
                    "not found"
                ),
                severity="error",
            )

        required_keys = ["fraise_name", "environment", "app_path"]
        missing = [k for k in required_keys if k not in self.fraise_config]
        if missing:
            return ValidationCheckResult(
                name="config_accessible",
                passed=False,
                message=f"Config missing keys: {', '.join(missing)}",
                severity="error",
            )

        return ValidationCheckResult(name="config_accessible", passed=True)

    def _check_git_repo_accessible(self) -> ValidationCheckResult:
        """Check git clone_url or bare git_repo is accessible."""
        clone_url = self.fraise_config.get("clone_url")
        git_repo = self.fraise_config.get("git_repo")

        if not clone_url and not git_repo:
            # Neither is configured — skip
            return ValidationCheckResult(
                name="git_repo_accessible",
                passed=True,
                message="not configured (skipped)",
            )

        if clone_url:
            try:
                subprocess.run(
                    ["git", "ls-remote", "--exit-code", clone_url],
                    capture_output=True,
                    timeout=15,
                    check=True,
                )
                return ValidationCheckResult(
                    name="git_repo_accessible",
                    passed=True,
                    message=f"Reachable: {clone_url}",
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                return ValidationCheckResult(
                    name="git_repo_accessible",
                    passed=False,
                    message=(
                        f"Cannot reach git repo: {clone_url}. "
                        "Check the URL, credentials, and network."
                    ),
                    severity="error",
                )

        if git_repo:
            if Path(git_repo).is_dir():
                return ValidationCheckResult(
                    name="git_repo_accessible",
                    passed=True,
                    message=f"Bare repo exists: {git_repo}",
                )
            return ValidationCheckResult(
                name="git_repo_accessible",
                passed=False,
                message=(
                    f"Bare git repo directory not found: {git_repo}. "
                    f"Fix: sudo mkdir -p {git_repo} && "
                    f"sudo git init --bare {git_repo}"
                ),
                severity="error",
            )

        return ValidationCheckResult(
            name="git_repo_accessible", passed=True, message="not configured"
        )

    def _check_app_path_writable(self) -> ValidationCheckResult:
        """Check app_path exists and is writable."""
        app_path = self.fraise_config.get("app_path")
        if not app_path:
            return ValidationCheckResult(
                name="app_path_writable",
                passed=False,
                message="app_path not configured",
                severity="error",
            )

        app_dir = Path(app_path)
        if not app_dir.is_dir():
            return ValidationCheckResult(
                name="app_path_writable",
                passed=False,
                message=(
                    f"{app_path} does not exist. "
                    f"Fix: sudo mkdir -p {app_path} && "
                    f"sudo chown ${{deploy_user}} {app_path}"
                ),
                severity="error",
            )

        if not os.access(app_path, os.W_OK):
            return ValidationCheckResult(
                name="app_path_writable",
                passed=False,
                message=(
                    f"{app_path} is not writable. "
                    f"Fix: sudo chown ${{deploy_user}} {app_path}"
                ),
                severity="error",
            )

        return ValidationCheckResult(name="app_path_writable", passed=True)

    def _check_database_config_complete(self) -> ValidationCheckResult:
        """Check database config is present and complete if configured."""
        db_config = self.fraise_config.get("database")

        if not db_config:
            # Not configured — skip
            return ValidationCheckResult(
                name="database_config_complete",
                passed=True,
                message="not configured (skipped)",
            )

        required_db_fields = ["host", "dbname", "user", "strategy"]
        missing = [f for f in required_db_fields if not db_config.get(f)]

        if missing:
            return ValidationCheckResult(
                name="database_config_complete",
                passed=False,
                message=(
                    f"Database config incomplete. Missing: {', '.join(missing)}. "
                    f"Fix: add {', '.join(missing)} to database section"
                ),
                severity="error",
            )

        return ValidationCheckResult(name="database_config_complete", passed=True)

    def _check_systemd_service_exists(self) -> ValidationCheckResult:
        """Check systemd service is installed and active."""
        service_name = self.fraise_config.get("systemd_service")

        if not service_name:
            return ValidationCheckResult(
                name="systemd_service_exists",
                passed=True,
                message="not configured (skipped)",
            )

        try:
            result = subprocess.run(
                ["systemctl", "is-active", service_name],
                capture_output=True,
                timeout=5,
                check=False,
            )
            status = result.stdout.decode().strip()

            if status == "unknown":
                return ValidationCheckResult(
                    name="systemd_service_exists",
                    passed=False,
                    message=(
                        f"Service '{service_name}' unknown. "
                        f"Fix: ensure service unit file exists at "
                        f"/etc/systemd/system/{service_name}.service"
                    ),
                    severity="error",
                )

            if status != "active":
                return ValidationCheckResult(
                    name="systemd_service_exists",
                    passed=False,
                    message=(
                        f"Service '{service_name}' is {status}. "
                        f"Fix: sudo systemctl start {service_name}"
                    ),
                    severity="error",
                )

            return ValidationCheckResult(
                name="systemd_service_exists",
                passed=True,
                message=f"Service '{service_name}' is active",
            )

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ValidationCheckResult(
                name="systemd_service_exists",
                passed=False,
                message="systemctl command not available",
                severity="error",
            )

    def _check_wrapper_scripts_valid(self) -> ValidationCheckResult:
        """Check wrapper scripts (systemctl, pg) are present and executable."""
        systemctl_wrapper = os.environ.get("FRAISIER_SYSTEMCTL_WRAPPER")
        pg_wrapper = os.environ.get("FRAISIER_PG_WRAPPER")

        issues: list[str] = []

        if not systemctl_wrapper:
            issues.append(
                "FRAISIER_SYSTEMCTL_WRAPPER env var not set. "
                "Fix: export FRAISIER_SYSTEMCTL_WRAPPER=/path/to/systemctl-wrapper"
            )
        elif not Path(systemctl_wrapper).exists():
            issues.append(
                f"FRAISIER_SYSTEMCTL_WRAPPER file not found: {systemctl_wrapper}"
            )
        elif not os.access(systemctl_wrapper, os.X_OK):
            issues.append(
                f"FRAISIER_SYSTEMCTL_WRAPPER not executable: {systemctl_wrapper}. "
                f"Fix: chmod 755 {systemctl_wrapper}"
            )

        if not pg_wrapper:
            issues.append(
                "FRAISIER_PG_WRAPPER env var not set. "
                "Fix: export FRAISIER_PG_WRAPPER=/path/to/pg-wrapper"
            )
        elif not Path(pg_wrapper).exists():
            issues.append(f"FRAISIER_PG_WRAPPER file not found: {pg_wrapper}")
        elif not os.access(pg_wrapper, os.X_OK):
            issues.append(
                f"FRAISIER_PG_WRAPPER not executable: {pg_wrapper}. "
                f"Fix: chmod 755 {pg_wrapper}"
            )

        if issues:
            return ValidationCheckResult(
                name="wrapper_scripts_valid",
                passed=False,
                message=" — ".join(issues),
                severity="error",
            )

        return ValidationCheckResult(name="wrapper_scripts_valid", passed=True)

    def _check_sudoers_installed(self) -> ValidationCheckResult:
        """Check sudoers file is installed for this project."""
        project_name = self.fraise_name
        sudoers_path = Path(f"/etc/sudoers.d/{project_name}")

        try:
            sudoers_exists = sudoers_path.exists()
        except PermissionError:
            # Can't check due to permissions; treat as missing
            sudoers_exists = False

        if not sudoers_exists:
            return ValidationCheckResult(
                name="sudoers_installed",
                passed=False,
                message=(
                    f"Sudoers file not installed: {sudoers_path}. "
                    f"Fix: run 'fraisier scaffold-install' to install sudo rules"
                ),
                severity="warning",
            )

        return ValidationCheckResult(
            name="sudoers_installed",
            passed=True,
            message=f"Sudoers installed: {sudoers_path}",
        )

    def _check_health_check_reachable(self) -> ValidationCheckResult:
        """Check health check endpoint responds (no retries)."""
        from fraisier.health_check import HTTPHealthChecker

        health_config = self.fraise_config.get("health_check")

        if not health_config:
            return ValidationCheckResult(
                name="health_check_reachable",
                passed=True,
                message="not configured (skipped)",
            )

        url = health_config.get("url")
        if not url:
            return ValidationCheckResult(
                name="health_check_reachable",
                passed=True,
                message="URL not configured (skipped)",
            )

        checker = HTTPHealthChecker(url)
        result = checker.check(timeout=3.0)

        if result.success:
            return ValidationCheckResult(
                name="health_check_reachable",
                passed=True,
                message=f"Healthy: {result.message}",
            )
        else:
            return ValidationCheckResult(
                name="health_check_reachable",
                passed=False,
                message=(
                    f"Health check failed: {result.message or 'no response'}. "
                    f"Fix: ensure {url} is reachable and responding"
                ),
                severity="error",
            )

    def _check_install_command_available(self) -> ValidationCheckResult:
        """Check install command binary is available."""
        import shutil

        install_config = self.fraise_config.get("install")

        if not install_config:
            return ValidationCheckResult(
                name="install_command_available",
                passed=True,
                message="not configured (skipped)",
            )

        command = install_config.get("command")
        if not command:
            return ValidationCheckResult(
                name="install_command_available",
                passed=True,
                message="command not configured (skipped)",
            )

        # Extract first token (the command itself)
        command_parts = command.split()
        if not command_parts:
            return ValidationCheckResult(
                name="install_command_available",
                passed=False,
                message="install command is empty",
                severity="error",
            )

        command_name = command_parts[0]

        if shutil.which(command_name):
            return ValidationCheckResult(
                name="install_command_available",
                passed=True,
                message=f"Command available: {command_name}",
            )
        else:
            return ValidationCheckResult(
                name="install_command_available",
                passed=False,
                message=(
                    f"Install command not found: {command_name}. "
                    f"Fix: install {command_name} or update install.command"
                ),
                severity="error",
            )
