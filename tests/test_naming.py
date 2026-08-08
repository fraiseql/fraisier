"""Unit tests for fraisier.naming."""

from __future__ import annotations

from pathlib import Path

import pytest

from fraisier.naming import (
    app_service_name,
    deploy_socket_name,
    retention_unit_names,
    unit_installer_socket_path,
    unit_installer_unit_names,
)


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


class TestAppServiceName:
    def test_default_pattern(self):
        assert (
            app_service_name("proj", "api", "production", {})
            == "proj_api_production.service"
        )

    def test_systemd_service_override(self):
        env = {"systemd_service": "api.myapp.dev.service"}
        assert (
            app_service_name("proj", "api", "development", env)
            == "api.myapp.dev.service"
        )

    def test_systemd_service_without_suffix(self):
        env = {"systemd_service": "api.myapp.dev"}
        assert (
            app_service_name("proj", "api", "development", env)
            == "api.myapp.dev.service"
        )

    def test_service_name_nested_override(self):
        env = {"service": {"service_name": "myapp-api"}}
        assert app_service_name("proj", "api", "production", env) == "myapp-api.service"

    def test_systemd_service_takes_precedence_over_service_name(self):
        env = {"systemd_service": "top.service", "service": {"service_name": "nested"}}
        assert app_service_name("proj", "api", "production", env) == "top.service"


class TestUnitInstallerSocketPath:
    """The path the helper socket listens on has one authority (#337)."""

    def test_unit_installer_socket_path_is_derived_from_project_and_env(self):
        assert unit_installer_socket_path("myapp", "production") == Path(
            "/run/fraisier/production/unit-installer-myapp.sock"
        )

    def test_socket_path_sits_beside_the_unit_names(self):
        """The path and the unit names agree on project and env.

        Both describe the same helper — one per (project, environment)
        (#240) — so a change to either that does not move the other is
        the drift this module exists to prevent.
        """
        socket_unit, _service_unit = unit_installer_unit_names("myapp", "production")
        path = unit_installer_socket_path("myapp", "production")

        assert "myapp" in socket_unit
        assert "production" in socket_unit
        assert path.parent.name == "production"
        assert path.name == "unit-installer-myapp.sock"


class TestRetentionUnitNames:
    """One authority for the retention unit pair's names (#339).

    #337 exists because a path was derived in three places. This helper is
    written before the second call site rather than after the third: the
    renderer names the files it writes and the manifest names the files it
    installs, and both read here.
    """

    def test_retention_unit_names_are_derived_from_the_entry_name(self):
        assert retention_unit_names("myapp", "development", "production-full") == (
            "fraisier-myapp-development-retain-production-full.service",
            "fraisier-myapp-development-retain-production-full.timer",
        )

    def test_the_pair_shares_a_stem(self):
        """systemd resolves a timer's target by stem when there is no `Unit=`.

        A timer and service whose stems differ is a firing into a unit that
        does not exist — how backup.timer and backup.service drifted apart.
        """
        service, timer = retention_unit_names("myapp", "development", "prod")

        assert service.removesuffix(".service") == timer.removesuffix(".timer")

    def test_the_service_comes_first(self):
        """Install order is load-bearing: enabling a timer whose service is
        not yet on disk is backup.timer's bug with the ordering inverted."""
        service, timer = retention_unit_names("myapp", "development", "prod")

        assert service.endswith(".service")
        assert timer.endswith(".timer")

    def test_two_entries_in_one_environment_get_distinct_names(self):
        first = retention_unit_names("myapp", "development", "production-full")
        second = retention_unit_names("myapp", "development", "production-slim")

        assert first != second

    def test_the_same_entry_name_in_two_environments_stays_distinct(self):
        """A received corpus is keyed by (environment, name), never by name."""
        dev = retention_unit_names("myapp", "development", "production")
        staging = retention_unit_names("myapp", "staging", "production")

        assert dev != staging
