"""Tests for file-based deployment locking (fcntl.flock)."""

import multiprocessing
from pathlib import Path
from unittest.mock import patch

import pytest

from fraisier.errors import DeploymentLockError
from fraisier.locking import file_deployment_lock, is_deployment_locked


def _hold_lock(lock_dir: Path, ready_event, release_event) -> None:
    """Hold a file lock until release_event is set. Must be module-level for spawn."""
    with file_deployment_lock("myfraise", lock_dir=lock_dir):
        ready_event.set()
        release_event.wait(timeout=5)


class TestFileDeploymentLock:
    """Test the file-based flock mutex."""

    def test_acquire_succeeds_when_unlocked(self, tmp_path):
        """Lock acquisition succeeds when no other process holds it."""
        with file_deployment_lock("myfraise", lock_dir=tmp_path):
            lock_file = tmp_path / "myfraise.lock"
            assert lock_file.exists()

    def test_raises_when_already_held(self, tmp_path):
        """Acquiring the lock raises DeploymentLockError when already held."""
        with file_deployment_lock("myfraise", lock_dir=tmp_path):  # noqa: SIM117
            with pytest.raises(DeploymentLockError, match="myfraise"):
                with file_deployment_lock("myfraise", lock_dir=tmp_path):
                    pass  # Should not reach here

    def test_lock_released_after_context_exit(self, tmp_path):
        """Lock is released after context manager exits normally."""
        with file_deployment_lock("myfraise", lock_dir=tmp_path):
            pass

        # Should be able to acquire again
        with file_deployment_lock("myfraise", lock_dir=tmp_path):
            pass

    def test_lock_released_on_exception(self, tmp_path):
        """Lock is released even when an exception occurs inside the block."""
        with (
            pytest.raises(ValueError, match="boom"),
            file_deployment_lock("myfraise", lock_dir=tmp_path),
        ):
            raise ValueError("boom")

        # Should be able to acquire again after exception
        with file_deployment_lock("myfraise", lock_dir=tmp_path):
            pass

    def test_different_fraises_dont_conflict(self, tmp_path):
        """Locks for different fraises are independent."""
        with (
            file_deployment_lock("fraise_a", lock_dir=tmp_path),
            file_deployment_lock("fraise_b", lock_dir=tmp_path),
        ):
            pass

    def test_lock_file_created_in_lock_dir(self, tmp_path):
        """Lock file is created with correct name in lock_dir."""
        with file_deployment_lock("myfraise", lock_dir=tmp_path):
            assert (tmp_path / "myfraise.lock").exists()

    def test_is_deployment_locked_returns_false_when_unlocked(self, tmp_path):
        """is_deployment_locked returns False when no lock is held."""
        assert is_deployment_locked("myfraise", lock_dir=tmp_path) is False

    def test_is_deployment_locked_returns_false_no_lock_file(self, tmp_path):
        """is_deployment_locked returns False when lock file does not exist."""
        assert is_deployment_locked("nonexistent", lock_dir=tmp_path) is False

    def test_is_deployment_locked_returns_true_when_held(self, tmp_path):
        """is_deployment_locked returns True when another context holds the lock."""
        with file_deployment_lock("myfraise", lock_dir=tmp_path):
            assert is_deployment_locked("myfraise", lock_dir=tmp_path) is True

    def test_is_deployment_locked_returns_false_after_release(self, tmp_path):
        """is_deployment_locked returns False after the lock is released."""
        with file_deployment_lock("myfraise", lock_dir=tmp_path):
            pass
        assert is_deployment_locked("myfraise", lock_dir=tmp_path) is False

    def test_lock_file_closed_on_flock_oserror(self, tmp_path):
        """File handle is closed when flock raises OSError (not BlockingIOError)."""
        from unittest.mock import MagicMock

        mock_file = MagicMock()
        mock_file.fileno.return_value = 999

        with (
            patch("fraisier.locking.fcntl.flock", side_effect=OSError("I/O error")),
            patch.object(type(tmp_path / "x"), "open", return_value=mock_file),
            pytest.raises(OSError, match="I/O error"),
            file_deployment_lock("myfraise", lock_dir=tmp_path),
        ):
            pass

        mock_file.close.assert_called()

    def test_cross_process_lock_contention(self, tmp_path):
        """A second process cannot acquire a lock held by the first."""
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        proc = ctx.Process(target=_hold_lock, args=(tmp_path, ready, release))
        proc.start()

        try:
            ready.wait(timeout=5)
            # Process holds the lock — we should fail to acquire
            with (
                pytest.raises(DeploymentLockError),
                file_deployment_lock("myfraise", lock_dir=tmp_path),
            ):
                pass
        finally:
            release.set()
            proc.join(timeout=5)


