"""Tests for ZFS snapshot operations."""

from unittest.mock import MagicMock

import pytest

from fraisier.zfs.exceptions import (
    ZFSDatasetNotFoundError,
    ZFSOperationFailedError,
    ZFSPermissionDeniedError,
)
from fraisier.zfs.operations import ZFSOperations


class TestZFSSnapshotOperations:
    """Test ZFS snapshot creation operations."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = MagicMock()
        self.zfs_ops = ZFSOperations(self.runner)

    def test_create_snapshot_basic(self):
        """Test basic snapshot creation."""
        # Mock successful command execution
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.create_snapshot("zroot/data", "test_snap")

        assert result == "zroot/data@test_snap"
        self.runner.run.assert_called_once()
        args = self.runner.run.call_args[0][0]
        assert args == ["zfs", "snapshot", "zroot/data@test_snap"]

    def test_create_snapshot_auto_name(self):
        """Test snapshot creation with auto-generated name."""
        import time
        from unittest.mock import patch

        with patch("time.strftime") as mock_strftime:
            mock_strftime.return_value = "20220101_120000"
            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = self.zfs_ops.create_snapshot("zroot/data")

            assert result == "zroot/data@snap_20220101_120000"
            args = self.runner.run.call_args[0][0]
            assert args == ["zfs", "snapshot", "zroot/data@snap_20220101_120000"]

    def test_create_snapshot_recursive(self):
        """Test recursive snapshot creation."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.create_snapshot("zroot/data", "test_snap", recursive=True)

        assert result == "zroot/data@test_snap"
        self.runner.run.assert_called_once()
        args = self.runner.run.call_args[0][0]
        assert args == ["zfs", "snapshot", "-r", "zroot/data@test_snap"]

    def test_create_snapshot_dataset_not_found(self):
        """Test snapshot creation with non-existent dataset."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "snapshot", "zroot/nonexistent@test"],
            stderr="cannot open 'zroot/nonexistent': dataset does not exist",
        )

        with pytest.raises(ZFSDatasetNotFoundError):
            self.zfs_ops.create_snapshot("zroot/nonexistent", "test")

    def test_create_snapshot_permission_denied(self):
        """Test snapshot creation with permission denied."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "snapshot", "zroot/data@test"],
            stderr="cannot open 'zroot/data': permission denied",
        )

        with pytest.raises(ZFSPermissionDeniedError):
            self.zfs_ops.create_snapshot("zroot/data", "test")

    def test_create_snapshot_with_timestamp(self):
        """Test snapshot creation with timestamp in name."""
        from unittest.mock import patch

        # Mock time.time() to return a fixed timestamp
        with patch("time.time", return_value=1640995200.0):
            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = self.zfs_ops.create_snapshot(
                "zroot/data", "backup_20220101_120000"
            )

            assert result == "zroot/data@backup_20220101_120000"
            self.runner.run.assert_called_once()
            args = self.runner.run.call_args[0][0]
            assert args == ["zfs", "snapshot", "zroot/data@backup_20220101_120000"]

    def test_create_snapshot_with_custom_prefix(self):
        """Test snapshot creation with custom prefix."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.create_snapshot("zroot/data", "prod_backup_001")

        assert result == "zroot/data@prod_backup_001"
        self.runner.run.assert_called_once()
        args = self.runner.run.call_args[0][0]
        assert args == ["zfs", "snapshot", "zroot/data@prod_backup_001"]

    def test_snapshot_name_generation(self):
        """Test snapshot name generation patterns."""
        import time
        from unittest.mock import patch

        # Test timestamp-based naming
        with patch("time.time", return_value=1640995200.0):
            # This would be in a higher-level function, but testing the concept
            timestamp = int(time.time())
            snapshot_name = f"backup_{timestamp}"

            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = self.zfs_ops.create_snapshot("zroot/data", snapshot_name)

            assert result == f"zroot/data@{snapshot_name}"
            args = self.runner.run.call_args[0][0]
            assert args == ["zfs", "snapshot", f"zroot/data@{snapshot_name}"]

    def test_snapshot_consistency_check(self):
        """Test that snapshot creation handles race conditions."""
        # This would typically involve checking dataset state before snapshot
        # For now, just test that the command is called correctly
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.create_snapshot("zroot/data", "test_snap")

        assert result == "zroot/data@test_snap"
        # Verify command was called exactly once
        assert self.runner.run.call_count == 1

    def test_create_snapshot_retry_on_exists(self):
        """Test retry logic when snapshot already exists."""
        from subprocess import CalledProcessError
        from unittest.mock import patch

        # First call fails with "already exists", second succeeds
        self.runner.run.side_effect = [
            CalledProcessError(
                1,
                ["zfs", "snapshot", "zroot/data@snap_20220101_120000"],
                stderr="cannot create snapshot 'zroot/data@snap_20220101_120000': dataset already exists",
            ),
            MagicMock(returncode=0, stdout="", stderr=""),  # Second call succeeds
        ]

        self.zfs_ops._generate_snapshot_name = MagicMock(
            side_effect=["snap_20220101_120000", "snap_20220101_120001"]
        )

        with patch("fraisier.zfs.operations.time.sleep") as mock_sleep:
            result = self.zfs_ops.create_snapshot("zroot/data")

            assert result == "zroot/data@snap_20220101_120001"
            assert self.runner.run.call_count == 2
            mock_sleep.assert_called_once_with(0.1)  # First retry delay

    def test_create_snapshot_max_retries_exceeded(self):
        """Test that max retries are respected."""
        from subprocess import CalledProcessError
        from unittest.mock import patch

        # Always fail with "already exists"
        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "snapshot", "zroot/data@snap_20220101_120000"],
            stderr="cannot create snapshot 'zroot/data@snap_20220101_120000': dataset already exists",
        )

        self.zfs_ops._generate_snapshot_name = MagicMock(
            return_value="snap_20220101_120000"
        )

        with patch("fraisier.zfs.operations.time.sleep"):
            with pytest.raises(ZFSOperationFailedError):
                self.zfs_ops.create_snapshot("zroot/data")

            # Should have tried 3 times (max_retries)
            assert self.runner.run.call_count == 3
