"""Unit tests for fraisier.naming."""

from __future__ import annotations

import pytest

from fraisier.naming import deploy_socket_name


class TestDeploySocketName:
    def test_derives_from_name_field(self):
        env = {"name": "api.myapp.dev"}
        assert deploy_socket_name(env) == "fraisier-api.myapp.dev.socket"

    def test_falls_back_to_env_key_when_name_absent(self):
        env = {"app_path": "/var/www/prod"}
        assert deploy_socket_name(env, "production") == "fraisier-production.socket"

    def test_explicit_override_takes_precedence(self):
        env = {"name": "api.myapp.dev", "systemd_deploy_socket": "custom-deploy.socket"}
        assert deploy_socket_name(env) == "custom-deploy.socket"

    def test_override_without_socket_suffix_gets_appended(self):
        env = {"name": "api.myapp.dev", "systemd_deploy_socket": "custom-deploy"}
        assert deploy_socket_name(env) == "custom-deploy.socket"

    def test_override_with_socket_suffix_unchanged(self):
        env = {"systemd_deploy_socket": "my-deploy.socket"}
        assert deploy_socket_name(env) == "my-deploy.socket"

    def test_name_field_takes_precedence_over_env_key(self):
        env = {"name": "api.myapp.io"}
        assert deploy_socket_name(env, "production") == "fraisier-api.myapp.io.socket"

    def test_empty_env_key_without_name_field(self):
        env = {}
        result = deploy_socket_name(env, "")
        assert result == "fraisier-.socket"

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("my-app-dev", "fraisier-my-app-dev.socket"),
            ("api.myapp.staging", "fraisier-api.myapp.staging.socket"),
            ("myapp-worker", "fraisier-myapp-worker.socket"),
        ],
    )
    def test_various_name_formats(self, name, expected):
        assert deploy_socket_name({"name": name}) == expected
