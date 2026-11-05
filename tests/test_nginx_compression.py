"""The API vhosts compress JSON with gzip and brotli (#500).

Debian's stock ``nginx.conf`` turns gzip on for ``text/html`` alone, so every
JSON or GraphQL response a fraisier vhost proxied went out uncompressed.

brotli is on by default too, and needs nginx's brotli module, which Debian
12/13 and Ubuntu 24.04 package and Ubuntu 22.04 does not. ``install.sh``
installs it with the other packages, and refuses in its preflight, before it
changes anything, on a host that can neither load nor install it.
"""

from __future__ import annotations

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_GZIP_TYPES = "gzip_types application/json application/graphql-response+json;"
_BROTLI_TYPES = "brotli_types application/json application/graphql-response+json;"
_PACKAGE = "libnginx-mod-http-brotli-filter"


def _render(tmp_path, *, nginx: str = "", fraise_nginx: str = "") -> dict[str, str]:
    """Render a one-API project; return the nginx files and install.sh."""
    out = tmp_path / "output"
    (tmp_path / "fraises.yaml").write_text(
        f"""
name: tp
scaffold:
  output_dir: {out}
{nginx}
fraises:
  api:
    type: api
{fraise_nginx}
    environments:
      production:
        app_path: /var/www/api
        health_check:
          url: http://127.0.0.1:8080/health
        nginx:
          server_name: api.example.com
          ssl_cert: /etc/ssl/certs/api.crt
          ssl_key: /etc/ssl/private/api.key
"""
    )
    ScaffoldRenderer(FraisierConfig(tmp_path / "fraises.yaml")).render()
    files = {p.name: p.read_text() for p in (out / "nginx").glob("*.conf")}
    files["install.sh"] = (out / "install.sh").read_text()
    return files


class TestTheEnvironmentVhostCompresses:
    def test_gzip_covers_json_and_graphql(self, tmp_path):
        vhost = _render(tmp_path)["api.example.com.conf"]

        assert _GZIP_TYPES in vhost

    def test_gzip_compresses_proxied_responses(self, tmp_path):
        vhost = _render(tmp_path)["api.example.com.conf"]

        assert "gzip_proxied any;" in vhost

    def test_brotli_is_on_by_default(self, tmp_path):
        vhost = _render(tmp_path)["api.example.com.conf"]

        assert _BROTLI_TYPES in vhost

    def test_brotli_false_leaves_brotli_out_and_keeps_gzip(self, tmp_path):
        vhost = _render(tmp_path, nginx="  nginx:\n    brotli: false")[
            "api.example.com.conf"
        ]

        assert ("brotli" not in vhost, _GZIP_TYPES in vhost) == (True, True)


class TestTheGatewayVhostsCompress:
    def test_every_https_server_block_compresses(self, tmp_path):
        out = tmp_path / "output"
        (tmp_path / "fraises.yaml").write_text(
            f"""
name: tp
scaffold:
  output_dir: {out}
fraises:
  management:
    type: api
    server_name: management.example.com
    environments:
      production:
        app_path: /var/www/management
  backend:
    type: api
    server_name: backend.example.com
    environments:
      production:
        app_path: /var/www/backend
"""
        )
        ScaffoldRenderer(FraisierConfig(tmp_path / "fraises.yaml")).render()
        gateway = (out / "nginx" / "gateway.conf").read_text()

        assert gateway.count("listen 443 ssl") == gateway.count(_GZIP_TYPES) > 0


class TestTheConfigKey:
    def test_a_non_boolean_brotli_is_refused(self, tmp_path):
        with pytest.raises(Exception, match=r"scaffold\.nginx\.brotli"):
            _render(tmp_path, nginx='  nginx:\n    brotli: "false"')


class TestInstallShProvidesTheModule:
    def test_the_package_is_installed_with_brotli_on(self, tmp_path):
        assert _PACKAGE in _render(tmp_path)["install.sh"]

    def test_the_package_is_not_asked_for_with_brotli_off(self, tmp_path):
        install = _render(tmp_path, nginx="  nginx:\n    brotli: false")["install.sh"]

        assert _PACKAGE not in install

    def test_the_preflight_names_both_fixes(self, tmp_path):
        install = _render(tmp_path)["install.sh"]

        assert (
            f"apt install {_PACKAGE}" in install,
            "scaffold.nginx.brotli: false" in install,
        ) == (True, True)

    def test_a_real_run_checks_before_its_first_change(self, tmp_path):
        install = _render(tmp_path)["install.sh"]
        check = install.index('[[ "$DRY_RUN" != "true" ]] && ! _nginx_brotli_ready')

        assert check < install.index("useradd --system")


class TestSetupRefusesWithoutTheModule:
    """``fraisier setup`` installs no packages, so the module must be there."""

    def _setup(self, tmp_path, runner, nginx: str = ""):
        from fraisier.setup import ServerSetup

        (tmp_path / "fraises.yaml").write_text(
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
{nginx}
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
"""
        )
        setup = ServerSetup(FraisierConfig(tmp_path / "fraises.yaml"), runner)
        setup._write_env_file = lambda: None  # ty: ignore[invalid-assignment]
        return setup

    def test_it_refuses_before_its_first_action(self, tmp_path):
        from fraisier.errors import ValidationError
        from fraisier.setup import NGINX_BROTLI_PROBE

        runner = _Runner(failing={NGINX_BROTLI_PROBE})

        with pytest.raises(ValidationError, match=r"scaffold\.nginx\.brotli: false"):
            self._setup(tmp_path, runner).execute()

        assert runner.calls == [list(NGINX_BROTLI_PROBE)]

    def test_brotli_false_does_not_probe(self, tmp_path):
        from fraisier.setup import NGINX_BROTLI_PROBE

        runner = _Runner(failing={NGINX_BROTLI_PROBE})
        self._setup(tmp_path, runner, "  nginx:\n    brotli: false").execute()

        assert list(NGINX_BROTLI_PROBE) not in runner.calls


class _Runner:
    """Records commands; fails the ones named, as a host without them would."""

    def __init__(self, *, failing: set[tuple[str, ...]]) -> None:
        self.calls: list[list[str]] = []
        self._failing = failing

    def run(self, cmd, *, check: bool = True, **_kwargs):
        import subprocess

        self.calls.append(list(cmd))
        code = 1 if tuple(cmd) in self._failing else 0
        if code and check:
            raise subprocess.CalledProcessError(code, cmd)
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="")
