"""The generated install.sh must not restart the unit running the deploy (#349).

A deploy runs in-process inside either the webhook service or a deploy socket's
``<stem>@N.service`` instance, and a changed ``fraises.yaml`` makes that deploy
run ``install.sh``. Restarting either unit from there kills the deploy that
asked for the install.

The defect is *which* systemctl command runs, so these tests drive the rendered
script with a recording ``sudo`` on ``PATH`` rather than mocking the restart —
a mock asserting "we invoked systemctl restart" passes against the broken code.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import textwrap

import pytest

from fraisier.config import FraisierConfig
from fraisier.naming import deploy_socket_name
from fraisier.scaffold.renderer import ScaffoldRenderer

_YAML = """\
name: testapp
servers:
  example.com:
    machine_hostnames: [default-testrunner]

fraises:
  api:
    type: api
    environments:
      production:
        server: example.com

deployment:
  lock_dir: {lock_dir}

scaffold:
  deploy_user: testapp_deploy
"""

_WEBHOOK_UNIT = "fraisier-testapp-webhook.service"
_DEPLOY_SOCKET = deploy_socket_name({}, "production", "api")

_SUDO_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FRAISIER_TEST_SUDO_LOG"
exit 0
"""


def _render(tmp_path, lock_dir):
    cfg_path = tmp_path / "fraises.yaml"
    cfg_path.write_text(_YAML.format(lock_dir=lock_dir))
    renderer = ScaffoldRenderer(FraisierConfig(cfg_path))
    renderer.output_dir = tmp_path / "generated"
    renderer.render()
    return tmp_path / "generated" / "install.sh"


