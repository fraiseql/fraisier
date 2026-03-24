"""Configuration loader for Fraisier deployment system.

Loads fraise definitions from fraises.yaml.
Supports hierarchical fraise -> environment structure.
"""

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from fraisier.errors import ValidationError

_VALID_STRATEGIES = {"rebuild", "restore_migrate", "migrate"}
_DEFAULT_TIMEOUT = 600  # 10 minutes


@dataclass
class SystemdScaffoldConfig:
    """Systemd scaffold options."""

    security_hardening: bool = True
    memory_max_default: str = "4G"


@dataclass
class NginxScaffoldConfig:
    """Nginx scaffold options."""

    ssl_provider: str = "letsencrypt"
    cors_origins: list[str] = field(default_factory=list)
    rate_limit: str = "10r/s"
    restricted_paths: list[str] = field(default_factory=list)


@dataclass
class GithubActionsScaffoldConfig:
    """GitHub Actions scaffold options."""

    python_versions: list[str] = field(default_factory=lambda: ["3.12"])
    test_command: str = "uv run pytest"
    lint_command: str = "uv run ruff check"
    format_command: str = "uv run ruff format --check"


@dataclass
class ScaffoldConfig:
    """Parsed scaffold: section from fraises.yaml."""

    output_dir: str = "scripts/generated"
    deploy_user: str = "fraisier"
    systemd: SystemdScaffoldConfig = field(default_factory=SystemdScaffoldConfig)
    nginx: NginxScaffoldConfig = field(default_factory=NginxScaffoldConfig)
    github_actions: GithubActionsScaffoldConfig = field(
        default_factory=GithubActionsScaffoldConfig
    )


@dataclass
class DeploymentConfig:
    """Parsed deployment: section from fraises.yaml."""

    lock_dir: str = "/run/fraisier"
    status_file: str = "deployment_status.json"
    webhook_secret_env: str = "DEPLOYMENT_TOKEN"
    poll_interval_seconds: int = 60
    deploy_user: str = "fraisier"
    strategies: dict[str, str] = field(default_factory=dict)
    timeouts: dict[str, int] = field(default_factory=dict)

    def get_strategy(self, environment: str) -> str | None:
        """Get deployment strategy for an environment."""
        return self.strategies.get(environment)

    def get_timeout(self, environment: str) -> int:
        """Get timeout for an environment, with fallback to default."""
        return self.timeouts.get(environment, _DEFAULT_TIMEOUT)


@dataclass
class HealthResponseConfig:
    """Security omission rules for health response."""

    include_version: bool = True
    include_schema_hash: bool = True
    include_response_time: bool = True
    include_database: bool = False
    include_environment: bool = False
    include_commit: bool = False


@dataclass
class HealthConfig:
    """Parsed health: section from fraises.yaml."""

    startup_timeout_seconds: int = 120
    deploy_poll_interval_seconds: int = 5
    endpoints: list[str] = field(default_factory=lambda: ["/health"])
    response: HealthResponseConfig = field(default_factory=HealthResponseConfig)


