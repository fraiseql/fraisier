"""What the root helper does with a deploy's render: apply what the policy allows (#433).

The render is untrusted input. Nothing in it decides what root accepts; the
root policy does. A unit that fails the validator, a root-owned file that only
an operator may change, or an artifact the policy has never seen stops the
whole apply before anything is written.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from fraisier.root_policy import RootPolicy, UnitGrant
from fraisier.scaffold_apply import (
    SafeTree,
    UnitWriter,
    UnsafeTreeError,
    apply_scaffold,
    read_installed,
)

SYSTEMD = "/etc/systemd/system"

UNIT = """\
[Service]
User=deploy
ExecStart=/home/deploy/.local/bin/fraisier {arg}
"""

POLICY = RootPolicy(
    project="demo",
    scaffold_dir="/scaffold",
    users=frozenset({"deploy"}),
    groups=frozenset({"deploy"}),
    exec_prefixes=("/home/deploy/.local/bin/",),
    read_paths=frozenset(),
    directories=frozenset(),
    units={
        "app.service": UnitGrant("systemd/app.service", "plain"),
        "prune.service": UnitGrant("systemd/prune.service", "timer"),
        "prune.timer": UnitGrant("systemd/prune.timer", "timer"),
        "demo-webhook.service": UnitGrant("demo-webhook-solo.service", "webhook"),
        "ih.service": UnitGrant("systemd/ih.service", "install_helper"),
    },
    operator_only={
        "/etc/sudoers.d/demo": "sudoers",
        f"{SYSTEMD}/demo-systemctl-helper.service": "systemd/demo-systemctl-helper.service",
    },
)

TIMER = "[Timer]\nOnCalendar=daily\nUnit=prune.service\n"


def _manifest(extra: list[dict] | None = None) -> bytes:
    artifacts = [
        {"source": g.source, "destination": f"{SYSTEMD}/{n}", "disposition": "plain"}
        for n, g in POLICY.units.items()
        if g.action != "webhook"
    ]
    artifacts.append(
        {
            "source": "demo-webhook-solo.service",
            "destination": f"{SYSTEMD}/demo-webhook.service",
            "disposition": "webhook",
        }
    )
    artifacts += [
        {"source": s, "destination": d, "disposition": "sudoers"}
        for d, s in POLICY.operator_only.items()
    ]
    payload = {
        "schema_version": 2,
        "artifacts": artifacts + (extra or []),
        "hosts": {
            "solo": {
                "scopes": [],
                "environments": [],
                "webhook": "demo-webhook-solo.service",
            }
        },
    }
    return json.dumps(payload).encode()


def _baseline() -> tuple[dict[str, bytes], dict[str, bytes]]:
    """A tree and an installed system that agree."""
    tree = {
        "artifact-manifest.json": _manifest(),
        "systemd/app.service": UNIT.format(arg="serve").encode(),
        "systemd/prune.service": UNIT.format(arg="prune").encode(),
        "systemd/prune.timer": TIMER.encode(),
        "demo-webhook-solo.service": UNIT.format(arg="webhook").encode(),
        "systemd/ih.service": UNIT.format(arg="ih").encode(),
        "sudoers": b"deploy ALL=(root) NOPASSWD: /usr/bin/true\n",
        "systemd/demo-systemctl-helper.service": b"[Service]\nExecStart=/x\n",
    }
    installed = {f"{SYSTEMD}/{n}": tree[g.source] for n, g in POLICY.units.items()} | {
        d: tree[s] for d, s in POLICY.operator_only.items()
    }
    return tree, installed


class _Host:
    def __init__(self, tree, installed, *, systemctl_ok=True):
        self.tree, self.installed = tree, installed
        self.written: dict[str, bytes] = {}
        self.calls: list[tuple[str, ...]] = []
        self.systemctl_ok = systemctl_ok

    def write(self, name: str, data: bytes) -> None:
        self.written[name] = data

    def systemctl(self, *args: str) -> bool:
        self.calls.append(args)
        return self.systemctl_ok

    def apply(self, *, deploy_in_flight=True, policy=POLICY):
        return apply_scaffold(
            policy,
            read_tree=self.tree.get,
            read_installed=self.installed.get,
            write_unit=self.write,
            systemctl=self.systemctl,
            hostname="solo",
            deploy_in_flight=deploy_in_flight,
        )


def _changed(source: str, arg: str = "changed"):
    tree, installed = _baseline()
    tree[source] = UNIT.format(arg=arg).encode()
    return _Host(tree, installed)


def test_an_unchanged_tree_writes_nothing():
    host = _Host(*_baseline())
    outcome = host.apply()
    assert outcome.ok
    assert host.written == {}
    assert host.calls == []
    assert sorted(outcome.unchanged) == sorted(POLICY.units)


def test_a_changed_plain_unit_is_written_and_reloaded():
    host = _changed("systemd/app.service")
    outcome = host.apply()
    assert outcome.ok
    assert list(host.written) == ["app.service"]
    assert host.calls == [("daemon-reload",)]
    assert outcome.installed == ["app.service"]


def test_a_changed_timer_is_enabled_after_the_reload():
    tree, installed = _baseline()
    tree["systemd/prune.timer"] = (TIMER + "Persistent=true\n").encode()
    host = _Host(tree, installed)
    assert host.apply().ok
    assert host.calls == [("daemon-reload",), ("enable", "--now", "prune.timer")]


def test_the_webhook_restart_is_deferred_under_a_deploy():
    host = _changed("demo-webhook-solo.service")
    outcome = host.apply(deploy_in_flight=True)
    assert outcome.ok
    assert outcome.deferred_restarts == ["demo-webhook.service"]
    assert ("restart", "demo-webhook.service") not in host.calls


def test_the_webhook_is_restarted_when_no_deploy_runs():
    host = _changed("demo-webhook-solo.service")
    outcome = host.apply(deploy_in_flight=False)
    assert outcome.deferred_restarts == []
    assert host.calls == [("daemon-reload",), ("restart", "demo-webhook.service")]


def test_a_timers_service_is_written_but_not_enabled():
    host = _changed("systemd/prune.service")
    assert host.apply().ok
    assert host.calls == [("daemon-reload",)]


def test_a_policy_unit_name_that_is_not_a_unit_is_refused():
    policy = replace(
        POLICY, units={"../x.service": UnitGrant("systemd/app.service", "plain")}
    )
    outcome = _Host(*_baseline()).apply(policy=policy)
    assert [r.rule for r in outcome.refused["../x.service"]] == ["unit-name"]


def test_a_rebaked_install_helper_is_stopped_so_its_socket_re_execs_it():
    host = _changed("systemd/ih.service")
    assert host.apply().ok
    assert host.calls == [("daemon-reload",), ("stop", "ih.service")]


def test_a_refused_unit_stops_everything_and_names_its_rule():
    tree, installed = _baseline()
    tree["systemd/app.service"] = b"[Service]\nUser=root\nExecStart=/bin/sh\n"
    tree["systemd/ih.service"] = UNIT.format(arg="changed").encode()
    host = _Host(tree, installed)
    outcome = host.apply()
    assert not outcome.ok
    assert host.written == {}
    assert host.calls == []
    assert {r.rule for r in outcome.refused["app.service"]} == {"user", "exec-path"}


def test_an_operator_only_file_that_changed_is_pending():
    tree, installed = _baseline()
    tree["sudoers"] = b"deploy ALL=(root) NOPASSWD: ALL\n"
    tree["systemd/app.service"] = UNIT.format(arg="changed").encode()
    host = _Host(tree, installed)
    outcome = host.apply()
    assert not outcome.ok
    assert host.written == {}
    assert outcome.pending == ["/etc/sudoers.d/demo: differs from the render"]


def test_an_operator_only_file_that_is_missing_is_pending():
    tree, installed = _baseline()
    del installed["/etc/sudoers.d/demo"]
    outcome = _Host(tree, installed).apply()
    assert outcome.pending == ["/etc/sudoers.d/demo: not installed"]


def test_an_artifact_the_policy_has_never_seen_is_pending():
    tree, installed = _baseline()
    new = {"source": "systemd/new.service", "destination": f"{SYSTEMD}/new.service"}
    tree["artifact-manifest.json"] = _manifest([{**new, "disposition": "plain"}])
    tree["systemd/new.service"] = UNIT.format(arg="new").encode()
    outcome = _Host(tree, installed).apply()
    assert not outcome.ok
    assert outcome.pending == [
        f"{SYSTEMD}/new.service: new, never approved by an operator"
    ]


def test_another_hosts_artifact_is_not_pending():
    tree, installed = _baseline()
    other = {
        "source": "systemd/other.service",
        "destination": f"{SYSTEMD}/other.service",
        "disposition": "plain",
        "fraise": "x",
        "environment": "elsewhere",
    }
    tree["artifact-manifest.json"] = _manifest([other])
    assert _Host(tree, installed).apply().ok


@pytest.mark.parametrize("manifest", [None, b"not json", b"[]"])
def test_an_unreadable_manifest_is_pending(manifest):
    tree, installed = _baseline()
    if manifest is None:
        del tree["artifact-manifest.json"]
    else:
        tree["artifact-manifest.json"] = manifest
    outcome = _Host(tree, installed).apply()
    assert not outcome.ok
    assert outcome.pending[0].startswith("artifact-manifest.json:")


def test_a_host_the_render_does_not_know_is_pending():
    tree, installed = _baseline()
    tree["artifact-manifest.json"] = _manifest().replace(b'"solo"', b'"other"')
    outcome = _Host(tree, installed).apply()
    assert outcome.pending == [
        "artifact-manifest.json: this host (solo) is not in the render"
    ]


def test_a_granted_unit_missing_from_the_render_is_pending():
    tree, installed = _baseline()
    del tree["systemd/app.service"]
    outcome = _Host(tree, installed).apply()
    assert outcome.pending == ["app.service: systemd/app.service is not in the render"]


def test_an_unsafe_tree_entry_is_refused():
    tree, installed = _baseline()

    def read_tree(rel: str) -> bytes | None:
        if rel == "systemd/app.service":
            raise UnsafeTreeError("systemd/app.service is a symlink")
        return tree.get(rel)

    outcome = apply_scaffold(
        POLICY,
        read_tree=read_tree,
        read_installed=installed.get,
        write_unit=lambda *_: None,
        systemctl=lambda *_: True,
        hostname="solo",
        deploy_in_flight=True,
    )
    assert [r.rule for r in outcome.refused["app.service"]] == ["tree"]


def test_a_failed_systemctl_call_fails_the_apply():
    tree, installed = _baseline()
    tree["systemd/app.service"] = UNIT.format(arg="changed").encode()
    outcome = _Host(tree, installed, systemctl_ok=False).apply()
    assert not outcome.ok
    assert outcome.failed == ["systemctl daemon-reload"]


def test_a_render_that_asks_for_root_cannot_widen_the_policy():
    """The config and manifest in the tree are data; only the policy decides."""
    tree, installed = _baseline()
    tree["fraises.yaml"] = (
        b"fraises:\n  app:\n    environments:\n      p:\n        service: {user: root}\n"
    )
    tree["systemd/app.service"] = (
        UNIT.replace("User=deploy", "User=root").format(arg="x").encode()
    )
    outcome = _Host(tree, installed).apply()
    assert [r.rule for r in outcome.refused["app.service"]] == ["user"]


# ---------------------------------------------------------------------------
# The real readers and writer, on a tmp root
# ---------------------------------------------------------------------------


class TestSafeTree:
    def test_reads_a_regular_file(self, tmp_path):
        (tmp_path / "systemd").mkdir()
        (tmp_path / "systemd" / "a.service").write_bytes(b"x")
        assert SafeTree(tmp_path).read("systemd/a.service") == b"x"

    def test_a_missing_file_is_none(self, tmp_path):
        assert SafeTree(tmp_path).read("systemd/a.service") is None

    def test_a_symlinked_file_is_refused(self, tmp_path):
        (tmp_path / "a.service").symlink_to("/etc/hostname")
        with pytest.raises(UnsafeTreeError):
            SafeTree(tmp_path).read("a.service")

    def test_a_symlinked_directory_is_refused(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "a.service").write_bytes(b"x")
        (tmp_path / "systemd").symlink_to(real)
        with pytest.raises(UnsafeTreeError):
            SafeTree(tmp_path).read("systemd/a.service")

    def test_a_symlink_above_the_tree_is_refused(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "a.service").write_bytes(b"x")
        (tmp_path / "link").symlink_to(real)
        with pytest.raises(UnsafeTreeError):
            SafeTree(tmp_path / "link").read("a.service")

    def test_a_hard_link_is_refused(self, tmp_path):
        (tmp_path / "x").write_bytes(b"x")
        os.link(tmp_path / "x", tmp_path / "a.service")
        with pytest.raises(UnsafeTreeError):
            SafeTree(tmp_path).read("a.service")

    @pytest.mark.parametrize("rel", ["../x", "/etc/shadow", "a/../b", "", "a//b"])
    def test_a_path_that_leaves_the_tree_is_refused(self, tmp_path, rel):
        with pytest.raises(UnsafeTreeError):
            SafeTree(tmp_path).read(rel)

    def test_an_oversized_file_is_refused(self, tmp_path):
        (tmp_path / "a.service").write_bytes(b"x" * (2 * 1024 * 1024))
        with pytest.raises(UnsafeTreeError):
            SafeTree(tmp_path).read("a.service")

    def test_a_fifo_is_refused(self, tmp_path):
        os.mkfifo(tmp_path / "a.service")
        with pytest.raises(UnsafeTreeError, match="not a regular file"):
            SafeTree(tmp_path).read("a.service")

    def test_a_directory_is_refused(self, tmp_path):
        (tmp_path / "a.service").mkdir()
        with pytest.raises(UnsafeTreeError):
            SafeTree(tmp_path).read("a.service")


class TestUnitWriter:
    def test_writes_0644(self, tmp_path):
        UnitWriter(tmp_path).write("a.service", b"x")
        path = tmp_path / "a.service"
        assert path.read_bytes() == b"x"
        assert path.stat().st_mode & 0o777 == 0o644

    def test_replaces_a_symlink_without_following_it(self, tmp_path):
        target = tmp_path / "target"
        target.write_bytes(b"keep")
        (tmp_path / "a.service").symlink_to(target)
        UnitWriter(tmp_path).write("a.service", b"new")
        assert target.read_bytes() == b"keep"
        assert not (tmp_path / "a.service").is_symlink()
        assert (tmp_path / "a.service").read_bytes() == b"new"

    @pytest.mark.parametrize("name", ["../a.service", "a/b.service", ".a.service", ""])
    def test_refuses_a_name_that_is_not_a_plain_file_name(self, tmp_path, name):
        with pytest.raises(ValueError, match="unit name"):
            UnitWriter(tmp_path).write(name, b"x")

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        UnitWriter(tmp_path).write("a.service", b"x")
        assert [p.name for p in tmp_path.iterdir()] == ["a.service"]


class TestReadInstalled:
    def test_reads_a_file(self, tmp_path):
        (tmp_path / "f").write_bytes(b"x")
        assert read_installed(str(tmp_path / "f")) == b"x"

    def test_missing_is_none(self, tmp_path):
        assert read_installed(str(tmp_path / "f")) is None

    def test_a_non_regular_file_reads_as_differing(self, tmp_path):
        os.mkfifo(tmp_path / "f")
        assert read_installed(str(tmp_path / "f")) == b""

    def test_a_directory_reads_as_differing(self, tmp_path):
        (tmp_path / "f").mkdir()
        assert read_installed(str(tmp_path / "f")) == b""

    def test_a_masked_unit_reads_as_differing(self, tmp_path):
        (tmp_path / "f").symlink_to("/dev/null")
        assert read_installed(str(tmp_path / "f")) == b""
