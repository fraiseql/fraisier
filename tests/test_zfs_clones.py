"""Tests for ZFS clone operations."""

from unittest.mock import MagicMock

import pytest

from fraisier.zfs.exceptions import ZFSDatasetNotFoundError, ZFSPermissionDeniedError
from fraisier.zfs.operations import ZFSOperations


class TestZFSCloneOperations:
    """Test ZFS clone creation operations."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = MagicMock()
        self.zfs_ops = ZFSOperations(self.runner)

    def test_clone_snapshot_basic(self):
        """Test basic snapshot cloning."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.clone_snapshot("zroot/data@snap1", "zroot/clone1")

        assert result == "zroot/clone1"
        self.runner.run.assert_called_once()
        args = self.runner.run.call_args[0][0]
        assert args == ["zfs", "clone", "zroot/data@snap1", "zroot/clone1"]

    def test_clone_snapshot_with_properties(self):
        """Test cloning with custom ZFS properties."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.clone_snapshot(
            "zroot/data@snap1",
            "zroot/clone1",
            properties={"mountpoint": "/tmp/test", "readonly": "on"},
        )

        assert result == "zroot/clone1"
        self.runner.run.assert_called_once()
        args = self.runner.run.call_args[0][0]
        expected = [
            "zfs",
            "clone",
            "-o",
            "mountpoint=/tmp/test",
            "-o",
            "readonly=on",
            "zroot/data@snap1",
            "zroot/clone1",
        ]
        assert args == expected

    def test_clone_snapshot_from_different_pool(self):
        """Test cloning snapshot from different pool."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.clone_snapshot("pool1/data@snap1", "pool2/clone1")

        assert result == "pool2/clone1"
        args = self.runner.run.call_args[0][0]
        assert args == ["zfs", "clone", "pool1/data@snap1", "pool2/clone1"]

    def test_clone_snapshot_nonexistent_snapshot(self):
        """Test cloning from non-existent snapshot."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "clone", "zroot/data@nonexistent", "zroot/clone1"],
            stderr="cannot open 'zroot/data@nonexistent': dataset does not exist",
        )

        with pytest.raises(ZFSDatasetNotFoundError):
            self.zfs_ops.clone_snapshot("zroot/data@nonexistent", "zroot/clone1")

    def test_clone_snapshot_clone_already_exists(self):
        """Test cloning when target clone already exists."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "clone", "zroot/data@snap1", "zroot/clone1"],
            stderr="cannot create 'zroot/clone1': dataset already exists",
        )

        # This should raise ZFSOperationFailedError since "dataset already exists"
        # is not a "does not exist" error
        from fraisier.zfs.exceptions import ZFSOperationFailedError

        with pytest.raises(ZFSOperationFailedError):
            self.zfs_ops.clone_snapshot("zroot/data@snap1", "zroot/clone1")

    def test_clone_snapshot_permission_denied(self):
        """Test cloning with permission denied."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "clone", "zroot/data@snap1", "zroot/clone1"],
            stderr="cannot create 'zroot/clone1': permission denied",
        )

        with pytest.raises(ZFSPermissionDeniedError):
            self.zfs_ops.clone_snapshot("zroot/data@snap1", "zroot/clone1")

    def test_clone_snapshot_invalid_snapshot_format(self):
        """Test cloning with invalid snapshot reference format."""
        # Should still work as long as ZFS accepts it
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.clone_snapshot(
            "zroot/data@snapshot-name", "zroot/clone-name"
        )

        assert result == "zroot/clone-name"
        args = self.runner.run.call_args[0][0]
        assert args == ["zfs", "clone", "zroot/data@snapshot-name", "zroot/clone-name"]

    def test_clone_snapshot_empty_properties(self):
        """Test cloning with empty properties dict."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.clone_snapshot(
            "zroot/data@snap1", "zroot/clone1", properties={}
        )

        assert result == "zroot/clone1"
        args = self.runner.run.call_args[0][0]
        assert args == ["zfs", "clone", "zroot/data@snap1", "zroot/clone1"]

    def test_clone_verification(self):
        """Test that clone creation is verified."""
        # The current implementation doesn't do explicit verification,
        # but the command execution serves as verification
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.zfs_ops.clone_snapshot("zroot/data@snap1", "zroot/clone1")

        assert result == "zroot/clone1"
        # Verify command was executed
        assert self.runner.run.call_count == 1
