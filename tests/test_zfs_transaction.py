"""Tests for ZFS transaction-like semantics operations."""

from unittest.mock import MagicMock, call, patch

import pytest

from fraisier.zfs.exceptions import ZFSDatasetNotFoundError, ZFSPermissionDeniedError
from fraisier.zfs.operations import ZFSOperations


class TestZFSTransactionSemantics:
    """Test ZFS atomic operations and context managers."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = MagicMock()
        self.zfs_ops = ZFSOperations(self.runner)

    def test_create_snapshot_and_clone_success(self):
        """Test successful atomic snapshot+clone operation."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.create_snapshot_and_clone(
            "zroot/data", "zroot/clone1", snapshot_name="test_snap"
        )

        assert result == ("zroot/data@test_snap", "zroot/clone1")

        # Should call snapshot creation first, then clone
        assert self.runner.run.call_count == 2
        calls = self.runner.run.call_args_list

        # First call: snapshot
        assert calls[0][0][0] == ["zfs", "snapshot", "zroot/data@test_snap"]
        # Second call: clone
        assert calls[1][0][0] == [
            "zfs",
            "clone",
            "zroot/data@test_snap",
            "zroot/clone1",
        ]

    def test_create_snapshot_and_clone_snapshot_failure(self):
        """Test snapshot failure prevents clone attempt."""
        from subprocess import CalledProcessError

        # Snapshot fails
        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "snapshot", "zroot/data@test_snap"],
            stderr="cannot create snapshot: dataset busy",
        )

        from fraisier.zfs.exceptions import ZFSOperationFailedError

        with pytest.raises(ZFSOperationFailedError):  # Should raise the snapshot error
            self.zfs_ops.create_snapshot_and_clone(
                "zroot/data", "zroot/clone1", snapshot_name="test_snap"
            )

        # Should only attempt snapshot, not clone
        assert self.runner.run.call_count == 1
        args = self.runner.run.call_args[0][0]
        assert args == ["zfs", "snapshot", "zroot/data@test_snap"]

    def test_create_snapshot_and_clone_clone_failure(self):
        """Test clone failure leaves snapshot intact."""
        from subprocess import CalledProcessError

        # Snapshot succeeds, clone fails
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # Snapshot succeeds
            CalledProcessError(
                1,
                ["zfs", "clone", "zroot/data@test_snap", "zroot/clone1"],
                stderr="cannot create clone: dataset exists",
            ),  # Clone fails
        ]

        from fraisier.zfs.exceptions import ZFSOperationFailedError

        with pytest.raises(ZFSOperationFailedError):  # Should raise the clone error
            self.zfs_ops.create_snapshot_and_clone(
                "zroot/data", "zroot/clone1", snapshot_name="test_snap"
            )

        # Should attempt both operations
        assert self.runner.run.call_count == 2

    def test_create_snapshot_and_clone_with_properties(self):
        """Test atomic operation with clone properties."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.create_snapshot_and_clone(
            "zroot/data",
            "zroot/clone1",
            snapshot_name="test_snap",
            clone_properties={"readonly": "on", "mountpoint": "/tmp/test"},
        )

        assert result == ("zroot/data@test_snap", "zroot/clone1")

        # Check clone command includes properties
        clone_call = self.runner.run.call_args_list[1]
        args = clone_call[0][0]
        expected = [
            "zfs",
            "clone",
            "-o",
            "readonly=on",
            "-o",
            "mountpoint=/tmp/test",
            "zroot/data@test_snap",
            "zroot/clone1",
        ]
        assert args == expected

    def test_create_snapshot_and_clone_auto_snapshot_name(self):
        """Test atomic operation with auto-generated snapshot name."""
        import time

        with patch("time.strftime", return_value="20220101_120000"):
            self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = self.zfs_ops.create_snapshot_and_clone(
                "zroot/data", "zroot/clone1"
            )

            assert result == ("zroot/data@snap_20220101_120000", "zroot/clone1")

    def test_temporary_clone_context_manager(self):
        """Test temporary clone context manager."""
        # Mock clone creation and type checking for destroy_clone
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # Clone creation
            MagicMock(
                returncode=0, stdout="clone\n", stderr=""
            ),  # Type check for destroy
            MagicMock(returncode=0, stdout="", stderr=""),  # Destroy
        ]

        with self.zfs_ops.temporary_clone(
            "zroot/data@snap1", "zroot/temp_clone"
        ) as clone_path:
            assert clone_path == "zroot/temp_clone"
            # Clone should be created
            assert self.runner.run.call_count == 1

        # Clone should be destroyed on exit
        assert self.runner.run.call_count == 3
        destroy_call = self.runner.run.call_args_list[2]
        args = destroy_call[0][0]
        assert args == ["zfs", "destroy", "zroot/temp_clone"]

    def test_temporary_clone_context_manager_exception_cleanup(self):
        """Test temporary clone cleanup on exception."""
        # Mock clone creation and type checking for destroy_clone
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # Clone creation
            MagicMock(
                returncode=0, stdout="clone\n", stderr=""
            ),  # Type check for destroy
            MagicMock(returncode=0, stdout="", stderr=""),  # Destroy
        ]

        with (
            pytest.raises(ValueError),
            self.zfs_ops.temporary_clone(
                "zroot/data@snap1", "zroot/temp_clone"
            ) as clone_path,
        ):
            assert clone_path == "zroot/temp_clone"
            raise ValueError("Test exception")

        # Clone should still be destroyed even on exception
        assert self.runner.run.call_count == 3
        destroy_call = self.runner.run.call_args_list[2]
        args = destroy_call[0][0]
        assert args == ["zfs", "destroy", "zroot/temp_clone"]

    def test_temporary_clone_context_manager_with_properties(self):
        """Test temporary clone with custom properties."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with self.zfs_ops.temporary_clone(
            "zroot/data@snap1", "zroot/temp_clone", properties={"readonly": "on"}
        ) as clone_path:
            assert clone_path == "zroot/temp_clone"

        # Check clone creation included properties
        clone_call = self.runner.run.call_args_list[0]
        args = clone_call[0][0]
        expected = [
            "zfs",
            "clone",
            "-o",
            "readonly=on",
            "zroot/data@snap1",
            "zroot/temp_clone",
        ]
        assert args == expected

    def test_temporary_clone_context_manager_clone_failure(self):
        """Test temporary clone context manager when clone creation fails."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "clone", "zroot/data@snap1", "zroot/temp_clone"],
            stderr="cannot create clone: permission denied",
        )

        with (
            pytest.raises(ZFSPermissionDeniedError),
            self.zfs_ops.temporary_clone("zroot/data@snap1", "zroot/temp_clone"),
        ):
            pass  # Should not reach here

        # Should only attempt clone creation, not destruction
        assert self.runner.run.call_count == 1

    def test_create_snapshot_and_clone_rollback_on_clone_failure(self):
        """Test that failed clone operations leave snapshot for manual cleanup."""
        from subprocess import CalledProcessError

        # Setup: snapshot succeeds, clone fails
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # Snapshot succeeds
            CalledProcessError(
                1,
                ["zfs", "clone", "zroot/data@test_snap", "zroot/clone1"],
                stderr="cannot create clone: out of space",
            ),  # Clone fails
        ]

        from fraisier.zfs.exceptions import ZFSOperationFailedError

        with pytest.raises(ZFSOperationFailedError):
            self.zfs_ops.create_snapshot_and_clone(
                "zroot/data", "zroot/clone1", snapshot_name="test_snap"
            )

        # Snapshot should remain (not cleaned up automatically)
        # In real usage, the snapshot would be left for manual cleanup or retention policy
        assert self.runner.run.call_count == 2