class _Harness:
    """Sources the rendered install.sh and runs a snippet against its guard."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.lock_dir = tmp_path / "lockdir"
        self.lock_dir.mkdir()
        self.install_sh = _render(tmp_path, self.lock_dir)
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.sudo_log = tmp_path / "sudo.log"
        sudo = self.bin / "sudo"
        sudo.write_text(_SUDO_STUB)
        sudo.chmod(0o755)

    def run(self, snippet: str, *, env=None, with_flock: bool = True, expect_ok=True):
        script = self.tmp_path / "harness.sh"
        script.write_text(
            f'. "{self.install_sh}"\n{textwrap.dedent(snippet)}\n',
        )
        run_env = {
            **os.environ,
            "FRAISIER_TEST_SUDO_LOG": str(self.sudo_log),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
        }
        run_env.pop("FRAISIER_DEPLOY_IN_FLIGHT", None)
        run_env.pop("FRAISIER_VIA_SCAFFOLD_INSTALL_HELPER", None)
        if not with_flock:
            # A PATH with the stub dir and the coreutils dir but no flock(1).
            stripped = self.tmp_path / "noflock"
            stripped.mkdir(exist_ok=True)
            for tool in ("bash", "realpath", "dirname", "grep", "sort", "cat", "rm"):
                found = shutil.which(tool)
                if found and not (stripped / tool).exists():
                    (stripped / tool).symlink_to(found)
            run_env["PATH"] = f"{self.bin}:{stripped}"
        run_env.update(env or {})
        result = subprocess.run(
            ["bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=run_env,
        )
        if expect_ok:
            # An undefined function exits non-zero and leaves an empty sudo log,
            # which is exactly what a deferral looks like. Assert the snippet
            # actually ran before any test reads that emptiness as evidence.
            assert result.returncode == 0, (
                f"harness snippet failed: {result.stderr}\n{result.stdout}"
            )
        return result

    def sudo_calls(self) -> list[str]:
        if not self.sudo_log.exists():
            return []
        return [ln for ln in self.sudo_log.read_text().splitlines() if ln.strip()]

    def ledger(self) -> list[str]:
        path = self.lock_dir / ".deferred-restarts"
        if not path.exists():
            return []
        return [ln for ln in path.read_text().splitlines() if ln.strip()]


@pytest.fixture
def harness(tmp_path):
    return _Harness(tmp_path)


# --------------------------------------------------------------------------
# The seam behaves: restart when free, defer when a deploy is in flight.
# --------------------------------------------------------------------------


class TestRestartsWhenNoDeployIsInFlight:
    def test_webhook_is_restarted(self, harness):
        result = harness.run(f"_restart_deploy_host_unit {_WEBHOOK_UNIT}")
        assert result.returncode == 0, result.stderr
        assert any(
            f"systemctl restart {_WEBHOOK_UNIT}" in call
            for call in harness.sudo_calls()
        ), harness.sudo_calls()

    def test_restart_announces_its_intent_before_issuing_it(self, harness):
        """A SIGKILLed deploy cannot report; the restart that causes it can."""
        result = harness.run(f"_restart_deploy_host_unit {_WEBHOOK_UNIT}")
        assert "Restarting" in result.stdout
        assert _WEBHOOK_UNIT in result.stdout
        assert "terminates" in result.stdout.lower()

    def test_nothing_is_recorded_as_deferred(self, harness):
        harness.run(f"_restart_deploy_host_unit {_WEBHOOK_UNIT}")
        assert harness.ledger() == []


class TestDefersUnderALiveDeploy:
    def test_caller_declaration_defers_the_restart(self, harness):
        result = harness.run(
            f"_restart_deploy_host_unit {_WEBHOOK_UNIT}",
            env={"FRAISIER_DEPLOY_IN_FLIGHT": "1"},
        )
        assert result.returncode == 0, result.stderr
        assert harness.sudo_calls() == []

    def test_helper_marker_defers_the_restart(self, harness):
        """An older webhook over the socket sets only the legacy marker."""
        harness.run(
            f"_restart_deploy_host_unit {_WEBHOOK_UNIT}",
            env={"FRAISIER_VIA_SCAFFOLD_INSTALL_HELPER": "1"},
        )
        assert harness.sudo_calls() == []

    def test_a_held_deployment_lock_defers_the_restart(self, harness):
        """The probe is the backstop for a caller that does not declare."""
        lock = harness.lock_dir / "api.lock"
        lock.touch()
        with lock.open("w") as fd:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            harness.run(f"_restart_deploy_host_unit {_WEBHOOK_UNIT}")
        assert harness.sudo_calls() == []

    def test_a_free_lock_file_does_not_defer(self, harness):
        """A lock file left behind by a finished deploy is not a live deploy."""
        (harness.lock_dir / "api.lock").touch()
        harness.run(f"_restart_deploy_host_unit {_WEBHOOK_UNIT}")
        assert any(
            f"systemctl restart {_WEBHOOK_UNIT}" in call
            for call in harness.sudo_calls()
        )

    def test_deferral_names_the_unit_and_the_signal(self, harness):
        result = harness.run(
            f"_restart_deploy_host_unit {_WEBHOOK_UNIT}",
            env={"FRAISIER_DEPLOY_IN_FLIGHT": "1"},
        )
        assert "Deferring restart" in result.stdout
        assert _WEBHOOK_UNIT in result.stdout
        assert "declared" in result.stdout

    def test_deploy_socket_is_deferred_too(self, harness):
        """The deploy socket propagates a restart to the deploy-daemon instance."""
        harness.run(
            f"_restart_deploy_host_unit {_DEPLOY_SOCKET}",
            env={"FRAISIER_DEPLOY_IN_FLIGHT": "1"},
        )
        assert harness.sudo_calls() == []


class TestLockProbeSideEffects:
    def test_probe_creates_no_lock_files(self, harness):
        """Creating a root-owned lock file would break the deploy user's open('w')."""
        harness.run(f"_restart_deploy_host_unit {_WEBHOOK_UNIT}")
        assert list(harness.lock_dir.glob("*.lock")) == []

    def test_missing_flock_warns_and_still_restarts(self, harness):
        """A backstop that cannot run must not block a manual re-bake."""
        result = harness.run(
            f"_restart_deploy_host_unit {_WEBHOOK_UNIT}", with_flock=False
        )
        assert "could not probe deployment locks" in result.stderr
        assert any(
            f"systemctl restart {_WEBHOOK_UNIT}" in call
            for call in harness.sudo_calls()
        )

    def test_missing_flock_does_not_override_a_declaring_caller(self, harness):
        """The caller's declaration is authoritative and never needs the probe."""
        result = harness.run(
            f"_restart_deploy_host_unit {_WEBHOOK_UNIT}",
            env={"FRAISIER_DEPLOY_IN_FLIGHT": "1"},
            with_flock=False,
        )
        assert harness.sudo_calls() == []
        assert "could not probe" not in result.stderr


# --------------------------------------------------------------------------
# The debt is recorded, so nothing pays it silently or forgets it.
# --------------------------------------------------------------------------


