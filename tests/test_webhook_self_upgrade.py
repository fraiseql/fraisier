"""Tests for webhook self-upgrade runner (issue #162)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fraisier.webhook_self_upgrade import (
    _build_install_cmd,
    _preflight_helper_allowlist,
    _run_upgrade,
    _spawn_upgrade,
    maybe_self_upgrade,
)


def _write_pyproject(tmp_path: Path, fraisier_pin: str | None) -> None:
    deps = "[]" if fraisier_pin is None else f'["fraisier=={fraisier_pin}"]'
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "myapp"\nversion = "1.0.0"\ndependencies = {deps}\n'
    )


class TestBuildInstallCmd:
    def test_matches_bootstrap_form(self):
        """The install command must mirror fraisier/bootstrap.py:221-222 exactly."""
        assert _build_install_cmd("0.16.6") == [
            "uv",
            "tool",
            "install",
            "--force",
            "--refresh-package",
            "fraisier",
            "fraisier==0.16.6",
        ]


class TestMaybeSelfUpgrade:
    def test_no_spawn_when_disabled(self, tmp_path):
        _write_pyproject(tmp_path, "99.0.0")
        spawn = MagicMock()
        maybe_self_upgrade(tmp_path, project_name="foo", enabled=False, spawn=spawn)
        spawn.assert_not_called()

    def test_no_spawn_when_required_is_none(self, tmp_path):
        # No pyproject.toml at all → detect returns None.
        spawn = MagicMock()
        maybe_self_upgrade(tmp_path, project_name="foo", enabled=True, spawn=spawn)
        spawn.assert_not_called()

    def test_no_spawn_when_required_equals_installed(self, tmp_path):
        _write_pyproject(tmp_path, "1.2.3")
        spawn = MagicMock()
        with patch(
            "fraisier.webhook_self_upgrade.importlib_metadata.version",
            return_value="1.2.3",
        ):
            maybe_self_upgrade(tmp_path, project_name="foo", enabled=True, spawn=spawn)
        spawn.assert_not_called()

    def test_no_spawn_when_required_older(self, tmp_path):
        _write_pyproject(tmp_path, "1.0.0")
        spawn = MagicMock()
        with patch(
            "fraisier.webhook_self_upgrade.importlib_metadata.version",
            return_value="1.2.3",
        ):
            maybe_self_upgrade(tmp_path, project_name="foo", enabled=True, spawn=spawn)
        spawn.assert_not_called()

    def test_spawn_when_required_newer(self, tmp_path):
        _write_pyproject(tmp_path, "2.0.0")
        spawn = MagicMock()
        with patch(
            "fraisier.webhook_self_upgrade.importlib_metadata.version",
            return_value="1.2.3",
        ):
            maybe_self_upgrade(tmp_path, project_name="foo", enabled=True, spawn=spawn)
        spawn.assert_called_once_with("2.0.0", "foo")

    def test_never_raises_on_internal_error(self, tmp_path):
        spawn = MagicMock(side_effect=RuntimeError("boom"))
        _write_pyproject(tmp_path, "99.0.0")
        # Must not raise even though spawn blows up.
        with patch(
            "fraisier.webhook_self_upgrade.importlib_metadata.version",
            return_value="1.2.3",
        ):
            maybe_self_upgrade(tmp_path, project_name="foo", enabled=True, spawn=spawn)

    def test_never_raises_on_malformed_installed_version(self, tmp_path):
        _write_pyproject(tmp_path, "1.2.3")
        spawn = MagicMock()
        with patch(
            "fraisier.webhook_self_upgrade.importlib_metadata.version",
            return_value="not-a-version",
        ):
            maybe_self_upgrade(tmp_path, project_name="foo", enabled=True, spawn=spawn)
        # Conservative: skip when we can't compare.
        spawn.assert_not_called()

    def test_preflight_rejection_skips_spawn_and_warns(
        self, tmp_path, monkeypatch, caplog
    ):
        """When the helper rejects the pre-flight (#218), don't spawn the worker.

        Without this, ``_spawn_upgrade`` installs the new binary in a detached
        subprocess whose restart RPC then fails silently in a per-event log
        under /var/lib/fraisier/self-upgrade/, leaving the webhook process on
        the old code with no signal in its journal.
        """
        import logging

        _write_pyproject(tmp_path, "2.0.0")
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", "/run/x.sock")
        spawn = MagicMock()
        with (
            patch(
                "fraisier.webhook_self_upgrade.importlib_metadata.version",
                return_value="1.2.3",
            ),
            patch(
                "fraisier.webhook_self_upgrade._call_via_socket",
                side_effect=subprocess.CalledProcessError(
                    1,
                    "is-active",
                    stderr="service not allowed: fraisier-foo-webhook.service",
                ),
            ),
            caplog.at_level(logging.WARNING, logger="fraisier.webhook_self_upgrade"),
        ):
            maybe_self_upgrade(tmp_path, project_name="foo", enabled=True, spawn=spawn)
        spawn.assert_not_called()
        assert "service not allowed" in caplog.text
        assert "scaffold-install" in caplog.text

    def test_preflight_pass_still_spawns(self, tmp_path, monkeypatch):
        """A clean pre-flight must not block the upgrade."""
        _write_pyproject(tmp_path, "2.0.0")
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", "/run/x.sock")
        spawn = MagicMock()
        with (
            patch(
                "fraisier.webhook_self_upgrade.importlib_metadata.version",
                return_value="1.2.3",
            ),
            patch(
                "fraisier.webhook_self_upgrade._call_via_socket",
                return_value=SimpleNamespace(stdout="active", stderr="", returncode=0),
            ),
        ):
            maybe_self_upgrade(tmp_path, project_name="foo", enabled=True, spawn=spawn)
        spawn.assert_called_once_with("2.0.0", "foo")

    def test_preflight_unreachable_socket_still_spawns(self, tmp_path, monkeypatch):
        """A transient socket failure must not punish the upgrade.

        ConnectionRefusedError can happen during webhook/helper startup races.
        Falling through to the worker preserves the existing install-anyway
        behaviour; the worker will log its own per-event failure if restart
        also fails.
        """
        _write_pyproject(tmp_path, "2.0.0")
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", "/run/x.sock")
        spawn = MagicMock()
        with (
            patch(
                "fraisier.webhook_self_upgrade.importlib_metadata.version",
                return_value="1.2.3",
            ),
            patch(
                "fraisier.webhook_self_upgrade._call_via_socket",
                side_effect=ConnectionRefusedError("socket missing"),
            ),
        ):
            maybe_self_upgrade(tmp_path, project_name="foo", enabled=True, spawn=spawn)
        spawn.assert_called_once_with("2.0.0", "foo")

    def test_preflight_no_socket_env_still_spawns(self, tmp_path, monkeypatch):
        """install-only mode (no helper configured) keeps working."""
        _write_pyproject(tmp_path, "2.0.0")
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_SOCKET", raising=False)
        spawn = MagicMock()
        with (
            patch(
                "fraisier.webhook_self_upgrade.importlib_metadata.version",
                return_value="1.2.3",
            ),
            patch("fraisier.webhook_self_upgrade._call_via_socket") as mock_socket,
        ):
            maybe_self_upgrade(tmp_path, project_name="foo", enabled=True, spawn=spawn)
        spawn.assert_called_once_with("2.0.0", "foo")
        mock_socket.assert_not_called()


class TestPreflightHelperAllowlist:
    def test_empty_socket_returns_none(self):
        assert _preflight_helper_allowlist("", "any.service") is None

    def test_rejection_returns_stderr(self):
        with patch(
            "fraisier.webhook_self_upgrade._call_via_socket",
            side_effect=subprocess.CalledProcessError(
                1, "is-active", stderr="service not allowed: x.service"
            ),
        ):
            result = _preflight_helper_allowlist("/run/x.sock", "x.service")
        assert result == "service not allowed: x.service"

    def test_inactive_service_is_not_a_rejection(self):
        """`systemctl is-active` returning exit 3 (inactive) is not an allowlist
        problem — the helper accepted the call, the service just isn't running.
        """
        with patch(
            "fraisier.webhook_self_upgrade._call_via_socket",
            side_effect=subprocess.CalledProcessError(
                3, "is-active", stderr="unknown error from systemctl helper"
            ),
        ):
            result = _preflight_helper_allowlist("/run/x.sock", "x.service")
        assert result is None

    def test_connection_refused_returns_none(self):
        with patch(
            "fraisier.webhook_self_upgrade._call_via_socket",
            side_effect=ConnectionRefusedError("nope"),
        ):
            result = _preflight_helper_allowlist("/run/x.sock", "x.service")
        assert result is None

    def test_clean_response_returns_none(self):
        with patch(
            "fraisier.webhook_self_upgrade._call_via_socket",
            return_value=SimpleNamespace(stdout="active", stderr="", returncode=0),
        ):
            result = _preflight_helper_allowlist("/run/x.sock", "x.service")
        assert result is None


class TestSpawnUpgrade:
    def test_spawn_uses_detached_kwargs(self, monkeypatch, tmp_path):
        monkeypatch.setattr("fraisier.webhook_self_upgrade._LOG_DIR", tmp_path / "logs")
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", "/run/x.sock")
        with patch("fraisier.webhook_self_upgrade.subprocess.Popen") as mock_popen:
            _spawn_upgrade("0.17.0", "myproj")
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == sys.executable
        assert "fraisier.webhook_self_upgrade" in cmd
        assert "--required" in cmd
        assert "0.17.0" in cmd
        assert "--service" in cmd
        assert "fraisier-myproj-webhook.service" in cmd
        assert "--socket" in cmd
        assert "/run/x.sock" in cmd
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.STDOUT
        # stdout should be a writable file handle, not None.
        assert kwargs["stdout"] is not None
        # Log directory was created on demand.
        assert (tmp_path / "logs").is_dir()

    def test_spawn_works_when_log_dir_uncreatable(self, monkeypatch):
        # Pointing _LOG_DIR at an unwritable path should not crash the spawn.
        monkeypatch.setattr(
            "fraisier.webhook_self_upgrade._LOG_DIR", Path("/dev/null/no")
        )
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", "/run/x.sock")
        with patch("fraisier.webhook_self_upgrade.subprocess.Popen") as mock_popen:
            _spawn_upgrade("0.17.0", "myproj")
        mock_popen.assert_called_once()
        _, kwargs = mock_popen.call_args
        # Falls back to DEVNULL when log file can't be opened.
        assert kwargs["stdout"] is subprocess.DEVNULL

    def test_spawn_with_no_socket_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr("fraisier.webhook_self_upgrade._LOG_DIR", tmp_path / "logs")
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_SOCKET", raising=False)
        with patch("fraisier.webhook_self_upgrade.subprocess.Popen") as mock_popen:
            _spawn_upgrade("0.17.0", "myproj")
        args, _ = mock_popen.call_args
        cmd = args[0]
        # --socket is still passed (empty string) so the child can choose to skip.
        idx = cmd.index("--socket")
        assert cmd[idx + 1] == ""


class TestRunUpgrade:
    def test_install_success_triggers_restart_rpc(self):
        with (
            patch("fraisier.webhook_self_upgrade.subprocess.run") as mock_run,
            patch("fraisier.drain_restart._call_via_socket") as mock_socket,
        ):
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            rc = _run_upgrade("0.17.0", "fraisier-foo-webhook.service", "/run/x.sock")
        assert rc == 0
        assert mock_run.call_args[0][0] == _build_install_cmd("0.17.0")
        mock_socket.assert_called_once_with(
            "/run/x.sock", "restart", "fraisier-foo-webhook.service"
        )

    def test_install_failure_skips_restart(self):
        with (
            patch("fraisier.webhook_self_upgrade.subprocess.run") as mock_run,
            patch("fraisier.drain_restart._call_via_socket") as mock_socket,
        ):
            mock_run.return_value = SimpleNamespace(
                returncode=1, stdout="", stderr="oops"
            )
            rc = _run_upgrade("0.17.0", "fraisier-foo-webhook.service", "/run/x.sock")
        assert rc == 1
        mock_socket.assert_not_called()

    def test_install_success_no_socket_logs_and_returns_ok(self, caplog):
        import logging

        with (
            patch("fraisier.webhook_self_upgrade.subprocess.run") as mock_run,
            patch("fraisier.drain_restart._call_via_socket") as mock_socket,
            caplog.at_level(logging.WARNING, logger="fraisier.webhook_self_upgrade"),
        ):
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            rc = _run_upgrade("0.17.0", "fraisier-foo-webhook.service", "")
        assert rc == 0
        mock_socket.assert_not_called()
        assert "FRAISIER_SYSTEMCTL_SOCKET" in caplog.text

    def test_restart_rpc_failure_returns_nonzero(self):
        with (
            patch("fraisier.webhook_self_upgrade.subprocess.run") as mock_run,
            patch(
                "fraisier.drain_restart._call_via_socket",
                side_effect=ConnectionRefusedError("nope"),
            ),
        ):
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            rc = _run_upgrade("0.17.0", "fraisier-foo-webhook.service", "/run/x.sock")
        assert rc != 0


@pytest.mark.parametrize("argv_socket", ["/run/x.sock", ""])
def test_main_entrypoint_invokes_run_upgrade(monkeypatch, argv_socket):
    """`python -m fraisier.webhook_self_upgrade` wires argv → _run_upgrade."""
    from fraisier import webhook_self_upgrade as mod

    captured = {}

    def fake_run(required, service, socket_path, **kwargs):
        captured["args"] = (required, service, socket_path)
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(mod, "_run_upgrade", fake_run)
    argv = [
        "webhook_self_upgrade",
        "--required",
        "0.17.0",
        "--service",
        "fraisier-foo-webhook.service",
        "--socket",
        argv_socket,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as excinfo:
        mod._main()
    assert excinfo.value.code == 0
    assert captured["args"] == (
        "0.17.0",
        "fraisier-foo-webhook.service",
        argv_socket,
    )


class TestWithDrainingFlag:
    """``_with_draining_flag`` context manager (#246)."""

    def test_touches_on_enter_and_clears_on_exit(self, tmp_path):
        from fraisier.locking import DRAINING_FLAG_NAME
        from fraisier.webhook_self_upgrade import _with_draining_flag

        flag = tmp_path / DRAINING_FLAG_NAME
        assert not flag.exists()
        with _with_draining_flag(tmp_path):
            assert flag.exists()
        assert not flag.exists()

    def test_clears_flag_even_when_body_raises(self, tmp_path):
        from fraisier.locking import DRAINING_FLAG_NAME
        from fraisier.webhook_self_upgrade import _with_draining_flag

        flag = tmp_path / DRAINING_FLAG_NAME
        with pytest.raises(RuntimeError, match="boom"), _with_draining_flag(tmp_path):
            assert flag.exists()
            raise RuntimeError("boom")
        assert not flag.exists()


class TestWaitForDeploysToDrain:
    """``_wait_for_deploys_to_drain`` (#246)."""

    def test_returns_drained_true_when_count_reaches_zero(self, tmp_path):
        from fraisier.webhook_self_upgrade import _wait_for_deploys_to_drain

        counts = iter([2, 1, 0])
        with patch(
            "fraisier.drain_restart.count_held_deployment_locks",
            side_effect=lambda _ld: next(counts),
        ):
            result = _wait_for_deploys_to_drain(tmp_path, 5, 0.01)
        assert result.drained is True
        assert result.held == []

    def test_returns_drained_false_with_held_basenames_on_timeout(self, tmp_path):
        from fraisier.webhook_self_upgrade import _wait_for_deploys_to_drain

        (tmp_path / "api.lock").touch()
        (tmp_path / "worker.lock").touch()
        with patch(
            "fraisier.drain_restart.count_held_deployment_locks",
            return_value=2,
        ):
            result = _wait_for_deploys_to_drain(tmp_path, 0.05, 0.01)
        assert result.drained is False
        assert set(result.held) == {"api.lock", "worker.lock"}


class TestSendRestart:
    """``_send_restart`` preserves today's rc semantics (#246)."""

    def test_returns_0_on_success(self):
        from fraisier.webhook_self_upgrade import _send_restart

        with patch(
            "fraisier.drain_restart._call_via_socket",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as mock_socket:
            rc = _send_restart("/run/x.sock", "fraisier-foo-webhook.service")
        assert rc == 0
        mock_socket.assert_called_once_with(
            "/run/x.sock", "restart", "fraisier-foo-webhook.service"
        )

    def test_returns_1_on_connection_refused(self):
        from fraisier.webhook_self_upgrade import _send_restart

        with patch(
            "fraisier.drain_restart._call_via_socket",
            side_effect=ConnectionRefusedError("no socket"),
        ):
            rc = _send_restart("/run/x.sock", "svc")
        assert rc == 1

    def test_returns_1_on_called_process_error(self):
        from fraisier.webhook_self_upgrade import _send_restart

        with patch(
            "fraisier.drain_restart._call_via_socket",
            side_effect=subprocess.CalledProcessError(1, "restart"),
        ):
            rc = _send_restart("/run/x.sock", "svc")
        assert rc == 1


class TestRunUpgradeDrainCoordination:
    """``_run_upgrade`` orchestrates flag → install → settle → drain → restart."""

    def test_run_upgrade_touches_flag_before_install_and_restarts_after_drain(
        self, tmp_path
    ):
        """Sequence: flag-on → install → sleep(settle) → wait_for_drain → flag-off → restart."""
        from fraisier.locking import DRAINING_FLAG_NAME
        from fraisier.webhook_self_upgrade import _DrainResult

        events: list[str] = []

        def fake_install(*_a, **_kw):
            events.append(
                "flag_set" if (tmp_path / DRAINING_FLAG_NAME).exists() else "flag_unset"
            )
            events.append("install")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def fake_sleep(_s):
            events.append("sleep")

        def fake_drain(*_a, **_kw):
            events.append(
                "flag_set" if (tmp_path / DRAINING_FLAG_NAME).exists() else "flag_unset"
            )
            events.append("drain")
            return _DrainResult(drained=True, held=[])

        def fake_socket(_sock, *args):
            events.append(
                "flag_set" if (tmp_path / DRAINING_FLAG_NAME).exists() else "flag_unset"
            )
            events.append(f"socket:{args[0]}")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch(
                "fraisier.webhook_self_upgrade.subprocess.run",
                side_effect=fake_install,
            ),
            patch("fraisier.webhook_self_upgrade.time.sleep", side_effect=fake_sleep),
            patch(
                "fraisier.webhook_self_upgrade._wait_for_deploys_to_drain",
                side_effect=fake_drain,
            ),
            patch(
                "fraisier.drain_restart._call_via_socket",
                side_effect=fake_socket,
            ),
        ):
            rc = _run_upgrade(
                "0.31.0",
                "fraisier-foo-webhook.service",
                "/run/x.sock",
                lock_dir=tmp_path,
                drain_timeout_s=10,
                drain_poll_s=0.01,
                drain_settle_s=0.5,
            )
        assert rc == 0
        # Flag must be set during install + drain, cleared before restart RPC.
        assert events == [
            "flag_set",
            "install",
            "sleep",
            "flag_set",
            "drain",
            "flag_unset",
            "socket:restart",
        ]
        # Flag is gone by the time _run_upgrade returns.
        assert not (tmp_path / DRAINING_FLAG_NAME).exists()

    def test_run_upgrade_skips_restart_on_drain_timeout(self, tmp_path, caplog):
        import logging

        from fraisier.locking import DRAINING_FLAG_NAME
        from fraisier.webhook_self_upgrade import _DRAIN_TIMEOUT_RC, _DrainResult

        with (
            patch(
                "fraisier.webhook_self_upgrade.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ),
            patch("fraisier.webhook_self_upgrade.time.sleep"),
            patch(
                "fraisier.webhook_self_upgrade._wait_for_deploys_to_drain",
                return_value=_DrainResult(drained=False, held=["api.lock"]),
            ),
            patch("fraisier.drain_restart._call_via_socket") as mock_socket,
            caplog.at_level(logging.WARNING, logger="fraisier.webhook_self_upgrade"),
        ):
            rc = _run_upgrade(
                "0.31.0",
                "fraisier-foo-webhook.service",
                "/run/x.sock",
                lock_dir=tmp_path,
                drain_timeout_s=1,
                drain_poll_s=0.01,
                drain_settle_s=0.01,
            )
        assert rc == _DRAIN_TIMEOUT_RC == 2
        mock_socket.assert_not_called()
        assert "drain timeout" in caplog.text
        assert "api.lock" in caplog.text
        assert not (tmp_path / DRAINING_FLAG_NAME).exists()

    def test_run_upgrade_install_failure_clears_flag_skips_drain_and_restart(
        self, tmp_path
    ):
        from fraisier.locking import DRAINING_FLAG_NAME

        with (
            patch(
                "fraisier.webhook_self_upgrade.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="bad"),
            ),
            patch(
                "fraisier.webhook_self_upgrade._wait_for_deploys_to_drain"
            ) as mock_drain,
            patch("fraisier.drain_restart._call_via_socket") as mock_socket,
        ):
            rc = _run_upgrade(
                "0.31.0",
                "fraisier-foo-webhook.service",
                "/run/x.sock",
                lock_dir=tmp_path,
                drain_timeout_s=10,
                drain_poll_s=0.01,
                drain_settle_s=0.01,
            )
        assert rc == 1
        mock_drain.assert_not_called()
        mock_socket.assert_not_called()
        assert not (tmp_path / DRAINING_FLAG_NAME).exists()

    def test_run_upgrade_no_lock_dir_skips_drain_and_warns(self, caplog):
        """When lock_dir is None, fall back to today's behaviour and warn."""
        import logging

        with (
            patch(
                "fraisier.webhook_self_upgrade.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ),
            patch(
                "fraisier.webhook_self_upgrade._wait_for_deploys_to_drain"
            ) as mock_drain,
            patch("fraisier.drain_restart._call_via_socket") as mock_socket,
            caplog.at_level(logging.WARNING, logger="fraisier.webhook_self_upgrade"),
        ):
            rc = _run_upgrade(
                "0.31.0",
                "fraisier-foo-webhook.service",
                "/run/x.sock",
                lock_dir=None,
            )
        assert rc == 0
        mock_drain.assert_not_called()
        mock_socket.assert_called_once_with(
            "/run/x.sock", "restart", "fraisier-foo-webhook.service"
        )
        assert "lock_dir unresolved" in caplog.text


class TestSpawnUpgradeArgvPlumbing:
    """``_spawn_upgrade`` reads config via .get() and propagates argv."""

    def test_spawn_passes_lock_dir_and_drain_knobs(self, monkeypatch, tmp_path):
        monkeypatch.setattr("fraisier.webhook_self_upgrade._LOG_DIR", tmp_path / "logs")
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", "/run/x.sock")

        stub_config = SimpleNamespace(
            webhook={
                "self_upgrade_drain_timeout_s": 300,
                "self_upgrade_drain_poll_s": 0.5,
                "self_upgrade_drain_settle_s": 1.0,
            },
            deployment=SimpleNamespace(lock_dir="/run/fraisier"),
        )

        with (
            patch(
                "fraisier.webhook_self_upgrade.get_config",
                return_value=stub_config,
            ),
            patch("fraisier.webhook_self_upgrade.subprocess.Popen") as mock_popen,
        ):
            _spawn_upgrade("0.31.0", "myproj")

        cmd = mock_popen.call_args[0][0]
        assert "--lock-dir" in cmd
        assert cmd[cmd.index("--lock-dir") + 1] == "/run/fraisier"
        assert "--drain-timeout" in cmd
        assert cmd[cmd.index("--drain-timeout") + 1] == "300"
        assert "--drain-poll" in cmd
        assert cmd[cmd.index("--drain-poll") + 1] == "0.5"
        assert "--drain-settle" in cmd
        assert cmd[cmd.index("--drain-settle") + 1] == "1.0"

    def test_spawn_defaults_when_config_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr("fraisier.webhook_self_upgrade._LOG_DIR", tmp_path / "logs")
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", "/run/x.sock")

        with (
            patch(
                "fraisier.webhook_self_upgrade.get_config",
                side_effect=FileNotFoundError("no config"),
            ),
            patch("fraisier.webhook_self_upgrade.subprocess.Popen") as mock_popen,
        ):
            _spawn_upgrade("0.31.0", "myproj")

        cmd = mock_popen.call_args[0][0]
        assert cmd[cmd.index("--lock-dir") + 1] == "/run/fraisier"
        assert cmd[cmd.index("--drain-timeout") + 1] == "600"
        assert cmd[cmd.index("--drain-poll") + 1] == "1.0"
        assert cmd[cmd.index("--drain-settle") + 1] == "2.0"


def test_main_entrypoint_passes_drain_knobs(monkeypatch):
    """`_main` parses the new argv flags and forwards them as kwargs."""
    from fraisier import webhook_self_upgrade as mod

    captured = {}

    def fake_run(required, service, socket_path, **kwargs):
        captured["args"] = (required, service, socket_path)
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(mod, "_run_upgrade", fake_run)
    argv = [
        "webhook_self_upgrade",
        "--required",
        "0.31.0",
        "--service",
        "fraisier-foo-webhook.service",
        "--socket",
        "/run/x.sock",
        "--lock-dir",
        "/tmp/run-fraisier",
        "--drain-timeout",
        "300",
        "--drain-poll",
        "0.5",
        "--drain-settle",
        "1.0",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as excinfo:
        mod._main()
    assert excinfo.value.code == 0
    kwargs = captured["kwargs"]
    assert kwargs["lock_dir"] == Path("/tmp/run-fraisier")
    assert kwargs["drain_timeout_s"] == 300
    assert kwargs["drain_poll_s"] == 0.5
    assert kwargs["drain_settle_s"] == 1.0


def test_main_entrypoint_empty_lock_dir_yields_none(monkeypatch):
    """Empty ``--lock-dir`` (operator-invoked) → ``lock_dir=None``."""
    from fraisier import webhook_self_upgrade as mod

    captured = {}

    def fake_run(required, service, socket_path, **kwargs):
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(mod, "_run_upgrade", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "webhook_self_upgrade",
            "--required",
            "0.31.0",
            "--service",
            "svc",
            "--socket",
            "",
        ],
    )
    with pytest.raises(SystemExit):
        mod._main()
    assert captured["kwargs"]["lock_dir"] is None
