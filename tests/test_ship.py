"""Tests for fraisier ship command."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import yaml
from click.testing import CliRunner

from fraisier.cli import main


def _setup_project(tmp_path, version="1.0.0", webhook_secret=None):
    """Create version.json, pyproject.toml, and fraises.yaml."""
    vj = tmp_path / "version.json"
    vj.write_text(
        json.dumps(
            {
                "version": version,
                "commit": "abc123",
                "branch": "main",
                "timestamp": "2026-03-23T12:00:00Z",
                "schema_hash": "",
                "environment": "",
                "database_version": "",
            },
            indent=2,
        )
    )

    pp = tmp_path / "pyproject.toml"
    pp.write_text(f'[project]\nname = "myapp"\nversion = "{version}"\n')

    config = {
        "fraises": {
            "my_api": {
                "type": "api",
                "environments": {
                    "production": {
                        "name": "my-api",
                        "app_path": "/var/www/my-api",
                    },
                },
            },
        },
    }
    if webhook_secret:
        config["git"] = {
            "provider": "github",
            "github": {"webhook_secret": webhook_secret},
        }

    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(yaml.dump(config))
    return str(cfg)


class TestShipCommand:
    """Test fraisier ship bumps, commits, and pushes."""

    @patch("subprocess.run")
    def test_ship_patch_bumps_version(self, mock_run, tmp_path):
        """Test ship patch bumps version from 1.0.0 to 1.0.1."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        assert "1.0.1" in result.output

    @patch("subprocess.run")
    def test_ship_creates_git_commit(self, mock_run, tmp_path):
        """Test ship creates a git commit with version message."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )

        # Should have called git add --update and git commit
        calls = mock_run.call_args_list
        commands = [c[0][0] for c in calls]
        assert any(cmd == ["git", "add", "--update"] for cmd in commands)
        assert any("git" in str(cmd) and "commit" in str(cmd) for cmd in commands)

    @patch("subprocess.run")
    def test_ship_pushes_to_remote(self, mock_run, tmp_path):
        """Test ship pushes to git remote."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )

        calls = mock_run.call_args_list
        commands = [c[0][0] for c in calls]
        assert any("git" in str(cmd) and "push" in str(cmd) for cmd in commands)

    @patch("subprocess.run")
    def test_ship_retries_commit_on_precommit_failure(self, mock_run, tmp_path):
        """Test ship retries commit when pre-commit hooks modify files."""
        cfg = _setup_project(tmp_path)

        # First commit fails (pre-commit modified files), retry succeeds
        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "commit"] and not hasattr(side_effect, "retried"):
                side_effect.retried = True
                raise subprocess.CalledProcessError(1, cmd)
            # git diff --quiet returns 1 = dirty (files were modified by hook)
            if cmd == ["git", "diff", "--quiet"]:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        assert "Pre-commit hooks modified files" in result.output

        # Should have called git add --update twice (initial + retry)
        add_calls = [
            c for c in mock_run.call_args_list if c[0][0] == ["git", "add", "--update"]
        ]
        assert len(add_calls) == 2

    @patch("subprocess.run")
    def test_ship_raises_if_commit_fails_without_dirty_files(self, mock_run, tmp_path):
        """Test ship raises if commit fails and no files were modified by hooks."""
        cfg = _setup_project(tmp_path)

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "commit"]:
                raise subprocess.CalledProcessError(1, cmd)
            # git diff --quiet returns 0 = clean (no hook modifications)
            if cmd == ["git", "diff", "--quiet"]:
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code != 0
        assert "Pre-commit hooks modified files" not in result.output

    @patch("subprocess.run")
    def test_ship_raises_if_retry_also_fails(self, mock_run, tmp_path):
        """Test ship raises if commit fails even after retry."""
        cfg = _setup_project(tmp_path)

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["git", "commit"]:
                raise subprocess.CalledProcessError(1, cmd)
            # git diff --quiet returns 1 = dirty (hooks did modify files)
            if cmd == ["git", "diff", "--quiet"]:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code != 0


def _setup_project_with_pipeline(tmp_path, version="1.0.0"):
    """Create project with ship pipeline config."""
    vj = tmp_path / "version.json"
    vj.write_text(
        json.dumps(
            {
                "version": version,
                "commit": "abc123",
                "branch": "main",
                "timestamp": "2026-03-23T12:00:00Z",
                "schema_hash": "",
                "environment": "",
                "database_version": "",
            },
            indent=2,
        )
    )

    pp = tmp_path / "pyproject.toml"
    pp.write_text(f'[project]\nname = "myapp"\nversion = "{version}"\n')

    config = {
        "fraises": {
            "my_api": {
                "type": "api",
                "environments": {
                    "production": {
                        "name": "my-api",
                        "app_path": "/var/www/my-api",
                    },
                },
            },
        },
        "ship": {
            "pr_base": "dev",
            "checks": [
                {
                    "name": "ruff-fix",
                    "command": ["echo", "ruff"],
                    "phase": "fix",
                },
                {
                    "name": "pytest",
                    "command": ["echo", "tests"],
                    "phase": "test",
                    "timeout": 120,
                },
            ],
        },
    }

    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(yaml.dump(config))
    return str(cfg)


