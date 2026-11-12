"""Install one category or one file of the scaffold (#501).

A one-line vhost change used to mean reinstalling everything scaffold-install
owns, or bypassing fraisier with a hand-run ``install`` + ``nginx -t`` +
reload. ``--only <category>`` and ``--path <file>`` narrow scaffold-install,
scaffold-diff and the rendered ``install.sh`` to part of the tree:

- the filter lives in ``install.sh``, so ``nginx -t`` before the reload and
  ``visudo -c`` before the sudoers install stay in one script;
- the root policy after a partial install changes only the selected entries,
  so it keeps matching what is installed;
- a deploy still refuses while any root-owned change is pending, and says
  which categories and which ``--only`` command clear it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fraisier.cli import main
from fraisier.cli import scaffold as scaffold_mod
from fraisier.config import FraisierConfig
from fraisier.root_policy import RootPolicy, UnitGrant
from fraisier.scaffold.renderer import ScaffoldRenderer
from fraisier.scaffold.selection import (
    CATEGORIES,
    Selection,
    SelectionError,
    category_of,
    merge_policy,
    operator_remedy,
    parse_selection,
    select_artifacts,
)

CONFIG = """\
name: proj
servers:
  only.example.io:
    machine_hostnames: [solo]
scaffold:
  deploy_user: deployer
  output_dir: {output}
  nginx:
    brotli: false
fraises:
  api:
    type: api
    environments:
      production:
        server: only.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
        nginx:
          server_name: api.example.com
          upstream_port: 8000
