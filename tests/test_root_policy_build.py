"""The policy an operator's scaffold-install writes, from the render they approved (#433).

What it grants is what that render needs and nothing more: the identities,
executables and files its units name. A unit that would still run something as
root is never granted to a deploy; it stays with the operator.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from fraisier.root_policy import dump_policy, parse_policy
from fraisier.root_policy_build import PolicyBuildError, build_policy
from fraisier.unit_validator import validate_unit
from tests.test_install_plan_golden import _render

if TYPE_CHECKING:
    from pathlib import Path


CONFIG = """\
name: proj
servers:
  only.example.io:
    machine_hostnames: [solo]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    install:
      user: app_user
      command: [bash, scripts/deploy-install.sh]
    environments:
      production:
        server: only.example.io
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
        service:
          user: www-data
          group: www-data
          environment_file: /etc/proj/api.env
          credentials:
            pg_password: /etc/proj/credentials/pg_password
        nginx:
          server_name: api.example.io
"""

TREES = ("/home/deployer/.local/bin/", "/var/www/api/")

APP_UNIT = """\
[Unit]
Description=job
[Service]
Type=oneshot
User=postgres
Group=postgres
EnvironmentFile=-/etc/default/job
StateDirectory=job
ExecStart=/usr/bin/psql -c 'select 1'
[Install]
WantedBy=multi-user.target
"""


def _uid(name: str) -> int | None:
    return 0 if name == "toor" else 1000


@pytest.fixture(scope="module")
def tree(tmp_path_factory) -> Path:
    return _render(tmp_path_factory.mktemp("render"), CONFIG)


def _build(tree: Path, **overrides):
    payload = json.loads((tree / "artifact-manifest.json").read_text())

    def read_source(rel: str) -> str | None:
        path = tree / rel
        return path.read_text() if path.is_file() else None

    kwargs = {
        "project": "proj",
        "scaffold_dir": "/var/lib/fraisier/proj/scaffold",
        "payload": payload,
        "hostname": "solo",
        "read_source": read_source,
        "app_units": {},
        "trees": (*TREES, "/var/lib/fraisier/proj/scaffold/"),
        "uid_of": _uid,
        "gid_of": _uid,
        **overrides,
    }
    return build_policy(**kwargs)


def test_every_granted_unit_passes_the_policy_it_produced(tree):
    draft = _build(tree)
    policy = draft.policy
    assert policy.units, "the render must grant something, or this proves nothing"
    for name, grant in policy.units.items():
        text = (tree / grant.source).read_text()
        assert validate_unit(name, text, policy) == [], name


def test_deploy_rewritable_units_and_their_follow_up(tree):
    units = _build(tree).policy.units
    assert units["api.service"].action == "plain"
    assert units["fraisier-proj-webhook.service"].action == "webhook"
    helper = "fraisier-proj-api-production-install-helper.service"
    assert units[helper].action == "install_helper"
    assert units["fraisier-api-production@.service"].action == "plain"


def test_root_units_sockets_sudoers_and_nginx_stay_with_the_operator(tree):
    draft = _build(tree)
    operator = draft.policy.operator_only
    units = draft.policy.units
    for name in (
        "fraisier-proj-systemctl-helper.service",
        "fraisier-proj-scaffold-install-helper.service",
        "fraisier-proj-backup-alert@.service",
        "fraisier-api-production.socket",
        "fraisier-proj-api-production-install-helper.socket",
    ):
        assert f"/etc/systemd/system/{name}" in operator, name
        assert name not in units, name
    assert "/etc/sudoers.d/proj" in operator
    assert any(dest.startswith("/etc/nginx/") for dest in operator)


def test_identities_files_and_directories_come_from_the_units(tree):
    policy = _build(tree).policy
    assert {"deployer", "www-data", "app_user"} <= policy.users
    assert "www-data" in policy.groups
    assert {
        "/etc/proj/api.env",
        "/etc/proj/credentials/pg_password",
    } <= policy.read_paths
    assert "/home/deployer/.local/bin/" in policy.exec_prefixes


def test_a_unit_rendered_to_run_as_root_is_never_granted(tree):
    def read_source(rel: str) -> str | None:
        text = (tree / rel).read_text() if (tree / rel).is_file() else None
        if rel == "systemd/api.service" and text is not None:
            text = text.replace("User=www-data", "User=root")
        return text

    draft = _build(tree, read_source=read_source)
    assert "api.service" not in draft.policy.units
    assert "/etc/systemd/system/api.service" in draft.policy.operator_only
    assert "root" not in draft.policy.users
    assert any("api.service" in note and "[user]" in note for note in draft.notes)


def test_an_app_units_identity_and_binaries_are_granted(tree):
    policy = _build(tree, app_units={"job.service": APP_UNIT}).policy
    assert "postgres" in policy.users
    assert "/usr/bin/psql" in policy.exec_prefixes
    assert "/etc/default/job" in policy.read_paths
    assert "job" in policy.directories
    assert validate_unit("job.service", APP_UNIT, policy) == []


def test_a_secret_root_reads_is_never_granted(tree):
    unit = APP_UNIT.replace(
        "StateDirectory=job", "LoadCredential=x:/etc/shadow\nStateDirectory=job"
    )
    draft = _build(tree, app_units={"job.service": unit})
    assert "/etc/shadow" not in draft.policy.read_paths
    assert any("job.service" in note and "[read-path]" in note for note in draft.notes)


def test_a_user_that_resolves_to_uid_zero_is_never_granted(tree):
    unit = APP_UNIT.replace("User=postgres", "User=toor")
    draft = _build(tree, app_units={"job.service": unit})
    assert "toor" not in draft.policy.users


def test_the_policy_round_trips(tree):
    policy = _build(tree).policy
    assert parse_policy(dump_policy(policy)) == policy


def test_an_unregistered_host_is_refused(tree):
    with pytest.raises(PolicyBuildError, match="not a machine"):
        _build(tree, hostname="elsewhere")


def test_a_root_group_is_never_granted(tree):
    unit = APP_UNIT.replace("Group=postgres", "Group=root")
    assert "root" not in _build(tree, app_units={"job.service": unit}).policy.groups


def test_an_executable_inside_a_tree_is_granted_by_the_tree_alone(tree):
    prefixes = _build(tree).policy.exec_prefixes
    assert not [
        p for p in prefixes if p.startswith("/var/www/api/") and p != "/var/www/api/"
    ]


def test_only_the_service_section_is_observed(tree):
    unit = APP_UNIT.replace("[Unit]\n", "[Unit]\nUser=mallory\n")
    assert "mallory" not in _build(tree, app_units={"job.service": unit}).policy.users


def test_a_root_helper_stays_with_the_operator_whatever_it_renders(tree):
    helper = "systemd/fraisier-proj-systemctl-helper.service"

    def read_source(rel: str) -> str | None:
        text = (tree / rel).read_text() if (tree / rel).is_file() else None
        if rel == helper and text is not None:
            text = text.replace("[Service]\n", "[Service]\nUser=deployer\n")
        return text

    draft = _build(tree, read_source=read_source)
    assert "fraisier-proj-systemctl-helper.service" not in draft.policy.units


def test_sockets_go_to_the_operator_without_a_note(tree):
    draft = _build(tree)
    assert not [n for n in draft.notes if ".socket" in n]


def test_a_unit_missing_from_the_render_is_not_granted(tree):
    def read_source(rel: str) -> str | None:
        if rel == "systemd/api.service":
            return None
        return (tree / rel).read_text() if (tree / rel).is_file() else None

    draft = _build(tree, read_source=read_source)
    assert "api.service" not in draft.policy.units
    assert "/etc/systemd/system/api.service" in draft.policy.operator_only
    assert any(n.startswith("api.service: not in the render") for n in draft.notes)
