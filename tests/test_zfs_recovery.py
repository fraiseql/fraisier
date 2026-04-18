"""Tests for ZFS error recovery and edge case handling."""

from unittest.mock import MagicMock, patch

import pytest

from fraisier.zfs.exceptions import (
    ZFSDatasetNotFoundError,
    ZFSOperationFailedError,
    ZFSPermissionDeniedError,
    ZFSPoolOfflineError,
)
from fraisier.zfs.operations import ZFSOperations


class TestZFSErrorRecovery:
    """Test ZFS error recovery and edge case handling."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = MagicMock()
        self.zfs_ops = ZFSOperations(self.runner)

    def test_dataset_not_found_recovery(self):
        """Test recovery from dataset not found errors."""
        # Simulate dataset not found during snapshot creation
        self.runner.run.side_effect = [
            Exception("cannot open 'zroot/missing': dataset does not exist")
        ]

        with pytest.raises(ZFSDatasetNotFoundError) as exc_info:
            self.zfs_ops.create_snapshot("zroot/missing", "test_snap")

        assert "dataset does not exist" in str(exc_info.value)
        assert "zroot/missing" in str(exc_info.value)

    def test_permission_denied_recovery(self):
        """Test recovery from permission denied errors."""
        # Simulate permission denied during clone creation
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # Snapshot succeeds
            Exception("cannot create clone: permission denied"),
        ]

        with pytest.raises(ZFSPermissionDeniedError) as exc_info:
            self.zfs_ops.create_snapshot_and_clone("zroot/data", "zroot/clone1")

        assert "permission denied" in str(exc_info.value)

    def test_pool_offline_recovery(self):
        """Test recovery from pool offline errors."""
        # Simulate pool offline during list operation
        self.runner.run.side_effect = [
            Exception("cannot open 'zroot': pool I/O is currently suspended")
        ]

        with pytest.raises(ZFSPoolOfflineError) as exc_info:
            self.zfs_ops.list_snapshots("zroot")

        assert "pool I/O is currently suspended" in str(exc_info.value)

    def test_out_of_space_recovery(self):
        """Test recovery from out of space errors."""
        # Simulate out of space during clone creation
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # Snapshot succeeds
            Exception("cannot create clone: out of space"),
        ]

        with pytest.raises(ZFSOperationFailedError) as exc_info:
            self.zfs_ops.create_snapshot_and_clone("zroot/data", "zroot/clone1")

        assert "out of space" in str(exc_info.value)

    def test_child_dataset_handling(self):
        """Test handling of child dataset operations."""
        # Test that operations work with child datasets
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Should work with child datasets
        result = self.zfs_ops.create_snapshot("zroot/pool/child", "test_snap")
        assert result == "zroot/pool/child@test_snap"

        # Should work with deeply nested paths
        result = self.zfs_ops.clone_snapshot(
            "zroot/pool/child@snap", "zroot/pool/child/clone"
        )
        assert result == "zroot/pool/child/clone"

    def test_partial_operation_recovery(self):
        """Test recovery from partial operations."""
        # Simulate a scenario where multiple operations are attempted
        # but some fail - should provide clear error messages
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # First operation succeeds
            Exception("cannot destroy snapshot: snapshot is busy"),  # Second fails
        ]

        # Test that cleanup continues even when some operations fail
        snapshots = [
            type(
                "MockSnapshot",
                (),
                {"name": "zroot/data@snap1", "creation_time": 1640995200},
            )(),
            type(
                "MockSnapshot",
                (),
                {"name": "zroot/data@snap2", "creation_time": 1641081600},
            )(),
        ]
        with patch.object(self.zfs_ops, "list_snapshots", return_value=snapshots):
            deleted = self.zfs_ops.cleanup_old_snapshots("zroot/data", keep_count=0)

        # Should have attempted both operations, but only one succeeded
        assert self.runner.run.call_count == 2
        assert len(deleted) == 1  # Only one snapshot was successfully deleted
        assert deleted == ["zroot/data@snap1"]

    def test_malformed_command_recovery(self):
        """Test recovery from malformed commands."""
        # Test that invalid dataset names are handled gracefully
        with pytest.raises(ZFSOperationFailedError):
            self.zfs_ops.create_snapshot("", "test_snap")  # Empty dataset

        with pytest.raises(ZFSOperationFailedError):
            self.zfs_ops.create_snapshot("zroot/data", "")  # Empty snapshot name

    def test_network_timeout_recovery(self):
        """Test recovery from network/timeout issues."""
        # Simulate timeout during operation
        self.runner.run.side_effect = Exception("timeout")

        with pytest.raises(ZFSOperationFailedError) as exc_info:
            self.zfs_ops.create_snapshot("zroot/data", "test_snap")

        assert "timeout" in str(exc_info.value)

    def test_zfs_command_not_found_recovery(self):
        """Test recovery when zfs command is not available."""
        # Simulate zfs command not found
        self.runner.run.side_effect = Exception("zfs: command not found")

        with pytest.raises(ZFSOperationFailedError) as exc_info:
            self.zfs_ops.list_snapshots("zroot")

        assert "command not found" in str(exc_info.value)

    def test_invalid_snapshot_reference_recovery(self):
        """Test recovery from invalid snapshot references."""
        # Test various invalid snapshot reference formats
        invalid_refs = [
            "zroot/data@",  # Missing snapshot name
            "@snapshot",  # Missing dataset
            "zroot/data@snap@extra",  # Too many @
            "zroot/data snapshot",  # Space instead of @
        ]

        for ref in invalid_refs:
            with pytest.raises(ZFSOperationFailedError):
                self.zfs_ops.clone_snapshot(ref, "zroot/clone")

    def test_concurrent_operation_conflicts(self):
        """Test handling of concurrent operation conflicts."""
        # Simulate race condition where snapshot is created between checks
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # First attempt succeeds
            Exception("cannot create snapshot: dataset already exists"),  # Retry fails
        ]

        # This should be handled by the retry logic in create_snapshot
        result = self.zfs_ops.create_snapshot("zroot/data", "test_snap")
        assert result == "zroot/data@test_snap"

    def test_dataset_state_changes_during_operation(self):
        """Test handling when dataset state changes during multi-step operations."""
        # Simulate dataset becoming unavailable during operation
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # Snapshot succeeds
            Exception("cannot create clone: dataset does not exist"),  # Dataset gone
        ]

        with pytest.raises(ZFSDatasetNotFoundError) as exc_info:
            self.zfs_ops.create_snapshot_and_clone("zroot/data", "zroot/clone1")

        assert "dataset does not exist" in str(exc_info.value)

    def test_zfs_version_compatibility(self):
        """Test handling of ZFS version compatibility issues."""
        # Simulate older ZFS version not supporting certain features
        self.runner.run.side_effect = Exception("invalid option 'property'")

        with pytest.raises(ZFSOperationFailedError) as exc_info:
            self.zfs_ops.clone_snapshot(
                "zroot/data@snap1", "zroot/clone1", properties={"readonly": "on"}
            )

        assert "invalid option" in str(exc_info.value)

    def test_large_dataset_operations(self):
        """Test handling of operations on very large datasets."""
        # This is more of a documentation test - the operations should work
        # the same regardless of dataset size, but error messages should be clear
        self.runner.run.side_effect = Exception("cannot create snapshot: out of space")

        with pytest.raises(ZFSOperationFailedError) as exc_info:
            self.zfs_ops.create_snapshot("zroot/large_dataset", "backup")

        assert "out of space" in str(exc_info.value)