"""

VHOST = "nginx/api.example.com.conf"
VHOST_DEST = "/etc/nginx/sites-available/api.example.com"
GATEWAY_DEST = "/etc/nginx/sites-available/proj"
SUDOERS_DEST = "/etc/sudoers.d/proj"


# ---------------------------------------------------------------------------
# The selection itself
# ---------------------------------------------------------------------------


class TestCategories:
    def test_the_categories_are_the_ones_the_issue_names(self):
        assert CATEGORIES == ("nginx", "systemd", "sudoers", "scripts", "ci")

    @pytest.mark.parametrize(
        ("destination", "category"),
        [
            ("/etc/nginx/sites-available/proj", "nginx"),
            ("/etc/sudoers.d/proj", "sudoers"),
            ("/etc/systemd/system/api.service", "systemd"),
            ("/etc/passwd", None),
            ("/etc/nginxx/evil", None),
        ],
    )
    def test_a_destination_names_its_category(self, destination, category):
        assert category_of(destination) == category


class TestParseSelection:
    def test_no_filter_is_everything(self):
        assert parse_selection((), None) is None

    def test_an_unknown_category_is_refused_naming_the_known_ones(self):
        with pytest.raises(SelectionError) as raised:
            parse_selection(("vhosts",), None)
        assert "vhosts" in str(raised.value)
        assert "nginx, systemd, sudoers, scripts, ci" in str(raised.value)

    @pytest.mark.parametrize(
        "path", ["/etc/sudoers.d/proj", "../sudoers", "nginx/../sudoers", "", "./x"]
    )
    def test_a_path_outside_the_tree_is_refused(self, path):
        with pytest.raises(SelectionError, match="relative to the rendered"):
            parse_selection((), path)

    def test_only_and_path_are_not_combined(self):
        with pytest.raises(SelectionError, match="either"):
            parse_selection(("nginx",), VHOST)

    def test_categories_are_kept_in_their_canonical_order(self):
        selection = parse_selection(("sudoers", "nginx", "nginx"), None)
        assert selection is not None
        assert selection.install_args() == ["--only", "nginx", "--only", "sudoers"]

    def test_a_path_passes_itself_to_the_installer(self):
        selection = parse_selection((), VHOST)
        assert selection is not None
        assert selection.install_args() == ["--path", VHOST]


CHOSEN = [
    {"source": VHOST, "destination": VHOST_DEST},
    {"source": "nginx/gateway.conf", "destination": GATEWAY_DEST},
    {"source": "sudoers", "destination": SUDOERS_DEST},
    {
        "source": "systemd/api.service",
        "destination": "/etc/systemd/system/api.service",
    },
]


def _select(only=(), path=None) -> Selection:
    selection = parse_selection(only, path)
    assert selection is not None
    return selection


class TestSelectArtifacts:
    def test_a_category_selects_its_files_only(self):
        selection = _select(("nginx",), None)
        assert [a["source"] for a in select_artifacts(CHOSEN, selection)] == [
            VHOST,
            "nginx/gateway.conf",
        ]

    def test_a_path_selects_that_file_only(self):
        selection = _select((), VHOST)
        assert [a["source"] for a in select_artifacts(CHOSEN, selection)] == [VHOST]

    def test_a_path_this_host_does_not_install_is_refused_with_the_choices(self):
        selection = _select((), "nginx/other.conf")
        with pytest.raises(SelectionError) as raised:
            select_artifacts(CHOSEN, selection)
        assert "nginx/other.conf" in str(raised.value)
        assert VHOST in str(raised.value)

    @pytest.mark.parametrize("category", ["scripts", "ci"])
    def test_scripts_and_ci_select_nothing_root_installs_and_say_why(self, category):
        selection = _select((category,), None)
        with pytest.raises(SelectionError) as raised:
            select_artifacts(CHOSEN, selection)
        assert "used in place" in str(raised.value)
        assert "nginx, systemd and sudoers" in str(raised.value)


# ---------------------------------------------------------------------------
# The root policy after a partial install: merge the selected entries only
# ---------------------------------------------------------------------------


def _policy(**overrides) -> RootPolicy:
    fields = {
        "project": "proj",
        "scaffold_dir": "/var/lib/fraisier/proj",
        "users": frozenset({"deployer"}),
        "groups": frozenset({"deployer"}),
        "exec_prefixes": ("/usr/bin/old",),
        "read_paths": frozenset(),
        "directories": frozenset(),
        "units": {"api.service": UnitGrant("systemd/api.service", "plain")},
        "operator_only": {
            VHOST_DEST: VHOST,
            GATEWAY_DEST: "nginx/gateway.conf",
            SUDOERS_DEST: "sudoers",
        },
    }
    fields.update(overrides)
    return RootPolicy(**fields)


CURRENT = _policy()
DRAFT = _policy(
    users=frozenset({"deployer", "www-data"}),
    exec_prefixes=("/usr/bin/new",),
    units={
        "api.service": UnitGrant("systemd/api.service.v2", "plain"),
        "extra.service": UnitGrant("systemd/extra.service", "plain"),
    },
    operator_only={
        GATEWAY_DEST: "nginx/gateway.conf",
        "/etc/nginx/sites-available/new": "nginx/new.conf",
        SUDOERS_DEST: "sudoers.v2",
    },
)


class TestMergePolicy:
    def test_only_nginx_replaces_the_nginx_entries_and_nothing_else(self):
        merged = merge_policy(CURRENT, DRAFT, _select(("nginx",), None))
        assert merged.operator_only == {
            GATEWAY_DEST: "nginx/gateway.conf",
            "/etc/nginx/sites-available/new": "nginx/new.conf",
            SUDOERS_DEST: "sudoers",
        }
        assert merged.units == CURRENT.units
        assert merged.users == CURRENT.users
        assert merged.exec_prefixes == CURRENT.exec_prefixes

    def test_only_systemd_takes_the_units_and_the_identities_they_grant(self):
        merged = merge_policy(CURRENT, DRAFT, _select(("systemd",), None))
        assert merged.units == DRAFT.units
        assert merged.users == DRAFT.users
        assert merged.exec_prefixes == DRAFT.exec_prefixes
        assert merged.operator_only == CURRENT.operator_only

    def test_one_unit_keeps_every_other_grant_and_widens_identities_only(self):
        selection = _select((), "systemd/extra.service")
        merged = merge_policy(
            CURRENT,
            DRAFT,
            selection,
            selected=[
                {
                    "source": "systemd/extra.service",
                    "destination": "/etc/systemd/system/extra.service",
                }
            ],
        )
        assert merged.units == {
            "api.service": UnitGrant("systemd/api.service", "plain"),
            "extra.service": UnitGrant("systemd/extra.service", "plain"),
        }
        assert merged.users == CURRENT.users | DRAFT.users
        assert set(merged.exec_prefixes) == {"/usr/bin/old", "/usr/bin/new"}
        assert merged.operator_only == CURRENT.operator_only

    def test_one_vhost_leaves_the_identities_alone(self):
        selection = _select((), "nginx/new.conf")
        merged = merge_policy(
            CURRENT,
            DRAFT,
            selection,
            selected=[
                {
                    "source": "nginx/new.conf",
                    "destination": "/etc/nginx/sites-available/new",
                }
            ],
        )
        assert merged.users == CURRENT.users
        assert merged.operator_only == {
            **CURRENT.operator_only,
            "/etc/nginx/sites-available/new": "nginx/new.conf",
        }


# ---------------------------------------------------------------------------
# A deploy still refuses, and names what clears it
# ---------------------------------------------------------------------------


class TestOperatorRemedy:
    def test_the_pending_categories_and_the_only_command_are_named(self):
        remedy = operator_remedy(
            [
                f"{SUDOERS_DEST}: differs from the render",
                f"{VHOST_DEST}: not installed",
                "api.service: systemd/api.service is not in the render",
            ]
        )
        assert "pending: nginx, systemd, sudoers" in remedy
        assert (
            "sudo fraisier scaffold-install --only nginx --only systemd "
            "--only sudoers" in remedy
        )

    def test_a_pending_item_without_a_category_asks_for_the_whole_install(self):
        remedy = operator_remedy(
            [f"{VHOST_DEST}: not installed", "artifact-manifest.json: not valid JSON"]
        )
        assert "--only" not in remedy
        assert "sudo fraisier scaffold-install`" in remedy

    def test_the_helper_report_carries_the_same_remedy(self):
        from fraisier.scaffold_apply import ApplyOutcome

        outcome = ApplyOutcome(pending=[f"{VHOST_DEST}: differs from the render"])
        assert "sudo fraisier scaffold-install --only nginx" in outcome.report()

    def test_the_deploy_error_carries_it(self, tmp_path):
        from unittest.mock import MagicMock

        from fraisier.deployers.api import APIDeployer
        from fraisier.errors import DeploymentError

        deployer = APIDeployer({"fraise_name": "api", "environment": "production"})
        reply = MagicMock(
            returncode=1,
            stdout="installed 0 unit(s), 3 unchanged",
            stderr="",
            pending=[f"{VHOST_DEST}: differs from the render"],
            refused={},
            deferred_restarts=[],
        )
        deployer._try_scaffold_install_via_socket = lambda _path: reply  # ty: ignore[invalid-assignment]
        deployer._record_deferred_restarts = lambda *_a: None  # ty: ignore[invalid-assignment]
        with pytest.raises(DeploymentError) as raised:
            deployer._install_scaffold(tmp_path / "fraises.yaml")
        assert "sudo fraisier scaffold-install --only nginx" in str(raised.value)


# ---------------------------------------------------------------------------
# install.sh --only / --path
# ---------------------------------------------------------------------------


def _render(tmp_path: Path) -> Path:
    out = tmp_path / "generated"
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(CONFIG.format(output=out))
    renderer = ScaffoldRenderer(FraisierConfig(cfg))
    renderer.output_dir = out
    renderer.render()
    return out


def _fake_bin(tmp_path: Path, *, nginx_t_fails: bool = False) -> Path:
    """``hostname`` answers ``solo``; ``sudo`` logs its argv instead of running."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "hostname").write_text("#!/bin/bash\necho solo\n")
    log = tmp_path / "sudo.log"
    fail = 'if [[ "$1" == nginx && "$2" == -t ]]; then exit 1; fi\n'
    (bin_dir / "sudo").write_text(
        f'#!/bin/bash\necho "$*" >> {log}\n' + (fail if nginx_t_fails else "")
    )
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    return bin_dir


