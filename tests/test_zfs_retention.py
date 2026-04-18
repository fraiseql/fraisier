"""Tests for ZFS snapshot retention policy operations."""

from unittest.mock import MagicMock, patch

import pytest

from fraisier.zfs.dataclasses import Snapshot
from fraisier.zfs.operations import ZFSOperations


class TestZFSSnapshotRetention:
    """Test ZFS snapshot retention and cleanup operations."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = MagicMock()
        self.zfs_ops = ZFSOperations(self.runner)

    def test_cleanup_old_snapshots_keep_count_basic(self):
        """Test basic keep_count enforcement."""
        # Create mock snapshots with different creation times
        snapshots = [
            Snapshot("zroot/data@snap1", 1640995200, "1.0G", "512M"),  # Oldest
            Snapshot("zroot/data@snap2", 1641081600, "1.1G", "600M"),  # Middle
            Snapshot("zroot/data@snap3", 1641168000, "1.2G", "700M"),  # Newest
        ]

        # Mock list_snapshots to return our test snapshots
        with patch.object(self.zfs_ops, "list_snapshots", return_value=snapshots):
            # Mock destroy commands
            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            deleted = self.zfs_ops.cleanup_old_snapshots("zroot/data", keep_count=2)

            # Should keep 2 newest, delete 1 oldest
            assert len(deleted) == 1
            assert deleted == ["zroot/data@snap1"]

            # Should call destroy once
            assert self.runner.run.call_count == 1
            args = self.runner.run.call_args[0][0]
            assert args == ["zfs", "destroy", "zroot/data@snap1"]

    def test_cleanup_old_snapshots_keep_count_fewer_than_keep(self):
        """Test safety: don't delete when fewer snapshots than keep_count."""
        snapshots = [
            Snapshot("zroot/data@snap1", 1640995200, "1.0G", "512M"),
            Snapshot("zroot/data@snap2", 1641081600, "1.1G", "600M"),
        ]

        with patch.object(self.zfs_ops, "list_snapshots", return_value=snapshots):
            deleted = self.zfs_ops.cleanup_old_snapshots("zroot/data", keep_count=3)

            # Should not delete any (only 2 snapshots, want to keep 3)
            assert len(deleted) == 0
            self.runner.run.assert_not_called()

    def test_cleanup_old_snapshots_keep_count_equal(self):
        """Test when snapshot count equals keep_count."""
        snapshots = [
            Snapshot("zroot/data@snap1", 1640995200, "1.0G", "512M"),
            Snapshot("zroot/data@snap2", 1641081600, "1.1G", "600M"),
        ]

        with patch.object(self.zfs_ops, "list_snapshots", return_value=snapshots):
            deleted = self.zfs_ops.cleanup_old_snapshots("zroot/data", keep_count=2)

            # Should not delete any (exactly 2 snapshots, want to keep 2)
            assert len(deleted) == 0
            self.runner.run.assert_not_called()

    def test_cleanup_old_snapshots_fifo_deletion(self):
        """Test FIFO deletion - oldest first."""
        snapshots = [
            Snapshot("zroot/data@old1", 1640995200, "1.0G", "512M"),  # Oldest
            Snapshot("zroot/data@old2", 1641081600, "1.1G", "600M"),
            Snapshot("zroot/data@old3", 1641168000, "1.2G", "700M"),
            Snapshot("zroot/data@old4", 1641254400, "1.3G", "800M"),  # Newest
        ]

        with patch.object(self.zfs_ops, "list_snapshots", return_value=snapshots):
            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            deleted = self.zfs_ops.cleanup_old_snapshots("zroot/data", keep_count=2)

            # Should delete 2 oldest, keep 2 newest
            assert len(deleted) == 2
            assert deleted == ["zroot/data@old1", "zroot/data@old2"]

    def test_cleanup_old_snapshots_with_prefix(self):
        """Test prefix filtering for cleanup."""
        snapshots = [
            Snapshot("zroot/data@prod_001", 1640995200, "1.0G", "512M"),
            Snapshot("zroot/data@prod_002", 1641081600, "1.1G", "600M"),
            Snapshot(
                "zroot/data@backup_001", 1641168000, "1.2G", "700M"
            ),  # Different prefix
            Snapshot("zroot/data@prod_003", 1641254400, "1.3G", "800M"),
        ]

        with patch.object(self.zfs_ops, "list_snapshots") as mock_list:
            # Mock list_snapshots to return only prod_ prefixed snapshots
            mock_list.return_value = [
                s for s in snapshots if s.snapshot_name.startswith("prod_")
            ]
            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            deleted = self.zfs_ops.cleanup_old_snapshots(
                "zroot/data", keep_count=2, prefix="prod_"
            )

            # Should only consider prod_ snapshots for cleanup
            assert len(deleted) == 1  # Keep 2, delete 1 oldest prod_
            assert deleted == ["zroot/data@prod_001"]

            # Verify list_snapshots was called with prefix
            mock_list.assert_called_once_with("zroot/data", prefix="prod_")

    def test_cleanup_old_snapshots_empty_list(self):
        """Test cleanup when no snapshots exist."""
        with patch.object(self.zfs_ops, "list_snapshots", return_value=[]):
            deleted = self.zfs_ops.cleanup_old_snapshots("zroot/data", keep_count=5)

            assert deleted == []
            self.runner.run.assert_not_called()

    def test_cleanup_old_snapshots_keep_zero(self):
        """Test edge case: keep_count=0."""
        snapshots = [
            Snapshot("zroot/data@snap1", 1640995200, "1.0G", "512M"),
            Snapshot("zroot/data@snap2", 1641081600, "1.1G", "600M"),
        ]

        with patch.object(self.zfs_ops, "list_snapshots", return_value=snapshots):
            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            deleted = self.zfs_ops.cleanup_old_snapshots("zroot/data", keep_count=0)

            # Should delete all snapshots
            assert len(deleted) == 2
            assert set(deleted) == {"zroot/data@snap1", "zroot/data@snap2"}

    def test_cleanup_old_snapshots_destroy_failure(self):
        """Test handling of destroy command failure."""
        snapshots = [
            Snapshot("zroot/data@snap1", 1640995200, "1.0G", "512M"),
            Snapshot("zroot/data@snap2", 1641081600, "1.1G", "600M"),
            Snapshot("zroot/data@snap3", 1641168000, "1.2G", "700M"),
        ]

        with patch.object(self.zfs_ops, "list_snapshots", return_value=snapshots):
            # First destroy succeeds, second fails
            self.runner.run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),  # First destroy succeeds
                Exception("Destroy failed"),  # Second destroy fails
            ]

            # Should still return the snapshots that were successfully deleted
            deleted = self.zfs_ops.cleanup_old_snapshots("zroot/data", keep_count=1)

            # Should have attempted to delete 2 snapshots but only 1 succeeded
            assert len(deleted) == 1
            assert deleted == ["zroot/data@snap1"]

    def test_cleanup_old_snapshots_with_age_filter(self):
        """Test age-based cleanup in addition to count."""
        import time

        current_time = int(time.time())

        snapshots = [
            Snapshot(
                "zroot/data@old", current_time - 86400 * 10, "1.0G", "512M"
            ),  # 10 days old
            Snapshot(
                "zroot/data@medium", current_time - 86400 * 3, "1.1G", "600M"
            ),  # 3 days old
            Snapshot(
                "zroot/data@new", current_time - 86400 * 1, "1.2G", "700M"
            ),  # 1 day old
        ]

        with patch.object(self.zfs_ops, "list_snapshots", return_value=snapshots):
            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            # Delete snapshots older than 5 days, then apply count limit
            deleted = self.zfs_ops.cleanup_old_snapshots(
                "zroot/data", keep_count=1, max_age_days=5.0
            )

            # Should delete the 10-day-old snapshot due to age, and the 3-day-old due to count
            assert len(deleted) == 2
            assert set(deleted) == {"zroot/data@old", "zroot/data@medium"}

    def test_cleanup_old_snapshots_age_only(self):
        """Test age-based cleanup without count limit."""
        import time

        current_time = int(time.time())

        snapshots = [
            Snapshot(
                "zroot/data@very_old", current_time - 86400 * 10, "1.0G", "512M"
            ),  # 10 days old
            Snapshot(
                "zroot/data@old", current_time - 86400 * 6, "1.1G", "600M"
            ),  # 6 days old
            Snapshot(
                "zroot/data@new", current_time - 86400 * 1, "1.2G", "700M"
            ),  # 1 day old
        ]

        with patch.object(self.zfs_ops, "list_snapshots", return_value=snapshots):
            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            # Delete snapshots older than 5 days, no count limit
            deleted = self.zfs_ops.cleanup_old_snapshots("zroot/data", max_age_days=5.0)

            # Should delete snapshots older than 5 days
            assert len(deleted) == 2
            assert set(deleted) == {"zroot/data@very_old", "zroot/data@old"}

    def test_cleanup_old_snapshots_command_construction(self):
        """Test that destroy commands are constructed correctly."""
        snapshots = [
            Snapshot("zroot/data@snap1", 1640995200, "1.0G", "512M"),
        ]

        with patch.object(self.zfs_ops, "list_snapshots", return_value=snapshots):
            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            self.zfs_ops.cleanup_old_snapshots("zroot/data", keep_count=0)

            # Check destroy command construction
            args = self.runner.run.call_args[0][0]
            assert args == ["zfs", "destroy", "zroot/data@snap1"]
