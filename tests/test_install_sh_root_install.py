"""install.sh installs the root-owned fraisier the root helpers run (#433, D5).

Only when it is missing: upgrading it is ``fraisier-root-upgrade``'s job, run by
an operator. And uv runs with nothing inherited from the caller, so a ``HOME``
or ``UV_*`` that survived ``sudo`` cannot send the interpreter, the cache or the
package index anywhere the deploy user controls.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from importlib.metadata import version

import pytest

from fraisier.root_install import root_install_env
from tests.test_install_plan_golden import _SINGLE_HOST, _render

_SUDO = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FRAISIER_TEST_SUDO_LOG"
case "$1" in sh|chown) exit 0 ;; esac
exec "$@"
"""

_FAKE_UV = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" > "$(dirname "$0")/argv"
env > "$(dirname "$0")/env"
"""


class _Host:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.install_sh = _render(tmp_path, _SINGLE_HOST) / "install.sh"
        self.root = tmp_path / "fraisier-root"
        self.uv_dir = self.root / "uv"
        self.uv = self.uv_dir / "uv"
        self.python = self.root / "tools" / "fraisier" / "bin" / "python"
        self.links = tmp_path / "usr-local-bin"
        self.links.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        sudo = self.bin / "sudo"
        sudo.write_text(_SUDO)
        sudo.chmod(0o755)
        self.sudo_log = tmp_path / "sudo.log"

    def fake_uv(self):
        self.uv_dir.mkdir(parents=True)
        self.uv.write_text(_FAKE_UV)
        self.uv.chmod(0o755)

    def run(self, *, expect_ok: bool = True):
        snippet = f"""\
            . "{self.install_sh}"
            DRY_RUN=false
            _FRAISIER_ROOT_DIR="{self.root}"
            _FRAISIER_ROOT_UV="{self.uv}"
            _FRAISIER_ROOT_PYTHON="{self.python}"
            _FRAISIER_ROOT_BIN="{self.root}/bin"
            _FRAISIER_ROOT_LINK_DIR="{self.links}"
            _install_root_fraisier
        """
        script = self.tmp / "harness.sh"
        script.write_text(textwrap.dedent(snippet))
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FRAISIER_TEST_SUDO_LOG": str(self.sudo_log),
            "HOME": "/home/deployer",
            "UV_INDEX_URL": "https://evil.example/simple",
            "UV_TOOL_DIR": "/home/deployer/.local/share/uv/tools",
        }
        result = subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True, check=False
        )
        if expect_ok:
            assert result.returncode == 0, result.stderr
        return result

    def sudo_calls(self) -> list[str]:
        if not self.sudo_log.exists():
            return []
        return self.sudo_log.read_text().splitlines()


@pytest.fixture
def host(tmp_path):
    return _Host(tmp_path)


def test_uv_runs_with_only_the_pinned_environment(host):
    host.fake_uv()
    host.run()
    seen = dict(
        line.split("=", 1) for line in (host.uv_dir / "env").read_text().splitlines()
    )
    for shell_var in ("PWD", "SHLVL", "_", "OLDPWD"):
        seen.pop(shell_var, None)
    assert seen == root_install_env()


def test_it_installs_the_version_that_rendered_it(host):
    host.fake_uv()
    host.run()
    argv = (host.uv_dir / "argv").read_text().strip()
    assert argv == f"tool install --force --python 3.14 fraisier=={version('fraisier')}"


def test_it_links_the_root_commands_onto_sudos_path(host):
    host.fake_uv()
    host.run()
    for name in ("fraisier", "fraisier-root-upgrade"):
        link = host.links / name
        assert link.is_symlink()
        assert link.readlink() == host.root / "bin" / name


def test_an_existing_root_install_is_left_alone(host):
    host.fake_uv()
    host.python.parent.mkdir(parents=True)
    host.python.write_text("")
    host.python.chmod(0o755)
    result = host.run()
    assert not (host.uv_dir / "argv").exists()
    assert "fraisier-root-upgrade" in result.stdout


def test_a_missing_uv_is_installed_pinned_first(host):
    # The stub records the uv install without running it, so the step after
    # it fails strict; what is asserted is the install that was asked for.
    host.run(expect_ok=False)
    [uv_install] = [c for c in host.sudo_calls() if c.startswith("sh -c ")]
    assert "https://astral.sh/uv/0.9.18/install.sh" in uv_install
    assert f"UV_INSTALL_DIR={host.uv_dir}" in uv_install
    assert uv_install.endswith(
        f"&& chown -R root:root {host.uv_dir} && chmod -R go-w {host.uv_dir}"
    )