def _install_sh(tmp_path: Path, *args: str, nginx_t_fails: bool = False):
    out = _render(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{_fake_bin(tmp_path, nginx_t_fails=nginx_t_fails)}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(out / "install.sh"), "--scaffold-dir", str(out), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    log = tmp_path / "sudo.log"
    result.sudo = log.read_text().splitlines() if log.exists() else []  # ty: ignore[unresolved-attribute]
    result.plan = [  # ty: ignore[unresolved-attribute]
        line.strip().removeprefix("[would run] ").replace(str(out), "$SCAFFOLD")
        for line in result.stdout.splitlines()
        if line.strip().startswith("[would run] ")
    ]
    return result


class TestInstallShFilter:
    def test_only_nginx_plans_the_vhosts_and_the_checked_reload_only(self, tmp_path):
        result = _install_sh(tmp_path, "--dry-run", "--only", "nginx")
        assert result.returncode == 0, result.stderr
        assert result.plan == [
            f"sudo cp $SCAFFOLD/{VHOST} {VHOST_DEST}",
            f"sudo ln -sf {VHOST_DEST} /etc/nginx/sites-enabled/api.example.com",
            f"sudo cp $SCAFFOLD/nginx/gateway.conf {GATEWAY_DEST}",
            f"sudo ln -sf {GATEWAY_DEST} /etc/nginx/sites-enabled/proj",
            "sudo nginx -t && sudo systemctl reload nginx",
        ]

    def test_only_systemd_plans_no_nginx_and_no_sudoers(self, tmp_path):
        result = _install_sh(tmp_path, "--dry-run", "--only", "systemd")
        assert result.returncode == 0, result.stderr
        joined = "\n".join(result.plan)
        assert "/etc/systemd/system/api.service" in joined
        assert "/etc/nginx" not in joined
        assert "nginx -t" not in joined
        assert "/etc/sudoers.d" not in joined and "visudo" not in result.stdout

    def test_only_sudoers_validates_before_it_installs_and_touches_nothing_else(
        self, tmp_path
    ):
        result = _install_sh(tmp_path, "--dry-run", "--only", "sudoers")
        assert result.returncode == 0, result.stderr
        assert "[would validate] sudo visudo -c -f" in result.stdout
        assert result.plan == [
            f"sudo install -m 0440 $SCAFFOLD/sudoers {SUDOERS_DEST}",
        ]

    def test_a_path_installs_that_file_and_its_category_check(self, tmp_path):
        result = _install_sh(tmp_path, "--dry-run", "--path", VHOST)
        assert result.returncode == 0, result.stderr
        assert result.plan == [
            f"sudo cp $SCAFFOLD/{VHOST} {VHOST_DEST}",
            f"sudo ln -sf {VHOST_DEST} /etc/nginx/sites-enabled/api.example.com",
            "sudo nginx -t && sudo systemctl reload nginx",
        ]

    def test_a_path_to_one_unit_installs_that_unit_only(self, tmp_path):
        result = _install_sh(tmp_path, "--dry-run", "--path", "systemd/api.service")
        assert result.returncode == 0, result.stderr
        copies = [line for line in result.plan if line.startswith("sudo cp ")]
        assert copies == [
            "sudo cp $SCAFFOLD/systemd/api.service /etc/systemd/system/api.service"
        ]

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (("--only", "vhosts"), "unknown category"),
            (("--path", "../sudoers"), "relative to the scaffold"),
            (("--path", "/etc/sudoers.d/proj"), "relative to the scaffold"),
            (("--path", "nginx/missing.conf"), "not in"),
            (("--only", "nginx", "--path", VHOST), "either"),
        ],
    )
    def test_a_bad_filter_is_refused_before_anything_runs(
        self, tmp_path, args, message
    ):
        result = _install_sh(tmp_path, *args)
        assert result.returncode == 2
        assert message in result.stderr
        assert result.sudo == []

    def test_a_failing_nginx_t_reloads_nothing_and_fails_the_install(self, tmp_path):
        result = _install_sh(tmp_path, "--only", "nginx", nginx_t_fails=True)
        assert result.returncode != 0
        assert "nginx -t" in result.sudo
        assert "systemctl reload nginx" not in result.sudo
        assert "nginx -t" in result.stderr

    def test_a_passing_nginx_t_reloads(self, tmp_path):
        result = _install_sh(tmp_path, "--only", "nginx")
        assert result.returncode == 0, result.stderr
        assert result.sudo[-2:] == ["nginx -t", "systemctl reload nginx"]


