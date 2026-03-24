"""Tests for security validations: docker cp, backup dir, webhook env."""

import pytest

from fraisier.dbops._validation import validate_docker_cp_path, validate_file_path


class TestDockerCpPathTraversal:
    def test_rejects_double_dot(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_docker_cp_path("web:/../etc/shadow")

    def test_rejects_double_dot_in_middle(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_docker_cp_path("web:/app/../../etc/passwd")

    def test_accepts_normal_path(self):
        result = validate_docker_cp_path("web:/app/data/file.txt")
        assert result == "web:/app/data/file.txt"

    def test_rejects_empty_container(self):
        with pytest.raises(ValueError):
            validate_docker_cp_path(":/app/data")


class TestBackupOutputDir:
    def test_rejects_path_outside_safe_dir(self, tmp_path):
        with pytest.raises(ValueError, match="traversal"):
            validate_file_path("../../etc/cron.d", base_dir=tmp_path)

    def test_accepts_path_inside_safe_dir(self, tmp_path):
        result = validate_file_path("backups/daily", base_dir=tmp_path)
        assert result == "backups/daily"


class TestWebhookEnvValidation:
    def test_valid_port(self):
        from fraisier.webhook import _validate_env_config

        _validate_env_config(port=8080, rate_limit=10)

    def test_port_zero_raises(self):
        from fraisier.webhook import _validate_env_config

        with pytest.raises(ValueError, match="port"):
            _validate_env_config(port=0, rate_limit=10)

    def test_port_too_high_raises(self):
        from fraisier.webhook import _validate_env_config

        with pytest.raises(ValueError, match="port"):
            _validate_env_config(port=70000, rate_limit=10)

    def test_negative_port_raises(self):
        from fraisier.webhook import _validate_env_config

        with pytest.raises(ValueError, match="port"):
            _validate_env_config(port=-1, rate_limit=10)

    def test_rate_limit_zero_raises(self):
        from fraisier.webhook import _validate_env_config

        with pytest.raises(ValueError, match="rate"):
            _validate_env_config(port=8080, rate_limit=0)

    def test_rate_limit_negative_raises(self):
        from fraisier.webhook import _validate_env_config

        with pytest.raises(ValueError, match="rate"):
            _validate_env_config(port=8080, rate_limit=-5)