class FraisierConfig:
    """Load and manage deployment configuration from fraises.yaml.

    Supports hierarchical structure:
        fraises:
          <fraise_name>:
            type: api|etl|scheduled|backup
            environments:
              <env_name>:
                <config>
    """

    def __init__(self, config_path: Path | str | None = None):
        """Initialize configuration.

        Args:
            config_path: Path to fraises.yaml. If None, uses default locations.
        """
        self.config_path = self._resolve_config_path(config_path)
        self._config: dict[str, Any] = {}
        self._load()

    def _resolve_config_path(self, config_path: Path | str | None) -> Path:
        """Resolve configuration file path."""
        if config_path:
            return Path(config_path)

        # Check standard locations
        locations = [
            Path("/opt/fraisier/fraises.yaml"),
            Path.cwd() / "fraises.yaml",
            Path.cwd() / "config" / "fraises.yaml",
            Path(__file__).parent.parent / "fraises.yaml",
        ]

        for loc in locations:
            if loc.exists():
                return loc

        raise FileNotFoundError(
            f"fraises.yaml not found in any of: {[str(p) for p in locations]}"
        )

    def _load(self) -> None:
        """Load configuration from YAML file."""
        with Path(self.config_path).open() as f:
            self._config = yaml.safe_load(f)

    def reload(self) -> None:
        """Reload configuration from file."""
        self._load()

    @property
    def deployment(self) -> DeploymentConfig:
        """Get parsed deployment configuration with validation."""
        raw = self._config.get("deployment", {}) or {}
        strategies = raw.get("strategies", {}) or {}
        for env, strat in strategies.items():
            if strat not in _VALID_STRATEGIES:
                valid = ", ".join(sorted(_VALID_STRATEGIES))
                raise ValidationError(
                    f"Invalid strategy '{strat}' for {env}. Valid: {valid}",
                )
        return DeploymentConfig(
            lock_dir=raw.get("lock_dir", "/run/fraisier"),
            status_file=raw.get("status_file", "deployment_status.json"),
            webhook_secret_env=raw.get("webhook_secret_env", "DEPLOYMENT_TOKEN"),
            poll_interval_seconds=raw.get("poll_interval_seconds", 60),
            deploy_user=raw.get("deploy_user", "fraisier"),
            strategies=strategies,
            timeouts=raw.get("timeouts", {}) or {},
        )

    @property
    def scaffold(self) -> ScaffoldConfig:
        """Get parsed scaffold configuration with defaults."""
        raw = self._config.get("scaffold", {}) or {}
        raw_systemd = raw.get("systemd", {}) or {}
        raw_nginx = raw.get("nginx", {}) or {}
        raw_gh = raw.get("github_actions", {}) or {}

        # Fallback deploy_user: scaffold -> deployment -> "fraisier"
        deploy_user = raw.get("deploy_user")
        if not deploy_user:
            dep_raw = self._config.get("deployment", {}) or {}
            deploy_user = dep_raw.get("deploy_user", "fraisier")

        return ScaffoldConfig(
            output_dir=raw.get("output_dir", "scripts/generated"),
            deploy_user=deploy_user,
            systemd=SystemdScaffoldConfig(
                security_hardening=raw_systemd.get("security_hardening", True),
                memory_max_default=raw_systemd.get("memory_max_default", "4G"),
            ),
            nginx=NginxScaffoldConfig(
                ssl_provider=raw_nginx.get("ssl_provider", "letsencrypt"),
                cors_origins=raw_nginx.get("cors_origins", []),
                rate_limit=raw_nginx.get("rate_limit", "10r/s"),
                restricted_paths=raw_nginx.get("restricted_paths", []),
            ),
            github_actions=GithubActionsScaffoldConfig(
                python_versions=raw_gh.get("python_versions", ["3.12"]),
                test_command=raw_gh.get("test_command", "uv run pytest"),
                lint_command=raw_gh.get("lint_command", "uv run ruff check"),
                format_command=raw_gh.get(
                    "format_command", "uv run ruff format --check"
                ),
            ),
        )

    @property
    def health(self) -> HealthConfig:
        """Get parsed health configuration with defaults."""
        raw = self._config.get("health", {}) or {}
        raw_response = raw.get("response", {}) or {}
        return HealthConfig(
            startup_timeout_seconds=raw.get("startup_timeout_seconds", 120),
            deploy_poll_interval_seconds=raw.get("deploy_poll_interval_seconds", 5),
            endpoints=raw.get("endpoints", ["/health"]),
            response=HealthResponseConfig(
                include_version=raw_response.get("include_version", True),
                include_schema_hash=raw_response.get("include_schema_hash", True),
                include_response_time=raw_response.get("include_response_time", True),
                include_database=raw_response.get("include_database", False),
                include_environment=raw_response.get("include_environment", False),
                include_commit=raw_response.get("include_commit", False),
            ),
        )

    @property
    def fraises(self) -> dict[str, dict[str, Any]]:
        """Get all fraise configurations."""
        return self._config.get("fraises", {})

    @property
    def environments(self) -> dict[str, dict[str, Any]]:
        """Get global environment configurations."""
        return self._config.get("environments", {})

    @property
    def branch_mapping(self) -> dict[str, dict[str, str]]:
        """Get branch to fraise/environment mapping."""
        return self._config.get("branch_mapping", {})

    def get_fraise(self, fraise_name: str) -> dict[str, Any] | None:
        """Get configuration for a fraise (all environments)."""
        return self.fraises.get(fraise_name)

    def get_fraise_environment(
        self, fraise_name: str, environment: str
    ) -> dict[str, Any] | None:
        """Get configuration for a specific fraise + environment.

        Args:
            fraise_name: e.g., "my_api", "etl", "backup"
            environment: e.g., "development", "staging", "production"

        Returns:
            Merged config with fraise-level and environment-level settings
        """
        fraise = self.fraises.get(fraise_name)
        if not fraise:
            return None

        env_config = fraise.get("environments", {}).get(environment)
        if not env_config:
            return None

        # Merge fraise-level config with environment-specific config
        return {
            "fraise_name": fraise_name,
            "environment": environment,
            "type": fraise.get("type"),
            "description": fraise.get("description"),
            **env_config,
        }

    def get_fraise_for_branch(self, branch: str) -> dict[str, Any] | None:
        """Get fraise configuration for a git branch (webhook routing).

        Args:
            branch: Git branch name (e.g., "dev", "main")

        Returns:
            Full fraise+environment config for the branch
        """
        mapping = self.branch_mapping.get(branch)
        if not mapping:
            return None

        fraise_name = mapping.get("fraise")
        environment = mapping.get("environment")

        if not fraise_name or not environment:
            return None

        return self.get_fraise_environment(fraise_name, environment)

    def list_fraises(self) -> list[str]:
        """List all fraise names.

        Returns:
            List of fraise name strings
        """
        return list(self.fraises.keys())

    def list_all_deployments(self) -> list[dict[str, Any]]:
        """List all fraise+environment combinations (deployable targets).

        Returns:
            List of all deployable targets
        """
        result = []
        for fraise_name, fraise in self.fraises.items():
            fraise_type = fraise.get("type", "unknown")
            description = fraise.get("description", "")

            for env_name, env_config in fraise.get("environments", {}).items():
                # Handle fraises with nested jobs (backup, statistics)
                if "jobs" in env_config:
                    for job_name, job_config in env_config["jobs"].items():
                        result.append(
                            {
                                "fraise": fraise_name,
                                "environment": env_name,
                                "job": job_name,
                                "type": fraise_type,
                                "name": job_config.get("name", job_name),
                                "description": job_config.get(
                                    "description", description
                                ),
                            }
                        )
                else:
                    result.append(
                        {
                            "fraise": fraise_name,
                            "environment": env_name,
                            "job": None,
                            "type": fraise_type,
                            "name": env_config.get("name", fraise_name),
                            "description": description,
                        }
                    )
        return result

    def get_deployments_by_type(self, fraise_type: str) -> list[dict[str, Any]]:
        """Get all deployments of a specific type."""
        return [d for d in self.list_all_deployments() if d["type"] == fraise_type]

    def get_deployments_by_environment(self, environment: str) -> list[dict[str, Any]]:
        """Get all deployments for a specific environment."""
        return [
            d for d in self.list_all_deployments() if d["environment"] == environment
        ]

    def get_environment(
        self, fraise_name: str, environment: str
    ) -> dict[str, Any] | None:
        """Get environment config for a fraise. Alias for get_fraise_environment."""
        return self.get_fraise_environment(fraise_name, environment)

    def get_git_provider_config(self) -> dict[str, Any]:
        """Get git provider configuration."""
        return self._config.get("git", {})

    def list_environments(self, fraise_name: str) -> list[str]:
        """List environment names for a fraise."""
        fraise = self.fraises.get(fraise_name)
        if not fraise:
            return []
        return list(fraise.get("environments", {}).keys())

    def list_fraises_detailed(self) -> list[dict[str, Any]]:
        """List all fraises with detailed info (type, description, environments)."""
        result = []
        for fraise_name, fraise in self.fraises.items():
            environments = list(fraise.get("environments", {}).keys())
            result.append(
                {
                    "name": fraise_name,
                    "type": fraise.get("type", "unknown"),
                    "description": fraise.get("description", ""),
                    "environments": environments,
                }
            )
        return result


# Global config instance (lazy loaded, thread-safe)
_config: FraisierConfig | None = None
_config_lock = threading.Lock()


def get_config(config_path: Path | str | None = None) -> FraisierConfig:
    """Get or create global configuration instance."""
    global _config
    if _config is None or config_path:
        with _config_lock:
            if _config is None or config_path:
                _config = FraisierConfig(config_path)
    return _config
