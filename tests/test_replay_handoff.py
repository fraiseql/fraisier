"""The marker that tells a starting webhook its restart was an upgrade (#367).

`lifespan` is the natural place to replay a dispatch the upgrade refused: the
upgrade *ends* by restarting the webhook, so the new process comes up with the
ledger already on disk and is the first thing to run after the event that
caused the loss. It is also exactly where a replay is most dangerous — a
restart for any other reason would fire it too.

So the upgrade hands the replay over explicitly. The worker writes this marker
immediately before it requests the restart; the next start consumes it exactly
once. No marker, no replay.
"""

from __future__ import annotations

import json

from fraisier.replay_handoff import (
    REPLAY_HANDOFF_FILE,
    ReplayHandoff,
    consume_replay_handoff,
    record_replay_handoff,
)


class TestRoundTrip:
    def test_a_written_marker_is_read_back(self, tmp_path):
        record_replay_handoff(tmp_path, version="0.72.0", service="webhook.service")

        handoff = consume_replay_handoff(tmp_path)

        assert isinstance(handoff, ReplayHandoff)
        assert handoff.version == "0.72.0"
        assert handoff.service == "webhook.service"
        assert handoff.requested_at

    def test_it_lives_in_the_lock_dir_dot_prefixed(self, tmp_path):
        """Dot-prefixed for the reason its neighbours are: the lock counter
        globs ``*.lock`` there, and a stray match changes the answer to
        "is a deploy in flight"."""
        record_replay_handoff(tmp_path, version="0.72.0", service="w.service")

        assert (tmp_path / REPLAY_HANDOFF_FILE).exists()
        assert REPLAY_HANDOFF_FILE.startswith(".")
        assert not REPLAY_HANDOFF_FILE.endswith(".lock")


class TestConsumedExactlyOnce:
    def test_a_second_consume_finds_nothing(self, tmp_path):
        """Two starts, one replay. A marker that survived would re-deploy on
        every restart from then on."""
        record_replay_handoff(tmp_path, version="0.72.0", service="w.service")

        assert consume_replay_handoff(tmp_path) is not None
        assert consume_replay_handoff(tmp_path) is None

    def test_the_file_is_gone_after_consuming(self, tmp_path):
        record_replay_handoff(tmp_path, version="0.72.0", service="w.service")
        consume_replay_handoff(tmp_path)

        assert not (tmp_path / REPLAY_HANDOFF_FILE).exists()

    def test_it_is_consumed_even_when_unreadable(self, tmp_path):
        """A marker that cannot be parsed must still be removed, or every
        subsequent start retries the same unusable handoff."""
        (tmp_path / REPLAY_HANDOFF_FILE).write_text("{not json")

        assert consume_replay_handoff(tmp_path) is None
        assert not (tmp_path / REPLAY_HANDOFF_FILE).exists()


class TestNothingHereMayRaise:
    def test_absent_reads_as_no_handoff(self, tmp_path):
        assert consume_replay_handoff(tmp_path) is None

    def test_a_missing_lock_dir_reads_as_no_handoff(self, tmp_path):
        assert consume_replay_handoff(tmp_path / "nope") is None

    def test_a_wrong_shaped_payload_reads_as_no_handoff(self, tmp_path):
        (tmp_path / REPLAY_HANDOFF_FILE).write_text(json.dumps([1, 2, 3]))

        assert consume_replay_handoff(tmp_path) is None

    def test_recording_into_a_missing_dir_does_not_raise(self, tmp_path):
        """It runs in the upgrade worker, just before the restart it must not
        be able to prevent."""
        record_replay_handoff(tmp_path / "nope", version="0.72.0", service="w.service")

    def test_an_unknown_key_is_tolerated(self, tmp_path):
        """A self-upgrade puts two fraisier versions on one host by design, so
        a marker written by a newer one is normal."""
        (tmp_path / REPLAY_HANDOFF_FILE).write_text(
            json.dumps(
                {
                    "version": "0.99.0",
                    "service": "w.service",
                    "requested_at": "2026-09-06T00:00:00+00:00",
                    "something_new": True,
                }
            )
        )

        handoff = consume_replay_handoff(tmp_path)

        assert handoff is not None
        assert handoff.version == "0.99.0"


class TestTheUpgradeWorkerHandsOver:
    """The marker is written by the thing that causes the restart, and only by
    it. A restart requested for any other reason leaves none behind."""

    def test_requesting_a_restart_writes_the_handoff(self, tmp_path):
        from unittest.mock import patch

        from fraisier import webhook_self_upgrade

        with patch.object(webhook_self_upgrade, "_send_restart", return_value=0):
            _outcome, rc = webhook_self_upgrade._restart_outcome(
                "/run/x.sock",
                "fraisier-p-webhook.service",
                required="0.72.0",
                lock_dir=tmp_path,
            )

        assert rc == 0
        handoff = consume_replay_handoff(tmp_path)
        assert handoff is not None
        assert handoff.version == "0.72.0"
        assert handoff.service == "fraisier-p-webhook.service"

    def test_a_restart_that_was_not_requested_leaves_no_handoff(self, tmp_path):
        """The install-only path returns without asking for a restart, so
        nothing should be waiting for the next start to act on."""
        assert consume_replay_handoff(tmp_path) is None

    def test_the_handoff_is_written_before_the_restart_is_requested(self, tmp_path):
        """The restart can land before this process runs another line, so the
        marker must already be on disk when the RPC goes out."""
        from unittest.mock import patch

        from fraisier import webhook_self_upgrade

        seen: list[bool] = []

        def _record_then_check(_socket, _service):
            seen.append((tmp_path / REPLAY_HANDOFF_FILE).exists())
            return 0

        with patch.object(
            webhook_self_upgrade, "_send_restart", side_effect=_record_then_check
        ):
            webhook_self_upgrade._restart_outcome(
                "/run/x.sock", "w.service", required="0.72.0", lock_dir=tmp_path
            )

        assert seen == [True]

    def test_no_lock_dir_means_no_handoff_and_no_crash(self, tmp_path):
        from unittest.mock import patch

        from fraisier import webhook_self_upgrade

        with patch.object(webhook_self_upgrade, "_send_restart", return_value=0):
            _outcome, rc = webhook_self_upgrade._restart_outcome(
                "/run/x.sock", "w.service", required="0.72.0", lock_dir=None
            )

        assert rc == 0
