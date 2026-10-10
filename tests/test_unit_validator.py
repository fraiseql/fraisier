"""The positive allowlist a unit must pass before root installs it (#433).

Every refusal names the rule that refused it, so a validator that refuses for
the wrong reason (or for every reason) fails here rather than passing a table
of "refused" assertions vacuously. ``TestKnownGood`` is the other half of that
guard: a validator that refuses everything fails it.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from fraisier.root_policy import RootPolicy, UnitGrant
from fraisier.unit_validator import (
    Refusal,
    check_app_unit_name,
    check_scaffold_unit_name,
    validate_unit,
)

POLICY = RootPolicy(
    project="demo",
    scaffold_dir="/var/lib/fraisier/demo/scaffold",
    users=frozenset({"deploy", "www-data", "postgres"}),
    groups=frozenset({"deploy", "www-data", "postgres"}),
    exec_prefixes=(
        "/home/deploy/.local/bin/",
        "/srv/app/",
        "/bin/sh",
        "/usr/bin/psql",
    ),
    read_paths=frozenset({"/etc/demo/app.env", "/etc/demo/credentials/pg"}),
    directories=frozenset({"app", "fraisier"}),
    units={
        "app.service": UnitGrant(source="systemd/app.service", action="plain"),
        "prune.timer": UnitGrant(source="systemd/prune.timer", action="timer"),
    },
    operator_only={
        "/etc/systemd/system/fraisier-demo-systemctl-helper.service": (
            "systemd/fraisier-demo-systemctl-helper.service"
        ),
        "/etc/sudoers.d/demo": "sudoers",
    },
)

GOOD_SERVICE = """\
[Unit]
Description=demo app
After=network.target postgresql.service
Requires=fraisier-demo.socket

[Service]
Type=exec
User=www-data
Group=www-data
WorkingDirectory=/srv/app
ExecStartPre=/bin/sh -c 'echo hi > /run/app/x'
ExecStart=/srv/app/.venv/bin/uvicorn app:app --port 8000
ExecReload=-/home/deploy/.local/bin/fraisier reload
Restart=on-failure
RestartSec=5
MemoryMax=1G
RuntimeDirectory=app
LogsDirectory=app
LogsDirectoryMode=0750
EnvironmentFile=-/etc/demo/app.env
LoadCredential=pg:/etc/demo/credentials/pg
Environment=PYTHONDONTWRITEBYTECODE=1
Environment="A=1" "B=two words"
StandardInput=null
StandardOutput=journal
NoNewPrivileges=true
ProtectSystem=strict
SystemCallFilter=~@privileged @mount
ReadWritePaths=/srv/app

[Install]
WantedBy=multi-user.target
"""

GOOD_TIMER = """\
[Unit]
Description=prune

[Timer]
OnCalendar=daily
Persistent=true
Unit=prune.service