# ---------------------------------------------------------------------------
# scaffold-install --only / --path
# ---------------------------------------------------------------------------


@pytest.fixture
def host(tmp_path, monkeypatch):
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(CONFIG.format(output=tmp_path / "output"))
    state = SimpleNamespace(
        cfg=cfg, runs=[], written=[], installed={}, current=CURRENT, returncode=0
    )

    def run_script(cmd):
        state.runs.append(cmd)
        return state.returncode

    monkeypatch.setattr(scaffold_mod, "_euid", lambda: 0)
    monkeypatch.setattr(scaffold_mod, "_run_script", run_script)
    monkeypatch.setattr(scaffold_mod, "_hostname", lambda: "solo")
    monkeypatch.setattr(scaffold_mod, "_read_installed", state.installed.get)
    monkeypatch.setattr(scaffold_mod, "_read_current_policy", lambda _p: None)
    monkeypatch.setattr(scaffold_mod, "_load_current_policy", lambda _p: state.current)
    monkeypatch.setattr(scaffold_mod, "_write_root_policy", state.written.append)
    monkeypatch.setattr(
        scaffold_mod, "_read_current_sudoers", lambda _name: (None, "missing")
    )
    return state


def _scaffold_install(host, *args):
    return CliRunner().invoke(main, ["-c", str(host.cfg), "scaffold-install", *args])