class TestShipPipelineIntegration:
    """Test ship with pipeline config."""

    @patch("subprocess.run")
    @patch("fraisier.ship.pipeline.run_check")
    def test_ship_uses_no_verify_with_pipeline(self, mock_check, mock_run, tmp_path):
        """Pipeline path uses --no-verify on commit."""
        mock_run.return_value = MagicMock(returncode=0)
        from fraisier.ship.checks import CheckResult

        mock_check.return_value = CheckResult(
            name="test", success=True, output="", duration_seconds=0.1
        )
        cfg = _setup_project_with_pipeline(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0, result.output
        calls = mock_run.call_args_list
        commit_calls = [c[0][0] for c in calls if "commit" in str(c[0][0])]
        assert any("--no-verify" in cmd for cmd in commit_calls)

    @patch("subprocess.run")
    def test_ship_backward_compat_no_pipeline(self, mock_run, tmp_path):
        """Without ship config, uses legacy path (no --no-verify)."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        calls = mock_run.call_args_list
        commit_calls = [c[0][0] for c in calls if "commit" in str(c[0][0])]
        assert not any("--no-verify" in cmd for cmd in commit_calls)

    @patch("subprocess.run")
    @patch("fraisier.ship.pipeline.run_check")
    def test_ship_skip_checks_bypasses_pipeline(self, mock_check, mock_run, tmp_path):
        """--skip-checks skips pipeline even when configured."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project_with_pipeline(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--no-deploy",
                "--skip-checks",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        mock_check.assert_not_called()

    @patch("subprocess.run")
    @patch("fraisier.ship.pipeline.run_check")
    def test_ship_fix_failure_aborts(self, mock_check, mock_run, tmp_path):
        """Fix phase failure aborts ship."""
        mock_run.return_value = MagicMock(returncode=0)
        from fraisier.ship.checks import CheckResult

        mock_check.return_value = CheckResult(
            name="ruff-fix",
            success=False,
            output="lint error",
            duration_seconds=0.1,
        )
        cfg = _setup_project_with_pipeline(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code != 0
        assert "Fix checks failed" in result.output

    @patch("subprocess.run")
    @patch("fraisier.ship.pr.create_pr")
    @patch("fraisier.ship.pipeline.run_check")
    def test_ship_pr_flag_creates_pr(
        self, mock_check, mock_create_pr, mock_run, tmp_path
    ):
        """--pr creates a PR after push."""
        mock_run.return_value = MagicMock(returncode=0)
        from fraisier.ship.checks import CheckResult

        mock_check.return_value = CheckResult(
            name="test", success=True, output="", duration_seconds=0.1
        )
        mock_create_pr.return_value = "https://github.com/test/pr/1"
        cfg = _setup_project_with_pipeline(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--no-deploy",
                "--pr",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        mock_create_pr.assert_called_once()

    def test_dry_run_shows_pipeline_checks(self, tmp_path):
        """--dry-run lists pipeline checks."""
        cfg = _setup_project_with_pipeline(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--dry-run",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        assert "Pipeline checks" in result.output
        assert "ruff-fix" in result.output
        assert "pytest" in result.output


class TestShipDryRun:
    """Test fraisier ship --dry-run."""

    def test_dry_run_shows_plan(self, tmp_path):
        """Test --dry-run shows what would happen without executing."""
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--dry-run",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        assert "1.0.1" in result.output
        assert "dry" in result.output.lower()

    def test_dry_run_does_not_modify_files(self, tmp_path):
        """Test --dry-run leaves version files unchanged."""
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--dry-run",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )

        # version.json should still be 1.0.0
        data = json.loads((tmp_path / "version.json").read_text())
        assert data["version"] == "1.0.0"

    @patch("subprocess.run")
    def test_dry_run_does_not_call_git(self, mock_run, tmp_path):
        """Test --dry-run does not invoke any git commands."""
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--dry-run",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        mock_run.assert_not_called()


class TestShipBumpTypes:
    """Test all bump types work."""

    @patch("subprocess.run")
    def test_ship_minor(self, mock_run, tmp_path):
        """Test ship minor bumps 1.0.0 -> 1.1.0."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "minor",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert "1.1.0" in result.output

    @patch("subprocess.run")
    def test_ship_major(self, mock_run, tmp_path):
        """Test ship major bumps 1.0.0 -> 2.0.0."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "major",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert "2.0.0" in result.output


class TestShipNoBump:
    """Test fraisier ship --no-bump."""

    @patch("subprocess.run")
    def test_ship_no_bump_skips_version_change(self, mock_run, tmp_path):
        """--no-bump leaves version.json unchanged."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path, version="1.2.3")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "--no-bump",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0

        # version.json should still be 1.2.3
        data = json.loads((tmp_path / "version.json").read_text())
        assert data["version"] == "1.2.3"

    @patch("subprocess.run")
    def test_ship_no_bump_commits_with_current_version(self, mock_run, tmp_path):
        """--no-bump commits with the current version in the message."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path, version="2.0.0")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "--no-bump",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0

        calls = mock_run.call_args_list
        commit_calls = [c for c in calls if "commit" in str(c[0][0])]
        assert len(commit_calls) >= 1
        commit_msg = str(commit_calls[0])
        assert "v2.0.0" in commit_msg

    @patch("subprocess.run")
    def test_ship_no_bump_still_pushes(self, mock_run, tmp_path):
        """--no-bump still stages, commits, and pushes."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "--no-bump",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        calls = mock_run.call_args_list
        commands = [c[0][0] for c in calls]
        assert any(cmd == ["git", "add", "--update"] for cmd in commands)
        assert any("push" in str(cmd) for cmd in commands)

    def test_ship_no_bump_dry_run(self, tmp_path):
        """--no-bump --dry-run shows plan without version bump."""
        cfg = _setup_project(tmp_path, version="1.0.0")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "--no-bump",
                "--dry-run",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        assert "no bump" in result.output.lower() or "1.0.0" in result.output

    def test_ship_bump_type_required_without_no_bump(self, tmp_path):
        """bump_type is required when --no-bump is not set."""
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
            ],
        )
        assert result.exit_code != 0

    def test_ship_no_bump_with_bump_type_is_error(self, tmp_path):
        """Passing both --no-bump and a bump type is an error."""
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--no-bump",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
            ],
        )
        assert result.exit_code != 0


class TestShipDeploy:
    """Test ship triggers deploy after push."""

    @patch("subprocess.run")
    def test_ship_no_deploy_skips_deploy(self, mock_run, tmp_path):
        """ship --no-deploy does not trigger deployment."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--no-deploy",
                "--version-file",
                str(tmp_path / "version.json"),
                "--pyproject",
                str(tmp_path / "pyproject.toml"),
            ],
        )
        assert result.exit_code == 0
        assert "Shipped" in result.output
        # No deploy output
        assert "Deploying" not in result.output

    @patch("subprocess.run")
    def test_ship_triggers_deploy_for_mapped_branch(self, mock_run, tmp_path):
        """ship triggers deploy when branch has a mapped fraise."""
        mock_run.return_value = MagicMock(returncode=0, stdout="main\n", stderr="")
        cfg_data = {
            "fraises": {
                "my_api": {
                    "type": "api",
                    "environments": {
                        "production": {
                            "name": "my-api",
                            "app_path": "/var/www/api",
                        },
                    },
                },
            },
            "branch_mapping": {
                "main": {
                    "fraise": "my_api",
                    "environment": "production",
                },
            },
        }
        cfg_file = tmp_path / "fraises.yaml"
        cfg_file.write_text(yaml.dump(cfg_data))
        _setup_project(tmp_path)

        from fraisier.deployers.base import DeploymentResult, DeploymentStatus

        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = True
        mock_deployer.execute.return_value = DeploymentResult(
            success=True,
            status=DeploymentStatus.SUCCESS,
            old_version="v1",
            new_version="v2",
            duration_seconds=1.0,
        )

        runner = CliRunner()
        with (
            patch("fraisier.config.get_config") as mock_gc,
            patch(
                "fraisier.cli._helpers._get_deployer",
                return_value=mock_deployer,
            ),
            patch("fraisier.locking.deployment_lock"),
        ):
            mock_cfg = MagicMock()
            mock_cfg.get_fraises_for_branch.return_value = [
                {
                    "fraise_name": "my_api",
                    "environment": "production",
                    "type": "api",
                    "app_path": "/var/www/api",
                }
            ]
            mock_gc.return_value = mock_cfg

            result = runner.invoke(
                main,
                [
                    "-c",
                    str(cfg_file),
                    "ship",
                    "patch",
                    "--version-file",
                    str(tmp_path / "version.json"),
                    "--pyproject",
                    str(tmp_path / "pyproject.toml"),
                ],
            )

        assert result.exit_code == 0
        mock_deployer.execute.assert_called_once()

    @patch("subprocess.run")
    def test_ship_dry_run_shows_deploy_info(self, mock_run, tmp_path):
        """ship --dry-run mentions deploy in plan."""
        mock_run.return_value = MagicMock(returncode=0)
        cfg = _setup_project(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "ship",
                "patch",
                "--dry-run",
                "--version-file",
                str(tmp_path / "version.json"),
            ],
        )
        assert result.exit_code == 0
        assert "Deploy" in result.output
