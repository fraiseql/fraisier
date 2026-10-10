"""The unit-installer judges what it copies (#433, path 4; D7).

It copied the app's ``scripts/systemd/*`` into ``/etc/systemd/system`` as root
with the path checked and the content not: a unit there with no ``User=`` runs
as root. It now applies the same validator as the scaffold-install helper,
under the same root policy, plus a ``.service``/``.timer`` suffix allowlist.
Every op is judged before any is written, and what is written is the bytes
that were judged, never a second read of a file the deploy user can swap.
"""

from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING

import pytest

from fraisier import unit_installer_helper
from fraisier.root_policy import RootPolicy, RootPolicyError, UnitGrant
from fraisier.unit_installer_helper import (
    _execute_install_file_op,
    _handle_manifest,
    _resolve_allowlist,
)
from fraisier.unit_installer_protocol import (
    Allowlist,
    AllowlistEntry,
    InstallFileOp,
    Manifest,
    ManifestRejected,
    serialize_manifest,
    validate_manifest,
)

if TYPE_CHECKING:
    from pathlib import Path

POLICY = RootPolicy(
    project="demo",
    scaffold_dir="/var/lib/fraisier/demo/scaffold",
    users=frozenset({"postgres"}),
    groups=frozenset({"postgres"}),
    exec_prefixes=("/usr/bin/psql",),
    read_paths=frozenset(),
    directories=frozenset(),
    units={"api.service": UnitGrant("systemd/api.service", "plain")},
)

GOOD = "[Service]\nUser=postgres\nExecStart=/usr/bin/psql -c 'select 1'\n"


@pytest.fixture
def layout(tmp_path):
    src = tmp_path / "app" / "scripts" / "systemd"
    dest = tmp_path / "etc" / "systemd" / "system"
    src.mkdir(parents=True)
    dest.mkdir(parents=True)
    allowlist = Allowlist(
        entries=(AllowlistEntry(source_prefix=src, dest_prefix=dest),)
    )
    return src, dest, allowlist


def _send(layout, files: dict[str, str], *, policy_loader=lambda: POLICY) -> dict:
    src, dest, allowlist = layout
    ops = []
    for name, text in files.items():
        (src / name).write_text(text)
        ops.append(
            InstallFileOp(
                source_path=str(src / name), dest_path=str(dest / name), mode="0644"
            )
        )
    manifest = Manifest(version=1, deploy_id="t", operations=tuple(ops))
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client.sendall(serialize_manifest(manifest))
    client.shutdown(socket.SHUT_WR)
    _handle_manifest(
        server,
        allowlist=allowlist,
        resolved=_resolve_allowlist(allowlist),
        policy_loader=policy_loader,
    )
    with client.makefile("rb") as f:
        return json.loads(f.readline())


def test_a_good_unit_is_installed_byte_for_byte(layout):
    response = _send(layout, {"job.service": GOOD})
    assert response["status"] == "ok", response
    assert (layout[1] / "job.service").read_text() == GOOD


def test_a_root_unit_is_refused_by_rule_and_nothing_is_written(layout):
    root = GOOD.replace("User=postgres", "User=root")
    response = _send(layout, {"a.service": GOOD, "job.service": root})
    assert response["status"] == "rejected"
    assert "job.service" in response["reason"]
    assert "[user]" in response["reason"]
    assert list(layout[1].iterdir()) == []


def test_a_privileged_exec_line_is_refused(layout):
    response = _send(layout, {"job.service": GOOD + "ExecStartPre=+/usr/bin/psql\n"})
    assert response["status"] == "rejected"
    assert "[exec-prefix]" in response["reason"]


def test_a_unit_fraisier_installs_may_not_be_replaced(layout):
    response = _send(layout, {"api.service": GOOD})
    assert response["status"] == "rejected"
    assert "[unit-name]" in response["reason"]


def test_without_a_root_policy_nothing_is_installed(layout):
    def no_policy() -> RootPolicy:
        raise RootPolicyError(
            "no root policy at X: run `sudo fraisier scaffold-install`"
        )

    response = _send(layout, {"job.service": GOOD}, policy_loader=no_policy)
    assert response["status"] == "rejected"
    assert "sudo fraisier scaffold-install" in response["reason"]
    assert list(layout[1].iterdir()) == []


def test_the_judged_bytes_are_the_written_bytes(layout):
    src, dest, allowlist = layout
    (src / "job.service").write_text("[Service]\nUser=root\nExecStart=/bin/sh\n")
    op = InstallFileOp(
        source_path=str(src / "job.service"),
        dest_path=str(dest / "job.service"),
        mode="0644",
    )
    _execute_install_file_op(
        op, resolved=_resolve_allowlist(allowlist), data=GOOD.encode()
    )
    assert (dest / "job.service").read_text() == GOOD


@pytest.mark.parametrize("name", ["job.mount", "job.socket", "job.path", "job.conf"])
def test_the_protocol_refuses_any_suffix_but_service_and_timer(layout, name):
    src, dest, allowlist = layout
    (src / name).write_text(GOOD)
    op = InstallFileOp(
        source_path=str(src / name), dest_path=str(dest / name), mode="0644"
    )
    with pytest.raises(ManifestRejected, match=r"\.service or \.timer"):
        validate_manifest(
            Manifest(version=1, deploy_id="t", operations=(op,)), allowlist
        )


def test_a_file_swapped_after_it_was_judged_is_not_what_is_written(layout, monkeypatch):
    reads = iter([GOOD.encode(), b"[Service]\nUser=root\nExecStart=/bin/sh\n"])
    monkeypatch.setattr(
        unit_installer_helper, "_read_source", lambda *_a, **_k: next(reads)
    )
    response = _send(layout, {"job.service": GOOD})
    assert response["status"] == "ok"
    assert (layout[1] / "job.service").read_text() == GOOD


def test_production_refuses_until_main_installs_a_loader(layout):
    assert unit_installer_helper._policy_loader is unit_installer_helper._no_policy
    src, dest, allowlist = layout
    (src / "job.service").write_text(GOOD)
    op = InstallFileOp(
        source_path=str(src / "job.service"),
        dest_path=str(dest / "job.service"),
        mode="0644",
    )
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client.sendall(
        serialize_manifest(Manifest(version=1, deploy_id="t", operations=(op,)))
    )
    client.shutdown(socket.SHUT_WR)
    _handle_manifest(
        server, allowlist=allowlist, resolved=_resolve_allowlist(allowlist)
    )
    with client.makefile("rb") as f:
        assert json.loads(f.readline())["status"] == "rejected"
    assert not (dest / "job.service").exists()