class TestScaffoldInstallOnly:
    def test_the_filter_reaches_install_sh(self, host):
        result = _scaffold_install(host, "--yes", "--only", "nginx")
        assert result.exit_code == 0, result.output
        [cmd] = host.runs
        assert cmd[2:] == ["--only", "nginx"]

    def test_the_diff_shows_the_selected_files_only(self, host):
        host.installed["/etc/systemd/system/api.service"] = b"[Service]\nold\n"
        result = _scaffold_install(host, "--yes", "--only", "nginx")
        assert result.exit_code == 0, result.output
        assert f"new: {VHOST_DEST}" in result.output
        assert "/etc/systemd/system/api.service" not in result.output
        changed = [
            line
            for line in result.output.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        ]
        assert changed, "the nginx entries of the policy change"
        assert not [line for line in changed if "sudoers" in line]
        assert "skipping sudoers diff" not in result.output

    def test_the_preview_keeps_the_filter_whole(self, host, monkeypatch):
        monkeypatch.setattr("click.confirm", lambda *_a, **_k: False)
        result = _scaffold_install(host, "--path", VHOST)
        assert result.exit_code == 0, result.output
        [preview] = host.runs
        assert preview[2:] == ["--dry-run", "--path", VHOST]

    def test_the_written_policy_changes_the_selected_entries_only(self, host):
        result = _scaffold_install(host, "--yes", "--only", "nginx")
        assert result.exit_code == 0, result.output
        [policy] = host.written
        assert policy.units == CURRENT.units
        assert policy.users == CURRENT.users
        assert policy.operator_only[SUDOERS_DEST] == "sudoers"
        assert set(policy.operator_only) >= {VHOST_DEST, GATEWAY_DEST}

    def test_without_a_trusted_policy_a_partial_install_is_refused(self, host):
        host.current = None
        result = _scaffold_install(host, "--yes", "--only", "nginx")
        assert result.exit_code == 2
        assert "sudo fraisier scaffold-install" in result.output
        assert host.runs == []
        assert host.written == []

    @pytest.mark.parametrize(
        "args",
        [
            ("--only", "vhosts"),
            ("--path", "../etc/passwd"),
            ("--path", "nginx/other.conf"),
            ("--only", "scripts"),
        ],
    )
    def test_a_bad_filter_runs_and_writes_nothing(self, host, args):
        result = _scaffold_install(host, "--yes", *args)
        assert result.exit_code == 2, result.output
        assert host.runs == []
        assert host.written == []

    def test_no_filter_still_writes_the_whole_policy(self, host):
        result = _scaffold_install(host, "--yes")
        assert result.exit_code == 0, result.output
        [policy] = host.written
        assert SUDOERS_DEST in policy.operator_only
        assert host.runs[0][2:] == []


# ---------------------------------------------------------------------------
# scaffold-diff --only / --path
# ---------------------------------------------------------------------------