[Install]
WantedBy=timers.target
"""


def _rules(refusals: list[Refusal]) -> set[str]:
    return {r.rule for r in refusals}


def _with(line: str, *, drop: str | None = None) -> str:
    """GOOD_SERVICE with *line* added to [Service] (and *drop* removed)."""
    text = GOOD_SERVICE
    if drop is not None:
        assert text.count(drop) == 1, f"mutation anchor {drop!r} is not unique"
        text = text.replace(drop, "")
    anchor = "Restart=on-failure\n"
    assert text.count(anchor) == 1
    return text.replace(anchor, f"{anchor}{line}\n")


class TestKnownGood:
    def test_a_good_service_passes(self):
        assert validate_unit("app.service", GOOD_SERVICE, POLICY) == []

    def test_a_good_timer_passes(self):
        assert validate_unit("prune.timer", GOOD_TIMER, POLICY) == []

    def test_a_comment_inside_a_continued_line_is_dropped(self):
        # Kept, the comment would join the value and set PYTHONPATH.
        text = _with("Environment=A=1 \\\n# PYTHONPATH=/x\n  B=2")
        assert validate_unit("app.service", text, POLICY) == []

    def test_continuation_lines_and_comments_are_read_as_systemd_reads_them(self):
        text = _with(
            "# a comment\n; another\nExecStartPost=/srv/app/bin/x \\\n  --flag"
        )
        assert validate_unit("app.service", text, POLICY) == []


class TestIdentity:
    @pytest.mark.parametrize("value", ["root", "0", "00"])
    def test_root_user_is_refused_even_if_the_policy_lists_it(self, value):
        policy = replace(POLICY, users=POLICY.users | {value})
        text = GOOD_SERVICE.replace("User=www-data", f"User={value}")
        assert _rules(validate_unit("app.service", text, policy)) == {"user"}

    def test_no_user_is_refused(self):
        text = GOOD_SERVICE.replace("User=www-data\n", "")
        assert _rules(validate_unit("app.service", text, POLICY)) == {"user"}

    def test_an_empty_user_after_a_good_one_is_refused(self):
        # systemd reads the last assignment, and an empty one resets to root.
        assert _rules(validate_unit("app.service", _with("User="), POLICY)) == {"user"}

    def test_a_user_outside_the_policy_is_refused(self):
        text = GOOD_SERVICE.replace("User=www-data", "User=mallory")
        assert _rules(validate_unit("app.service", text, POLICY)) == {"user"}

    def test_an_instance_specifier_user_is_refused(self):
        text = GOOD_SERVICE.replace("User=www-data", "User=%i")
        assert _rules(validate_unit("app@.service", text, POLICY)) == {"user"}

    @pytest.mark.parametrize("value", ["root", "0"])
    def test_root_group_is_refused_even_if_the_policy_lists_it(self, value):
        policy = replace(POLICY, groups=POLICY.groups | {value})
        text = GOOD_SERVICE.replace("Group=www-data", f"Group={value}")
        assert _rules(validate_unit("app.service", text, policy)) == {"group"}

    @pytest.mark.parametrize("value", ["root", "0", "wheel", ""])
    def test_a_bad_group_is_refused(self, value):
        text = GOOD_SERVICE.replace("Group=www-data", f"Group={value}")
        assert _rules(validate_unit("app.service", text, POLICY)) == {"group"}

    def test_the_app_unit_from_service_user_root_is_refused(self):
        # Path 9: what service.j2 renders for `service: {user: root}`.
        text = GOOD_SERVICE.replace(
            "User=www-data\nGroup=www-data", "User=root\nGroup=root"
        )
        assert _rules(validate_unit("app.service", text, POLICY)) == {"user", "group"}


class TestExec:
    @pytest.mark.parametrize("prefix", ["+", "!", "!!", "-+", "@!", "|"])
    def test_a_privileged_prefix_is_refused_with_a_good_user(self, prefix):
        text = _with(f"ExecStartPre={prefix}/bin/sh -c true")
        assert _rules(validate_unit("app.service", text, POLICY)) == {"exec-prefix"}

    @pytest.mark.parametrize(
        "command",
        [
            "/usr/bin/sudo -u root id",
            "/tmp/x",
            "/srv/app/../usr/bin/sudo",
            "/srv/appx/bin/run",
            "relative/bin",
            "%h/bin/run",
            "/bin/shx",
            "/srv/app/%i/run",
            "/srv/app/$X/run",
        ],
    )
    def test_an_executable_outside_the_prefixes_is_refused(self, command):
        text = _with(f"ExecStartPost={command}")
        assert _rules(validate_unit("app.service", text, POLICY)) == {"exec-path"}

    @pytest.mark.parametrize(
        "key",
        [
            "ExecStart",
            "ExecStartPre",
            "ExecStartPost",
            "ExecReload",
            "ExecStop",
            "ExecStopPost",
            "ExecCondition",
        ],
    )
    def test_every_exec_key_is_judged(self, key):
        text = _with(f"{key}=/tmp/x")
        assert _rules(validate_unit("app.service", text, POLICY)) == {"exec-path"}

    def test_a_relative_executable_is_refused_even_if_the_policy_grants_it(self):
        policy = replace(POLICY, exec_prefixes=("bin/",))
        text = "[Service]\nUser=www-data\nExecStart=bin/x\n"
        assert _rules(validate_unit("app.service", text, policy)) == {"exec-path"}

    def test_an_exec_reset_is_allowed(self):
        assert validate_unit("app.service", _with("ExecStartPost="), POLICY) == []

    def test_a_quoted_executable_is_judged_unquoted(self):
        text = _with('ExecStartPost="/tmp/x" arg')
        assert _rules(validate_unit("app.service", text, POLICY)) == {"exec-path"}


class TestRefusedDirectives:
    @pytest.mark.parametrize(
        "line",
        [
            "DynamicUser=yes",
            "PermissionsStartOnly=yes",
            "SupplementaryGroups=docker",
            "AmbientCapabilities=CAP_SYS_ADMIN",
            "CapabilityBoundingSet=CAP_SYS_ADMIN",
            "LoadCredentialEncrypted=x:/etc/shadow",
            "SetCredential=x:y",
            "ImportCredential=x",
            "BindPaths=/etc/shadow:/srv/app/shadow",
            "BindReadOnlyPaths=/root",
            "RootDirectory=/srv/app/root",
            "RootImage=/srv/app/img.raw",
            "PAMName=login",
            "TTYPath=/dev/tty1",
            "StandardInputText=x",
            "FrobnicateEverything=yes",
        ],
    )
    def test_a_directive_off_the_allowlist_is_refused(self, line):
        assert _rules(validate_unit("app.service", _with(line), POLICY)) == {
            "directive"
        }

    def test_a_directive_in_the_wrong_section_is_refused(self):
        text = GOOD_SERVICE.replace("[Unit]\n", "[Unit]\nUser=www-data\n", 1)
        assert _rules(validate_unit("app.service", text, POLICY)) == {"directive"}

    def test_an_alias_in_install_is_refused(self):
        text = GOOD_SERVICE + "Alias=ssh.service\n"
        assert _rules(validate_unit("app.service", text, POLICY)) == {"directive"}


class TestReadPaths:
    @pytest.mark.parametrize(
        "line",
        [
            "LoadCredential=x:/etc/shadow",
            "LoadCredential=x",
            "EnvironmentFile=/etc/shadow",
            "EnvironmentFile=-/etc/demo/other.env",
        ],
    )
    def test_a_path_root_would_read_must_be_in_the_policy(self, line):
        assert _rules(validate_unit("app.service", _with(line), POLICY)) == {
            "read-path"
        }


class TestEnvironment:
    @pytest.mark.parametrize(
        "line",
        [
            "Environment=LD_PRELOAD=/srv/app/evil.so",
            "Environment=LD_LIBRARY_PATH=/srv/app",
            'Environment="A=1" "PYTHONPATH=/srv/app"',
            "Environment=PYTHONSTARTUP=/srv/app/x.py",
            "Environment=PYTHONHOME=/srv/app",
        ],
    )
    def test_a_loader_variable_is_refused(self, line):
        rules = _rules(validate_unit("app.service", _with(line), POLICY))
        assert rules == {"environment"}


class TestStdio:
    @pytest.mark.parametrize(
        "line",
        [
            "StandardOutput=file:/etc/cron.d/x",
            "StandardOutput=append:/etc/sudoers.d/x",
            "StandardError=truncate:/etc/passwd",
            "StandardInput=file:/etc/shadow",
            "StandardInput=tty",
        ],
    )
    def test_root_never_opens_a_path_for_the_unit(self, line):
        assert _rules(validate_unit("app.service", _with(line), POLICY)) == {"stdio"}


class TestDirectories:
    @pytest.mark.parametrize(
        "line",
        ["LogsDirectory=nginx", "RuntimeDirectory=sudo", "StateDirectory=../etc"],
    )
    def test_a_directory_root_would_chown_must_be_in_the_policy(self, line):
        assert _rules(validate_unit("app.service", _with(line), POLICY)) == {
            "directory"
        }


class TestSyntax:
    @pytest.mark.parametrize(
        "text",
        [
            "User=www-data\n[Service]\n",
            ".include /etc/x\n",
            "[Service]\nnot a directive\n",
            "[Service]\nUser www=x\n",
            "[Service]\nExecStart=/srv/app/x \\",
            "[Service]\nUser=www-\x00data\n",
        ],
    )
    def test_what_systemd_would_read_differently_is_refused(self, text):
        assert "syntax" in _rules(validate_unit("app.service", text, POLICY))

    @pytest.mark.parametrize("line", ["Requires=../../etc/x", "OnFailure=a/b.service"])
    def test_a_dependency_must_name_a_unit(self, line):
        text = GOOD_SERVICE.replace("[Unit]\n", f"[Unit]\n{line}\n", 1)
        assert _rules(validate_unit("app.service", text, POLICY)) == {"syntax"}

    def test_a_timer_must_activate_a_unit_by_name(self):
        text = GOOD_TIMER.replace("Unit=prune.service", "Unit=/etc/x")
        assert _rules(validate_unit("prune.timer", text, POLICY)) == {"syntax"}

    def test_an_unknown_section_is_refused(self):
        text = GOOD_SERVICE + "[Socket]\nListenStream=/etc/x\n"
        assert _rules(validate_unit("app.service", text, POLICY)) == {"section"}

    def test_a_timer_may_not_carry_a_service_section(self):
        text = GOOD_TIMER + "[Service]\nUser=www-data\n"
        assert _rules(validate_unit("prune.timer", text, POLICY)) == {"section"}

    def test_a_value_refusal_names_the_line(self):
        text = GOOD_SERVICE.replace("User=www-data", "User=mallory")
        [refusal] = validate_unit("app.service", text, POLICY)
        assert refusal.line == text.splitlines().index("User=mallory") + 1

    def test_a_refusal_names_the_line(self):
        [refusal] = validate_unit("app.service", _with("DynamicUser=yes"), POLICY)
        assert refusal.line == GOOD_SERVICE.splitlines().index("Restart=on-failure") + 2
        assert "DynamicUser" in refusal.message


class TestScaffoldUnitName:
    def test_a_granted_name_passes(self):
        assert check_scaffold_unit_name("app.service", POLICY) is None

    @pytest.mark.parametrize(
        "name",
        [
            "other.service",
            "app.mount",
            "app.service.d/x.conf",
            "fraisier-demo-systemctl-helper.service",
        ],
    )
    def test_a_name_the_policy_does_not_grant_is_refused(self, name):
        refusal = check_scaffold_unit_name(name, POLICY)
        assert refusal is not None
        assert refusal.rule == "unit-name"


class TestAppUnitName:
    @pytest.mark.parametrize("name", ["job.service", "job.timer", "kuma@.service"])
    def test_a_service_or_timer_passes(self, name):
        assert check_app_unit_name(name, POLICY) is None

    @pytest.mark.parametrize(
        "name", ["job.mount", "job.socket", "job.path", "job.service.d", "job"]
    )
    def test_any_other_suffix_is_refused(self, name):
        refusal = check_app_unit_name(name, POLICY)
        assert refusal is not None
        assert refusal.rule == "unit-suffix"

    @pytest.mark.parametrize(
        "name", ["fraisier-demo-systemctl-helper.service", "app.service"]
    )
    def test_a_name_fraisier_installs_is_refused(self, name):
        refusal = check_app_unit_name(name, POLICY)
        assert refusal is not None
        assert refusal.rule == "unit-name"