class TestDeferredRestartLedger:
    def test_deferred_unit_is_recorded(self, harness):
        harness.run(
            f"_restart_deploy_host_unit {_WEBHOOK_UNIT}\n_report_deferred_restarts",
            env={"FRAISIER_DEPLOY_IN_FLIGHT": "1"},
        )
        assert harness.ledger() == [_WEBHOOK_UNIT]

    def test_ledger_merges_with_an_unpaid_entry_from_an_earlier_install(self, harness):
        (harness.lock_dir / ".deferred-restarts").write_text(f"{_DEPLOY_SOCKET}\n")
        harness.run(
            f"_restart_deploy_host_unit {_WEBHOOK_UNIT}\n_report_deferred_restarts",
            env={"FRAISIER_DEPLOY_IN_FLIGHT": "1"},
        )
        assert sorted(harness.ledger()) == sorted([_WEBHOOK_UNIT, _DEPLOY_SOCKET])

    def test_ledger_does_not_duplicate_a_unit_already_pending(self, harness):
        (harness.lock_dir / ".deferred-restarts").write_text(f"{_WEBHOOK_UNIT}\n")
        harness.run(
            f"_restart_deploy_host_unit {_WEBHOOK_UNIT}\n_report_deferred_restarts",
            env={"FRAISIER_DEPLOY_IN_FLIGHT": "1"},
        )
        assert harness.ledger() == [_WEBHOOK_UNIT]

    def test_summary_lists_what_is_still_running_its_old_unit(self, harness):
        result = harness.run(
            f"_restart_deploy_host_unit {_WEBHOOK_UNIT}\n_report_deferred_restarts",
            env={"FRAISIER_DEPLOY_IN_FLIGHT": "1"},
        )
        assert "Deferred restarts" in result.stdout
        assert _WEBHOOK_UNIT in result.stdout

    def test_no_summary_and_no_ledger_when_nothing_was_deferred(self, harness):
        result = harness.run("_report_deferred_restarts")
        assert "Deferred restarts" not in result.stdout
        assert harness.ledger() == []


# --------------------------------------------------------------------------
# The guard: no deploy-hosting unit is restarted outside the seam.
# --------------------------------------------------------------------------

_SEAM = "_restart_deploy_host_unit"


def _unguarded_deploy_host_restarts(text: str, units: set[str]) -> list[str]:
    """Lines that restart a deploy-hosting unit outside the seam function.

    The seam is the only place these units may be restarted; inside it the unit
    is a shell variable, never a literal, so any literal occurrence in a
    ``systemctl restart`` line is by definition a bypass.
    """
    offenders = []
    in_seam = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{_SEAM}()"):
            in_seam = True
            continue
        if in_seam and stripped == "}":
            in_seam = False
            continue
        if in_seam or "systemctl restart" not in line:
            continue
        if any(unit in line for unit in units):
            offenders.append(line.strip())
    return offenders


class TestNoUnguardedDeployHostRestart:
    """A restart of a deploy-hosting unit outside the seam is the whole bug."""

    @pytest.fixture
    def rendered(self, tmp_path):
        return _render(tmp_path, tmp_path / "lockdir").read_text()

    def test_no_deploy_hosting_unit_is_restarted_outside_the_seam(self, rendered):
        offenders = _unguarded_deploy_host_restarts(
            rendered, {_WEBHOOK_UNIT, _DEPLOY_SOCKET}
        )
        assert offenders == [], (
            "these lines restart a unit that may be running a deploy; route them "
            f"through {_SEAM}: {offenders}"
        )

    def test_the_seam_is_actually_used(self, rendered):
        """A guard that nothing calls is indistinguishable from a passing one."""
        calls = [ln for ln in rendered.splitlines() if ln.strip().startswith(_SEAM)]
        assert len(calls) >= 2, (
            "expected the webhook unit and at least one deploy socket to be "
            f"restarted through {_SEAM}, found: {calls}"
        )

    def test_guard_detects_a_bypass(self, rendered):
        """Meta-test: a tree-scanning guard that matches nothing always passes."""
        mutated = rendered + (
            f"\n_run sudo systemctl restart {_WEBHOOK_UNIT} || true\n"
        )
        offenders = _unguarded_deploy_host_restarts(
            mutated, {_WEBHOOK_UNIT, _DEPLOY_SOCKET}
        )
        assert offenders, "the guard cannot detect an unguarded restart"

    def test_guard_ignores_units_that_do_not_host_deploys(self, rendered):
        """The install-helper re-bake (#279) must stay outside the seam."""
        offenders = _unguarded_deploy_host_restarts(
            rendered + "\n_run sudo systemctl restart some-other.service\n",
            {_WEBHOOK_UNIT, _DEPLOY_SOCKET},
        )
        assert offenders == []