class TestScaffoldDiffOnly:
    def _diff(self, host, monkeypatch, *args):
        from fraisier.scaffold.diff import FileDiff

        diffs = [
            FileDiff(VHOST, Path(VHOST_DEST), "missing_installed"),
            FileDiff("sudoers", Path(SUDOERS_DEST), "missing_installed"),
            FileDiff(
                "systemd/api.service",
                Path("/etc/systemd/system/api.service"),
                "missing_installed",
            ),
        ]
        monkeypatch.setattr(
            "fraisier.scaffold.diff.compute_scaffold_diff", lambda **_k: diffs
        )
        monkeypatch.setattr(
            scaffold_mod, "_report_foreign_units", lambda *_a, **_k: None
        )
        return CliRunner().invoke(main, ["-c", str(host.cfg), "scaffold-diff", *args])

    def test_only_shows_the_selected_category(self, host, monkeypatch):
        result = self._diff(host, monkeypatch, "--only", "nginx")
        assert result.exit_code == 1
        assert VHOST in result.output
        assert "sudoers" not in result.output
        assert "api.service" not in result.output
        assert "1/1 files differ" in result.output

    def test_a_path_shows_that_file(self, host, monkeypatch):
        result = self._diff(host, monkeypatch, "--path", "sudoers")
        assert "sudoers" in result.output
        assert VHOST not in result.output

    def test_a_bad_filter_is_refused(self, host, monkeypatch):
        result = self._diff(host, monkeypatch, "--only", "vhosts")
        assert result.exit_code == 2
        assert "unknown category" in result.output

    def test_apply_with_a_filter_is_the_root_install(self, host, monkeypatch):
        applied = []
        monkeypatch.setattr(
            "fraisier.scaffold.diff.apply_scaffold_diffs",
            lambda *a, **_k: applied.append(a) or ([], []),
        )
        result = self._diff(host, monkeypatch, "--apply", "--only", "nginx")
        assert result.exit_code == 0, result.output
        assert applied == [], "a filtered apply must not copy files itself"
        [cmd] = [c for c in host.runs if "--dry-run" not in c]
        assert cmd[2:] == ["--only", "nginx"]
        [policy] = host.written
        assert policy.units == CURRENT.units

    def test_apply_with_a_filter_needs_root(self, host, monkeypatch):
        monkeypatch.setattr(scaffold_mod, "_euid", lambda: 1000)
        result = self._diff(host, monkeypatch, "--apply", "--only", "nginx")
        assert result.exit_code == 1
        assert "sudo fraisier scaffold-diff --apply --only nginx" in result.output
        assert host.runs == []


def test_selection_is_a_value():
    assert Selection(frozenset({"nginx"})) == Selection(frozenset({"nginx"}))


# ---------------------------------------------------------------------------
# Every config the install-plan golden pins: a category installs only itself
# ---------------------------------------------------------------------------


def _golden_plan(tmp_path, yaml_text, hostname, *args):
    from tests.test_install_plan_golden import _render as golden_render

    out = golden_render(tmp_path, yaml_text)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "hostname").write_text(f"#!/bin/bash\necho {hostname}\n")
    (bin_dir / "hostname").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        [
            "bash",
            str(out / "install.sh"),
            "--dry-run",
            "--scaffold-dir",
            str(out),
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return [
        line.strip().removeprefix("[would run] ").replace(str(out), "$SCAFFOLD")
        for line in result.stdout.splitlines()
        if line.strip().startswith("[would run] ")
    ]


def _golden_cases():
    from tests.test_install_plan_golden import MATRIX

    return [pytest.param(text, host, id=case) for case, text, host in MATRIX]


@pytest.mark.parametrize(("yaml_text", "hostname"), _golden_cases())
def test_only_nginx_touches_no_unit_in_any_pinned_config(tmp_path, yaml_text, hostname):
    plan = _golden_plan(tmp_path, yaml_text, hostname, "--only", "nginx")
    assert [
        line
        for line in plan
        if "systemd" in line or ("systemctl" in line and "reload nginx" not in line)
    ] == []


@pytest.mark.parametrize(("yaml_text", "hostname"), _golden_cases())
def test_only_systemd_touches_no_nginx_or_sudoers_in_any_pinned_config(
    tmp_path, yaml_text, hostname
):
    plan = _golden_plan(tmp_path, yaml_text, hostname, "--only", "systemd")
    assert [l for l in plan if "/etc/nginx" in l or "/etc/sudoers.d" in l] == []


@pytest.mark.parametrize(("yaml_text", "hostname"), _golden_cases())
def test_the_categories_together_plan_the_whole_install(tmp_path, yaml_text, hostname):
    (tmp_path / "whole").mkdir()
    (tmp_path / "every").mkdir()
    whole = _golden_plan(tmp_path / "whole", yaml_text, hostname)
    every = _golden_plan(
        tmp_path / "every",
        yaml_text,
        hostname,
        *("--only", "nginx", "--only", "systemd", "--only", "sudoers"),
    )
    assert every == whole
