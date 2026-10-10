"""The root-owned fraisier install, and the operator command that upgrades it (#433, D5).

Every uv path is pinned under the root dir, and the environment is built from
nothing: under ``sudo`` the caller's ``HOME`` can survive, and uv would then
fetch the interpreter, read its config and keep its cache in a directory the
deploy user owns.
"""

from __future__ import annotations

import pytest

from fraisier import root_install
from fraisier.root_install import (
    ROOT_BIN_DIR,
    ROOT_CACHE_DIR,
    ROOT_PYTHON_INSTALL_DIR,
    ROOT_TOOL_DIR,
    ROOT_UV,
    ROOT_UV_DIR,
    root_install_argv,
    root_install_env,
    root_uv_install_argv,
)

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def test_the_install_environment_is_built_from_nothing():
    assert root_install_env() == {
        "HOME": "/root",
        "PATH": SAFE_PATH,
        "UV_TOOL_DIR": ROOT_TOOL_DIR,
        "UV_TOOL_BIN_DIR": ROOT_BIN_DIR,
        "UV_PYTHON_INSTALL_DIR": ROOT_PYTHON_INSTALL_DIR,
        "UV_CACHE_DIR": ROOT_CACHE_DIR,
        "UV_PYTHON_PREFERENCE": "only-managed",
        "UV_NO_CONFIG": "1",
    }


def test_the_install_command_pins_python_and_version():
    assert root_install_argv("0.90.0") == [
        ROOT_UV,
        "tool",
        "install",
        "--force",
        "--python",
        "3.14",
        "fraisier==0.90.0",
    ]


def test_uv_comes_from_a_pinned_installer_into_the_root_dir():
    argv = root_uv_install_argv()
    assert argv[:2] == ["sh", "-c"]
    script = argv[2]
    assert f"https://astral.sh/uv/{root_install.ROOT_UV_VERSION}/install.sh" in script
    assert f"UV_INSTALL_DIR={ROOT_UV_DIR}" in script
    assert "UV_NO_MODIFY_PATH=1" in script
    assert "env -i HOME=/root" in script


def test_uv_is_made_root_owned_after_the_installer_runs():
    """Measured: the installer keeps the tarball's owner (uid 1001, gid 117).

    Any account with that uid could then replace the uv root runs.
    """
    script = root_uv_install_argv()[2]
    assert script.endswith(
        f" && chown -R root:root {ROOT_UV_DIR} && chmod -R go-w {ROOT_UV_DIR}"
    )


class _Recorder:
    def __init__(self, returncode: int = 0):
        self.calls: list[tuple[list[str], dict | None]] = []
        self.returncode = returncode

    def __call__(self, argv, *, env=None, check=False, cwd=None):
        self.calls.append((list(argv), env))

        class _Done:
            returncode = self.returncode

        return _Done()


@pytest.fixture
def host(tmp_path, monkeypatch):
    links = tmp_path / "usr-local-bin"
    links.mkdir()
    monkeypatch.setattr(root_install, "ROOT_LINK_DIR", str(links))
    monkeypatch.setattr(root_install, "_uv_present", lambda: True)
    monkeypatch.setattr(root_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(root_install, "_prepare_root_dir", lambda: None)
    run = _Recorder()
    monkeypatch.setattr(root_install, "_run", run)
    return run, links


def test_upgrade_installs_the_named_version_with_the_pinned_environment(
    host, monkeypatch
):
    run, _links = host
    monkeypatch.setenv("HOME", "/home/deployer")
    monkeypatch.setenv("UV_INDEX_URL", "https://evil.example/simple")
    assert root_install.main(["0.90.0"]) == 0
    [(argv, env)] = run.calls
    assert argv == root_install_argv("0.90.0")
    assert env == root_install_env()


def test_upgrade_links_the_root_commands_onto_sudos_path(host):
    _run, links = host
    root_install.main(["0.90.0"])
    for name in ("fraisier", "fraisier-root-upgrade"):
        link = links / name
        assert link.is_symlink()
        assert str(link.readlink()) == f"{ROOT_BIN_DIR}/{name}"


def test_upgrade_replaces_a_link_that_points_elsewhere(host, tmp_path):
    _run, links = host
    deploy_copy = tmp_path / "deploy-fraisier"
    deploy_copy.write_text("#!/bin/sh\n")
    (links / "fraisier").symlink_to(deploy_copy)
    root_install.main(["0.90.0"])
    assert str((links / "fraisier").readlink()) == f"{ROOT_BIN_DIR}/fraisier"


def test_upgrade_installs_uv_first_when_it_is_missing(host, monkeypatch):
    run, _links = host
    monkeypatch.setattr(root_install, "_uv_present", lambda: False)
    root_install.main(["0.90.0"])
    assert [argv for argv, _env in run.calls] == [
        root_uv_install_argv(),
        root_install_argv("0.90.0"),
    ]


def test_upgrade_refuses_unless_root(host, monkeypatch, capsys):
    run, _links = host
    monkeypatch.setattr(root_install.os, "geteuid", lambda: 1000)
    assert root_install.main(["0.90.0"]) == 1
    assert run.calls == []
    assert "as root" in capsys.readouterr().err


@pytest.mark.parametrize(
    "version", ["latest", ">=0.90", "0.90.0; rm -rf /", "0.90.0 --index-url x", ""]
)
def test_upgrade_refuses_anything_but_one_exact_version(host, version, capsys):
    run, _links = host
    assert root_install.main([version]) == 2
    assert run.calls == []


def test_a_failed_install_leaves_the_links_alone(host, monkeypatch):
    run, links = host
    run.returncode = 1
    assert root_install.main(["0.90.0"]) == 1
    assert list(links.iterdir()) == []


@pytest.mark.parametrize("argv", [[], ["0.90.0", "0.91.0"]])
def test_upgrade_takes_exactly_one_version(host, argv):
    run, _links = host
    assert root_install.main(argv) == 2
    assert run.calls == []
