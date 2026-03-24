"""Tests for pre-deploy validation checks and drift detection."""

import hashlib
from unittest.mock import MagicMock, patch

from fraisier.validation import (
    ValidationCheckResult,
    ValidationRunner,
    _hash_file,
    detect_drift,
)


class TestValidationCheckResult:
    """Test ValidationCheckResult dataclass."""

    def test_check_result_to_dict(self):
        """Test serialization includes name and passed, message only if set."""
        result = ValidationCheckResult(name="test_check", passed=True)
        d = result.to_dict()
        assert d == {"name": "test_check", "passed": True, "severity": "error"}

        result_with_msg = ValidationCheckResult(
            name="fail_check", passed=False, message="something wrong"
        )
        d2 = result_with_msg.to_dict()
        assert d2 == {
            "name": "fail_check",
            "passed": False,
            "severity": "error",
            "message": "something wrong",
        }


class TestValidationRunner:
    """Test ValidationRunner checks."""

    def _make_config(self, fraises=None, deploy_user="fraisier", envs=None):
        """Build a mock FraisierConfig."""
        config = MagicMock()
        config.list_fraises.return_value = fraises or []
        config.deployment.deploy_user = deploy_user
        if envs is not None:
            config.list_environments.side_effect = lambda name: envs.get(name, [])
        else:
            config.list_environments.return_value = ["production"]
        # Provide sane defaults for the new checks
        config.get_fraise.side_effect = lambda name: {
            "type": "api",
            "environments": {
                e: {"name": name, "app_path": f"/var/www/{name}"}
                for e in (envs or {}).get(name, ["production"])
            },
        }
        config.get_environment.side_effect = lambda name, _env: {
            "name": name,
            "app_path": f"/var/www/{name}",
        }
        return config

    def test_config_valid_with_fraises(self):
        """Config check passes when fraises are defined."""
        config = self._make_config(fraises=["api", "etl"])
        runner = ValidationRunner(config)
        result = runner._check_config_valid()
        assert result.passed is True
        assert result.name == "config_valid"

    def test_config_valid_no_fraises(self):
        """Config check fails when no fraises are defined."""
        config = self._make_config(fraises=[])
        runner = ValidationRunner(config)
        result = runner._check_config_valid()
        assert result.passed is False
        assert "No fraises" in result.message

    @patch("fraisier.validation.pwd")
    def test_deploy_user_exists(self, mock_pwd):
        """Deploy user check passes when user exists."""
        mock_pwd.getpwnam.return_value = MagicMock()
        config = self._make_config(deploy_user="deploy")
        runner = ValidationRunner(config)
        result = runner._check_deploy_user()
        assert result.passed is True
        mock_pwd.getpwnam.assert_called_once_with("deploy")

    @patch("fraisier.validation.pwd")
    def test_deploy_user_missing(self, mock_pwd):
        """Deploy user check fails when user does not exist."""
        mock_pwd.getpwnam.side_effect = KeyError("no such user")
        config = self._make_config(deploy_user="ghost")
        runner = ValidationRunner(config)
        result = runner._check_deploy_user()
        assert result.passed is False
        assert "ghost" in result.message

    def test_fraises_have_environments_pass(self):
        """Check passes when all fraises have at least one environment."""
        config = self._make_config(
            fraises=["api", "etl"],
            envs={"api": ["production"], "etl": ["staging"]},
        )
        runner = ValidationRunner(config)
        result = runner._check_fraises_have_environments()
        assert result.passed is True

    def test_fraises_have_environments_fail(self):
        """Check fails when a fraise has no environments."""
        config = self._make_config(
            fraises=["api", "etl"],
            envs={"api": ["production"], "etl": []},
        )
        runner = ValidationRunner(config)
        result = runner._check_fraises_have_environments()
        assert result.passed is False
        assert "etl" in result.message

    @patch("fraisier.validation.pwd")
    def test_run_all_returns_all_checks(self, mock_pwd):
        """run_all returns results from all registered checks."""
        mock_pwd.getpwnam.return_value = MagicMock()
        config = self._make_config(fraises=["api"], envs={"api": ["prod"]})
        runner = ValidationRunner(config)
        results = runner.run_all()
        names = {r.name for r in results}
        # Must include at least the original 3 plus the new checks
        assert {"config_valid", "deploy_user", "fraises_have_environments"} <= names
        assert len(results) >= 3


class TestDriftDetection:
    """Test drift detection and file hashing."""

    def _compute_hash(self, content: bytes) -> str:
        return "sha256:" + hashlib.sha256(content).hexdigest()

    def test_detect_drift_no_drift(self, tmp_path):
        """No drift when file hashes match."""
        content = b"hello world"
        (tmp_path / "app.conf").write_bytes(content)
        hashes = {"app.conf": self._compute_hash(content)}
        results = detect_drift(tmp_path, hashes)
        assert results == []

    def test_detect_drift_modified(self, tmp_path):
        """Drift detected when file content differs from expected hash."""
        (tmp_path / "app.conf").write_bytes(b"modified content")
        hashes = {"app.conf": self._compute_hash(b"original content")}
        results = detect_drift(tmp_path, hashes)
        assert len(results) == 1
        assert results[0].drifted is True
        assert "Modified" in results[0].message

    def test_detect_drift_missing_file(self, tmp_path):
        """Drift detected when expected file is missing."""
        hashes = {"missing.conf": self._compute_hash(b"data")}
        results = detect_drift(tmp_path, hashes)
        assert len(results) == 1
        assert results[0].drifted is True
        assert "Missing" in results[0].message

    def test_detect_drift_ignore(self, tmp_path):
        """Ignored files are skipped even if they would drift."""
        hashes = {"ignored.conf": self._compute_hash(b"data")}
        results = detect_drift(tmp_path, hashes, ignore={"ignored.conf"})
        assert results == []

    def test_hash_file(self, tmp_path):
        """_hash_file returns sha256-prefixed hex digest."""
        content = b"test content"
        path = tmp_path / "file.txt"
        path.write_bytes(content)
        result = _hash_file(path)
        expected = "sha256:" + hashlib.sha256(content).hexdigest()
        assert result == expected
