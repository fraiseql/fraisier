"""Tests for docker cp path validation."""

import pytest

from fraisier.providers.docker_compose.provider import _validate_docker_cp_path


class TestValidateDockerCpPath:
    def test_valid_path_passes(self):
        _validate_docker_cp_path("mycontainer:/app/data.txt")

    def test_valid_path_with_subdirs(self):
        _validate_docker_cp_path("my-container:/var/log/app.log")

    def test_missing_colon_raises(self):
        with pytest.raises(ValueError, match="Invalid docker cp path"):
            _validate_docker_cp_path("/local/path/only")

    def test_missing_absolute_path_raises(self):
        with pytest.raises(ValueError, match="Invalid docker cp path"):
            _validate_docker_cp_path("container:relative/path")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Invalid docker cp path"):
            _validate_docker_cp_path("")

    def test_spaces_in_container_name_raises(self):
        with pytest.raises(ValueError, match="Invalid docker cp path"):
            _validate_docker_cp_path("my container:/path")
