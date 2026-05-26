"""systemd-name consumer audit — LazyEnv resolution (#220).

``systemd_service`` and ``systemd_deploy_socket`` fields can be
``!envvar``-tagged in fraises.yaml. These names then flow into
multiple consumer paths that either: shell out to ``systemctl`` (where
a bare LazyEnv raises TypeError or silently resolves via
``__fspath__``), or call string methods (``.endswith``, ``.removesuffix``)
that don't exist on LazyEnv.

The audit boundary is the read site. Each consumer reads
``systemd_service`` / ``systemd_deploy_socket`` via the central helpers
``naming.resolve_systemd_service`` and ``naming.resolve_systemd_deploy_socket``,
which run ``to_str`` once and surface unset-var errors with the YAML
path stamped by the loader walker, instead of as a deep subprocess
TypeError.
"""

from __future__ import annotations

import pytest

from fraisier.config._lazy_env import LazyEnv
from fraisier.errors import ConfigurationError
from fraisier.naming import (
    app_service_name,
    deploy_socket_name,
    resolve_systemd_deploy_socket,
    resolve_systemd_service,
)


class TestResolveSystemdService:
    def test_str_passes_through(self):
        assert (
            resolve_systemd_service({"systemd_service": "api.service"}) == "api.service"
        )

    def test_missing_returns_none(self):
        assert resolve_systemd_service({}) is None

    def test_explicit_none_returns_none(self):
        assert resolve_systemd_service({"systemd_service": None}) is None

    def test_lazyenv_resolves(self, monkeypatch):
        monkeypatch.setenv("SVC_NAME", "myapi.service")
        out = resolve_systemd_service(
            {
                "systemd_service": LazyEnv(
                    "SVC_NAME",
                    "fraises.api.environments.production.systemd_service",
                ),
            }
        )
        assert out == "myapi.service"

    def test_unset_lazyenv_raises_with_path(self, monkeypatch):
        monkeypatch.delenv("SVC_NAME", raising=False)
        with pytest.raises(
            ConfigurationError,
            match=r"SVC_NAME.*fraises\.api\.environments\.production\.systemd_service",
        ):
            resolve_systemd_service(
                {
                    "systemd_service": LazyEnv(
                        "SVC_NAME",
                        "fraises.api.environments.production.systemd_service",
                    ),
                }
            )


class TestResolveSystemdDeploySocket:
    def test_str_passes_through(self):
        assert (
            resolve_systemd_deploy_socket(
                {"systemd_deploy_socket": "api-deploy.socket"}
            )
            == "api-deploy.socket"
        )

    def test_missing_returns_none(self):
        assert resolve_systemd_deploy_socket({}) is None

    def test_lazyenv_resolves(self, monkeypatch):
        monkeypatch.setenv("SOCK_NAME", "api-deploy")
        assert (
            resolve_systemd_deploy_socket(
                {
                    "systemd_deploy_socket": LazyEnv(
                        "SOCK_NAME",
                        "fraises.api.environments.production.systemd_deploy_socket",
                    ),
                }
            )
            == "api-deploy"
        )

    def test_unset_lazyenv_raises_with_path(self, monkeypatch):
        monkeypatch.delenv("SOCK_NAME", raising=False)
        with pytest.raises(
            ConfigurationError,
            match=(
                r"SOCK_NAME.*fraises\.api\.environments\.production"
                r"\.systemd_deploy_socket"
            ),
        ):
            resolve_systemd_deploy_socket(
                {
                    "systemd_deploy_socket": LazyEnv(
                        "SOCK_NAME",
                        "fraises.api.environments.production.systemd_deploy_socket",
                    ),
                }
            )


class TestAppServiceNameWithLazy:
    def test_app_service_name_resolves_lazyenv(self, monkeypatch):
        monkeypatch.setenv("SVC_NAME", "api.service")
        out = app_service_name(
            "fraisier",
            "my_api",
            "production",
            {"systemd_service": LazyEnv("SVC_NAME", "p")},
        )
        # Suffix-strip-then-add behavior preserved on the resolved str.
        assert out == "api.service"

    def test_app_service_name_str_unchanged(self):
        out = app_service_name(
            "fraisier",
            "my_api",
            "production",
            {"systemd_service": "api"},
        )
        assert out == "api.service"


class TestDeploySocketNameWithLazy:
    def test_deploy_socket_resolves_lazyenv(self, monkeypatch):
        monkeypatch.setenv("SOCK_NAME", "api-deploy")
        out = deploy_socket_name(
            {"systemd_deploy_socket": LazyEnv("SOCK_NAME", "p")},
            env_key="production",
        )
        # No .socket suffix on env var → helper appends it.
        assert out == "api-deploy.socket"

    def test_deploy_socket_with_socket_suffix_already_set(self, monkeypatch):
        monkeypatch.setenv("SOCK_NAME", "api-deploy.socket")
        out = deploy_socket_name(
            {"systemd_deploy_socket": LazyEnv("SOCK_NAME", "p")},
            env_key="production",
        )
        assert out == "api-deploy.socket"
