"""Tests for RemoteDeploymentValidator manifest-driven checks."""

from pathlib import Path
from unittest.mock import MagicMock

from fraisier.config import FraisierConfig
from fraisier.manifest import ManagedPath, PathManifest
from fraisier.remote_validator import RemoteDeploymentValidator
from fraisier.validation import ValidationCheckResult


class TestCheckManifestPaths:
    """RemoteDeploymentValidator.check_manifest_paths validates paths from manifest."""

    def _make_validator(self, config_dict: dict, mock_runner: MagicMock | None = None):
        """Helper to create a RemoteDeploymentValidator."""
        import tempfile

        import yaml

        # Write config to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_dict, f)
            config_file = f.name

        config = FraisierConfig(config_file)
        fraise_config = config.get_fraise_environment("api", "prod")
        runner = mock_runner or MagicMock()
        return RemoteDeploymentValidator(fraise_config, runner, config)

    def test_check_manifest_paths_returns_list(self):
        """check_manifest_paths returns a list of ValidationCheckResult."""
        config = {
            "name": "myapp",
            "fraises": {
                "api": {
                    "type": "api",
                    "environments": {
                        "prod": {
                            "app_path": "/var/www/app",
                            "git_repo": "/var/repos/app.git",
                        }
                    },
                }
            },
        }
        validator = self._make_validator(config)
        manifest = PathManifest(
            global_paths=(
                ManagedPath(
                    path=Path("/opt/fraisier"),
                    owner="fraisier",
                    group="fraisier",
                    mode=0o755,
                    read_write_units=(),
                ),
            ),
            env_paths=(),
        )

        results = validator.check_manifest_paths(manifest)

        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, ValidationCheckResult) for r in results)

    def test_check_manifest_paths_skips_non_create_paths(self):
        """Paths with create_if_missing=False are skipped."""
        config = {
            "name": "myapp",
            "fraises": {
                "api": {
                    "type": "api",
                    "environments": {
                        "prod": {
                            "app_path": "/var/www/app",
                        }
                    },
                }
            },
        }
        validator = self._make_validator(config)
        manifest = PathManifest(
            global_paths=(
                ManagedPath(
                    path=Path("/run/fraisier"),
                    owner="fraisier",
                    group="fraisier",
                    mode=0o755,
                    read_write_units=(),
                    create_if_missing=False,  # systemd-managed, skip
                ),
            ),
            env_paths=(),
        )

        results = validator.check_manifest_paths(manifest)

        # Should skip the non-creatable path
        assert len(results) == 0

    def test_check_manifest_paths_correct_owner(self):
        """Path with correct owner returns passed=True."""
        config = {
            "name": "myapp",
            "fraises": {
                "api": {
                    "type": "api",
                    "environments": {
                        "prod": {
                            "app_path": "/var/www/app",
                        }
                    },
                }
            },
        }
        runner = MagicMock()
        # stat returns "fraisier fraisier" for the path
        runner.run.return_value = MagicMock(
            returncode=0, stdout="fraisier fraisier\n", stderr=""
        )

        validator = self._make_validator(config, runner)
        manifest = PathManifest(
            global_paths=(),
            env_paths=(
                ManagedPath(
                    path=Path("/var/www/app"),
                    owner="fraisier",
                    group="fraisier",
                    mode=0o755,
                    read_write_units=(),
                    create_if_missing=True,
                ),
            ),
        )

        results = validator.check_manifest_paths(manifest)

        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].fix_command is None

    def test_check_manifest_paths_missing_path(self):
        """Missing path returns passed=False with mkdir+chown fix_command."""
        config = {
            "name": "myapp",
            "fraises": {
                "api": {
                    "type": "api",
                    "environments": {
                        "prod": {
                            "app_path": "/var/www/app",
                        }
                    },
                }
            },
        }
        runner = MagicMock()
        # stat fails (path doesn't exist)
        runner.run.return_value = MagicMock(returncode=1, stdout="", stderr="")

        validator = self._make_validator(config, runner)
        manifest = PathManifest(
            global_paths=(),
            env_paths=(
                ManagedPath(
                    path=Path("/var/www/app"),
                    owner="fraisier",
                    group="fraisier",
                    mode=0o755,
                    read_write_units=(),
                    create_if_missing=True,
                ),
            ),
        )

        results = validator.check_manifest_paths(manifest)

        assert len(results) == 1
        assert results[0].passed is False
        assert "does not exist" in results[0].message
        assert results[0].fix_command is not None
        assert "mkdir -p" in results[0].fix_command
        assert "chown fraisier:fraisier" in results[0].fix_command
        has_chmod = "chmod 0755" in results[0].fix_command
        has_chmod_alt = "chmod 755" in results[0].fix_command
        assert has_chmod or has_chmod_alt

    def test_check_manifest_paths_wrong_owner(self):
        """Path with wrong owner returns passed=False with chown fix_command."""
        config = {
            "name": "myapp",
            "fraises": {
                "api": {
                    "type": "api",
                    "environments": {
                        "prod": {
                            "app_path": "/var/www/app",
                        }
                    },
                }
            },
        }
        runner = MagicMock()
        # stat returns "root root" (wrong owner)
        runner.run.return_value = MagicMock(
            returncode=0, stdout="root root\n", stderr=""
        )

        validator = self._make_validator(config, runner)
        manifest = PathManifest(
            global_paths=(),
            env_paths=(
                ManagedPath(
                    path=Path("/var/www/app"),
                    owner="fraisier",
                    group="fraisier",
                    mode=0o755,
                    read_write_units=(),
                    create_if_missing=True,
                ),
            ),
        )

        results = validator.check_manifest_paths(manifest)

        assert len(results) == 1
        assert results[0].passed is False
        assert "owned by root:root" in results[0].message
        assert "expected fraisier:fraisier" in results[0].message
        assert results[0].fix_command is not None
        assert "chown fraisier:fraisier" in results[0].fix_command
        assert "mkdir" not in results[0].fix_command  # Just chown, not mkdir

    def test_check_manifest_paths_multiple_paths(self):
        """Multiple paths are checked and results are returned for each."""
        config = {
            "name": "myapp",
            "fraises": {
                "api": {
                    "type": "api",
                    "environments": {
                        "prod": {
                            "app_path": "/var/www/app",
                            "git_repo": "/var/repos/app.git",
                        }
                    },
                }
            },
        }
        runner = MagicMock()

        def run_side_effect(cmd, **kwargs):
            # Different outcomes for different paths
            if "/var/www/app" in str(cmd):
                return MagicMock(returncode=0, stdout="fraisier fraisier\n", stderr="")
            elif "/var/repos/app.git" in str(cmd):
                return MagicMock(returncode=1, stdout="", stderr="")  # missing
            return MagicMock(returncode=0, stdout="", stderr="")

        runner.run.side_effect = run_side_effect

        validator = self._make_validator(config, runner)
        manifest = PathManifest(
            global_paths=(),
            env_paths=(
                ManagedPath(
                    path=Path("/var/www/app"),
                    owner="fraisier",
                    group="fraisier",
                    mode=0o755,
                    read_write_units=(),
                    create_if_missing=True,
                ),
                ManagedPath(
                    path=Path("/var/repos/app.git"),
                    owner="fraisier",
                    group="fraisier",
                    mode=0o755,
                    read_write_units=(),
                    create_if_missing=True,
                ),
            ),
        )

        results = validator.check_manifest_paths(manifest)

        assert len(results) == 2
        # /var/www/app is correct
        assert [r for r in results if r.passed and "/var/www/app" in r.name]
        # /var/repos/app.git is missing
        assert [r for r in results if not r.passed and "/var/repos/app.git" in r.name]
