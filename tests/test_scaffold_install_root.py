"""``sudo fraisier scaffold-install``: the operator's root install (#433, path 8).

Root content is rendered by root, into a directory nobody else can write, from
the config the operator names. Because that config is still one a commit
author chose, the operator is shown every root-owned file this would change and
the root policy it would write, before anything runs, ``--yes`` included. The
root policy is written only after the install succeeded.

Run as anyone but root it refuses: all it could do is hand a script someone
else wrote to ``sudo``.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fraisier.cli import main
from fraisier.cli import scaffold as scaffold_mod

CONFIG = """\
name: proj
servers:
  only.example.io:
    machine_hostnames: [solo]
scaffold:
  deploy_user: deployer
  output_dir: {output}
fraises:
  api:
    type: api
    environments:
      production:
        server: only.example.io
        app_path: {app}
        systemd_service: api.service
        git_repo: /var/git/api.git
        service:
          user: www-data
  nightly:
    type: scheduled
    environments:
      production:
        server: only.example.io
        app_path: {app}
        jobs:
          run:
            systemd_service: nightly.service
            systemd_timer: nightly.timer
"""

NIGHTLY = "[Service]\nUser=postgres\nExecStart=/usr/bin/psql -c 'select 1'\n"


@pytest.fixture
def host(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "scripts" / "systemd").mkdir(parents=True)
    (app / "scripts" / "systemd" / "nightly.service").write_text(NIGHTLY)
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(CONFIG.format(output=tmp_path / "output", app=app))

    state = SimpleNamespace(
        cfg=cfg,
        runs=[],
        trees=[],
        written=[],
        installed={},
        returncode=0,
    )

    def run_script(cmd):
        tree = Path(cmd[1]).parent
        state.trees.append(tree)
        state.runs.append(cmd)
        state.tree_mode = stat.S_IMODE(tree.stat().st_mode)
        state.tree_files = sorted(p.name for p in tree.iterdir())
        state.executable = os.access(cmd[1], os.X_OK)
        return state.returncode

    monkeypatch.setattr(scaffold_mod, "_euid", lambda: 0)
    monkeypatch.setattr(scaffold_mod, "_run_script", run_script)
    monkeypatch.setattr(scaffold_mod, "_hostname", lambda: "solo")
    monkeypatch.setattr(scaffold_mod, "_read_installed", state.installed.get)
    monkeypatch.setattr(scaffold_mod, "_read_current_policy", lambda _p: None)
    monkeypatch.setattr(scaffold_mod, "_write_root_policy", state.written.append)
    monkeypatch.setattr(
        scaffold_mod, "_read_current_sudoers", lambda _name: (None, "missing")
    )
    return state


def _invoke(host, *args):
    return CliRunner().invoke(main, ["-c", str(host.cfg), "scaffold-install", *args])


def test_anyone_but_root_is_refused(host, monkeypatch):
    monkeypatch.setattr(scaffold_mod, "_euid", lambda: 1000)
    result = _invoke(host, "--yes")
    assert result.exit_code == 1
    assert "sudo fraisier scaffold-install" in result.output
    assert host.runs == []


def test_a_tree_someone_else_rendered_is_refused(host, tmp_path):
    result = _invoke(host, "--yes", "--output-dir", str(tmp_path / "state"))
    assert result.exit_code == 1
    assert "--output-dir" in result.output
    assert host.runs == []


def test_root_renders_into_a_private_directory_and_removes_it(host, tmp_path):
    result = _invoke(host, "--yes")
    assert result.exit_code == 0, result.output
    [tree] = set(host.trees)
    assert not tree.is_relative_to(tmp_path / "output")
    assert host.tree_mode == 0o700
    assert "install.sh" in host.tree_files
    assert host.executable, "sudo runs install.sh directly; it must be executable"
    assert not tree.exists()


def test_a_failure_says_to_rerun_the_command_not_the_removed_script(host):
    host.returncode = 4
    result = _invoke(host, "--yes")
    assert result.exit_code == 4
    assert "sudo fraisier scaffold-install --yes --verbose" in result.output
    assert "install.sh exited with code 4" in result.output


def test_every_root_owned_file_that_changes_is_shown_even_with_yes(host):
    host.installed["/etc/systemd/system/api.service"] = b"[Service]\nUser=old\n"
    result = _invoke(host, "--yes")
    assert result.exit_code == 0, result.output
    assert "--- /etc/systemd/system/api.service (installed)" in result.output
    assert "-User=old" in result.output
    assert "+User=www-data" in result.output
    assert "new: /etc/sudoers.d/proj" in result.output


def test_the_policy_is_shown_and_written_after_the_install(host):
    result = _invoke(host, "--yes")
    assert result.exit_code == 0, result.output
    [policy] = host.written
    assert "api.service" in policy.units
    assert {"deployer", "www-data", "postgres"} <= policy.users
    assert "/usr/bin/psql" in policy.exec_prefixes
    assert "root policy" in result.output.lower()
    assert '"api.service"' in result.output


def test_a_failed_install_writes_no_policy(host):
    host.returncode = 5
    result = _invoke(host, "--yes")
    assert result.exit_code == 5
    assert host.written == []


@pytest.mark.parametrize("flag", ["--dry-run", "--validate-only"])
def test_a_preview_writes_no_policy(host, flag):
    result = _invoke(host, "--yes", flag)
    assert result.exit_code == 0, result.output
    assert host.written == []


def test_declining_runs_nothing(host):
    result = CliRunner().invoke(
        main, ["-c", str(host.cfg), "scaffold-install"], input="n\n"
    )
    assert "Aborted" in result.output
    install_runs = [cmd for cmd in host.runs if "--dry-run" not in cmd]
    assert install_runs == []
    assert host.written == []


def test_an_unregistered_host_gets_no_policy_and_says_so(host, monkeypatch):
    monkeypatch.setattr(scaffold_mod, "_hostname", lambda: "elsewhere")
    result = _invoke(host, "--yes")
    assert host.written == []
    assert "elsewhere" in result.output


def test_the_written_policy_is_the_one_shown(host):
    from fraisier.root_policy import dump_policy

    result = _invoke(host, "--yes")
    [policy] = host.written
    shown = json.loads(dump_policy(policy))
    assert f'"scaffold_dir": "{shown["scaffold_dir"]}"' in result.output


def test_files_that_already_match_are_not_listed(host, monkeypatch):
    seen = {}

    def installed(path):
        return seen.get(path)

    monkeypatch.setattr(scaffold_mod, "_read_installed", installed)
    real = scaffold_mod._render_root_tree

    def render_and_install(config):
        tree = real(config)
        for artifact in scaffold_mod._host_payload(tree)["artifacts"]:
            if artifact["destination"]:
                seen[artifact["destination"]] = (tree / artifact["source"]).read_bytes()
        return tree

    monkeypatch.setattr(scaffold_mod, "_render_root_tree", render_and_install)
    result = _invoke(host, "--yes")
    assert "(none differ from what is installed)" in result.output
    assert "new: " not in result.output


def test_another_hosts_app_units_do_not_widen_this_hosts_policy(host, tmp_path):
    other_app = tmp_path / "other-app"
    (other_app / "scripts" / "systemd").mkdir(parents=True)
    (other_app / "scripts" / "systemd" / "remote.service").write_text(
        NIGHTLY.replace("User=postgres", "User=mallory")
    )
    host.cfg.write_text(
        host.cfg.read_text().replace(
            "servers:\n",
            "servers:\n  else.example.io:\n    machine_hostnames: [other]\n",
        )
        + f"""\
  remote:
    type: scheduled
    environments:
      production:
        server: else.example.io
        app_path: {other_app}
        jobs:
          run:
            systemd_service: remote.service
"""
    )
    result = _invoke(host, "--yes")
    assert result.exit_code == 0, result.output
    [policy] = host.written
    assert "mallory" not in policy.users
    assert "postgres" in policy.users