class TestTheLockDirIsAbsolute:
    """A relative lock_dir was never meaningful, and once cost a tracked file.

    ``MagicMock/get_config().deployment.lock_dir/140298202465680/my_api.lock``
    was committed in ``1152f45``: a test called ``deployment_lock("my_api")``
    under a mocked ``get_config``, ``file_deployment_lock`` ran
    ``mkdir(parents=True)`` and ``open("w")`` relative to whatever the working
    directory happened to be, and the repository grew a directory tree named
    after a ``MagicMock.__fspath__``. The file was removed in #376; this is the
    guard that stops it coming back.

    The deploy user's cwd on a server is its home directory, so a relative
    ``deployment.lock_dir`` would put the lock somewhere no other process
    looks — which is worse than refusing it.
    """

    def test_acquiring_with_a_relative_dir_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with (
            pytest.raises(ValueError, match="absolute"),
            file_deployment_lock("my_api", lock_dir=Path("relative/locks")),
        ):
            pass
        assert not (tmp_path / "relative").exists(), "a directory was still created"

    def test_checking_with_a_relative_dir_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="absolute"):
            is_deployment_locked("my_api", lock_dir=Path("relative/locks"))

    def test_the_message_names_the_setting(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError) as excinfo:
            is_deployment_locked("my_api", lock_dir=Path("relative/locks"))
        assert "deployment.lock_dir" in str(excinfo.value)

    def test_a_mock_shaped_path_is_refused(self, tmp_path, monkeypatch):
        """The exact shape that got committed: a MagicMock's ``__fspath__``."""
        from unittest.mock import MagicMock

        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="absolute"):
            is_deployment_locked(
                "my_api", lock_dir=Path(str(MagicMock().deployment.lock_dir))
            )
        assert list(tmp_path.iterdir()) == []

    def test_an_absolute_dir_is_accepted(self, tmp_path):
        with file_deployment_lock("my_api", lock_dir=tmp_path):
            pass
        assert (tmp_path / "my_api.lock").exists()


class TestTheWebhookSurvivesAnUnusableLockDir:
    """A misconfigured `deployment.lock_dir` must not take the webhook down.

    The guard belongs at the lock helpers, where the damage is done; the
    webhook's concurrency and drain checks degrade to "cannot tell" and say so,
    and the deploy fails when it goes to take the lock — the moment the answer
    matters.
    """

    def test_a_relative_configured_dir_reads_as_unresolvable(self, caplog):
        import logging
        from unittest.mock import MagicMock

        from fraisier.webhook import _get_lock_dir

        config = MagicMock()
        config.deployment.lock_dir = "relative/locks"

        with caplog.at_level(logging.ERROR):
            assert _get_lock_dir(config) is None
        assert any("absolute" in r.getMessage() for r in caplog.records)

    def test_an_absolute_configured_dir_is_returned(self):
        from unittest.mock import MagicMock

        from fraisier.webhook import _get_lock_dir

        config = MagicMock()
        config.deployment.lock_dir = "/run/fraisier"
        assert _get_lock_dir(config) == Path("/run/fraisier")
