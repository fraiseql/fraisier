"""A restart install.sh deferred is a debt, and something has to pay it (#349).

Skipping the restart alone would leave the webhook running its previous unit —
old ReadWritePaths=, old Environment= — which is how a fraises.yaml change that
adds an environment makes the *next* deploy fail on a read-only filesystem.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fraisier.deferred_restart import (
    DEFERRED_RESTART_FILE,
    maybe_apply_deferred_restarts,
    read_deferred_restarts,
    run_deferred_restarts,
    settle_deferred_restarts,
)


@pytest.fixture
def lock_dir(tmp_path):
    d = tmp_path / "run-fraisier"
    d.mkdir()
    return d


def _write_ledger(lock_dir, *units):
    (lock_dir / DEFERRED_RESTART_FILE).write_text("".join(f"{u}\n" for u in units))


class TestReadTheLedger:
    def test_no_ledger_is_no_debt(self, lock_dir):
        assert read_deferred_restarts(lock_dir) == []

    def test_entries_are_returned_in_order(self, lock_dir):
        _write_ledger(lock_dir, "b.service", "a.socket")
        assert read_deferred_restarts(lock_dir) == ["b.service", "a.socket"]

    def test_blank_lines_are_ignored(self, lock_dir):
        (lock_dir / DEFERRED_RESTART_FILE).write_text("a.service\n\n  \nb.service\n")
        assert read_deferred_restarts(lock_dir) == ["a.service", "b.service"]

    def test_a_missing_lock_dir_is_no_debt(self, tmp_path):
        assert read_deferred_restarts(tmp_path / "nope") == []

    def test_unreadable_ledger_is_reported_as_no_debt(self, lock_dir, caplog):
        """A debt we cannot read must not crash the deploy that just succeeded."""
        with patch(
            "fraisier.deferred_restart.Path.read_text", side_effect=OSError("boom")
        ):
            assert read_deferred_restarts(lock_dir) == []


class TestSettleTheLedger:
    def test_paid_entries_are_removed(self, lock_dir):
        _write_ledger(lock_dir, "a.service", "b.socket")
        settle_deferred_restarts(lock_dir, paid=["a.service"])
        assert read_deferred_restarts(lock_dir) == ["b.socket"]

    def test_the_file_goes_when_the_debt_is_cleared(self, lock_dir):
        _write_ledger(lock_dir, "a.service")
        settle_deferred_restarts(lock_dir, paid=["a.service"])
        assert not (lock_dir / DEFERRED_RESTART_FILE).exists()

    def test_an_unpaid_entry_survives_so_doctor_still_reports_it(self, lock_dir):
        _write_ledger(lock_dir, "a.service", "b.socket")
        settle_deferred_restarts(lock_dir, paid=[])
        assert read_deferred_restarts(lock_dir) == ["a.service", "b.socket"]


class TestRunDeferredRestarts:
    """flag -> settle -> drain -> restart each -> settle the ledger."""

    def test_restart_is_sent_after_the_drain(self, lock_dir):
        _write_ledger(lock_dir, "fraisier-p-webhook.service")
        with (
            patch("fraisier.deferred_restart.time.sleep"),
            patch(
                "fraisier.deferred_restart.wait_for_deploys_to_drain",
                return_value=MagicMock(drained=True, held=[]),
            ),
            patch("fraisier.drain_restart._call_via_socket") as mock_socket,
        ):
            rc = run_deferred_restarts("/run/x.sock", lock_dir=lock_dir)
        assert rc == 0
        mock_socket.assert_called_once_with(
            "/run/x.sock", "restart", "fraisier-p-webhook.service"
        )
        assert read_deferred_restarts(lock_dir) == []

    def test_the_draining_flag_is_raised_for_the_whole_window(self, lock_dir):
        _write_ledger(lock_dir, "fraisier-p-webhook.service")
        seen = []

        def _drain(*_args, **_kwargs):
            seen.append((lock_dir / ".draining").exists())
            return MagicMock(drained=True, held=[])

        with (
            patch("fraisier.deferred_restart.time.sleep"),
            patch("fraisier.deferred_restart.wait_for_deploys_to_drain", _drain),
            patch("fraisier.drain_restart._call_via_socket"),
        ):
            run_deferred_restarts("/run/x.sock", lock_dir=lock_dir)
        assert seen == [True]
        assert not (lock_dir / ".draining").exists()

    def test_drain_timeout_sends_nothing_and_keeps_the_debt(self, lock_dir, caplog):
        _write_ledger(lock_dir, "fraisier-p-webhook.service")
        with (
            patch("fraisier.deferred_restart.time.sleep"),
            patch(
                "fraisier.deferred_restart.wait_for_deploys_to_drain",
                return_value=MagicMock(drained=False, held=["api.lock"]),
            ),
            patch("fraisier.drain_restart._call_via_socket") as mock_socket,
        ):
            rc = run_deferred_restarts("/run/x.sock", lock_dir=lock_dir)
        assert rc != 0
        mock_socket.assert_not_called()
        assert read_deferred_restarts(lock_dir) == ["fraisier-p-webhook.service"]

    def test_a_rejected_unit_stays_in_the_ledger(self, lock_dir):
        """Deploy sockets are not in the systemctl-helper allowlist, so their
        restart is refused — an unpaid debt, not a paid one."""
        _write_ledger(
            lock_dir, "fraisier-p-webhook.service", "fraisier-api-prod.socket"
        )

        def _call(_sock, _action, service):
            if service.endswith(".socket"):
                raise subprocess.CalledProcessError(
                    1, "restart", stderr="service not allowed"
                )

        with (
            patch("fraisier.deferred_restart.time.sleep"),
            patch(
                "fraisier.deferred_restart.wait_for_deploys_to_drain",
                return_value=MagicMock(drained=True, held=[]),
            ),
            patch("fraisier.drain_restart._call_via_socket", side_effect=_call),
        ):
            run_deferred_restarts("/run/x.sock", lock_dir=lock_dir)
        assert read_deferred_restarts(lock_dir) == ["fraisier-api-prod.socket"]

    def test_no_debt_is_a_no_op(self, lock_dir):
        with patch("fraisier.drain_restart._call_via_socket") as mock_socket:
            rc = run_deferred_restarts("/run/x.sock", lock_dir=lock_dir)
        assert rc == 0
        mock_socket.assert_not_called()

    def test_no_socket_configured_keeps_the_debt(self, lock_dir):
        _write_ledger(lock_dir, "fraisier-p-webhook.service")
        rc = run_deferred_restarts("", lock_dir=lock_dir)
        assert rc != 0
        assert read_deferred_restarts(lock_dir) == ["fraisier-p-webhook.service"]


class TestMaybeApplyDeferredRestarts:
    def test_spawns_a_detached_worker_when_a_debt_exists(self, lock_dir):
        _write_ledger(lock_dir, "fraisier-p-webhook.service")
        with patch("fraisier.deferred_restart.subprocess.Popen") as mock_popen:
            maybe_apply_deferred_restarts(lock_dir=lock_dir, socket_path="/run/x.sock")
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert "fraisier.deferred_restart" in cmd
        assert mock_popen.call_args.kwargs["start_new_session"] is True

    def test_does_nothing_when_there_is_no_debt(self, lock_dir):
        with patch("fraisier.deferred_restart.subprocess.Popen") as mock_popen:
            maybe_apply_deferred_restarts(lock_dir=lock_dir, socket_path="/run/x.sock")
        mock_popen.assert_not_called()

    def test_does_nothing_without_a_helper_socket(self, lock_dir, caplog):
        """No RPC channel means the debt cannot be paid here; leave it for doctor."""
        _write_ledger(lock_dir, "fraisier-p-webhook.service")
        with patch("fraisier.deferred_restart.subprocess.Popen") as mock_popen:
            maybe_apply_deferred_restarts(lock_dir=lock_dir, socket_path="")
        mock_popen.assert_not_called()
        assert read_deferred_restarts(lock_dir) == ["fraisier-p-webhook.service"]

    def test_never_raises(self, lock_dir):
        """A failure here must not turn a successful deploy into a failed one."""
        _write_ledger(lock_dir, "fraisier-p-webhook.service")
        with patch(
            "fraisier.deferred_restart.subprocess.Popen", side_effect=OSError("boom")
        ):
            maybe_apply_deferred_restarts(lock_dir=lock_dir, socket_path="/run/x.sock")
