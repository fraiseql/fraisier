"""Pre-deploy validation checks and drift detection.

Provides a registry of validation checks that verify the project
is ready for deployment: config validity, provider availability,
deploy user existence, etc.

Also provides drift detection for scaffolded files.
"""

import hashlib
import pwd
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fraisier.config import FraisierConfig

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

    def _check_deploy_user(self) -> ValidationCheckResult:
        """Check that the configured deploy user exists on the system."""
        deploy_user = self.config.deployment.deploy_user
        try:
            pwd.getpwnam(deploy_user)
            return ValidationCheckResult(
                name="deploy_user", passed=True, severity="error"
            )
        except KeyError:
            return ValidationCheckResult(
                name="deploy_user",
                passed=False,
                message=(
                    f"User '{deploy_user}' does not exist. "
                    f"Fix: sudo useradd -r -s /bin/bash {deploy_user}"
                ),
                severity="error",
            )

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
