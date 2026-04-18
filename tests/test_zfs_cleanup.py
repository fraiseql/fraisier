"""Tests for ZFS clone cleanup operations."""

from unittest.mock import MagicMock

import pytest

from fraisier.zfs.exceptions import ZFSDatasetNotFoundError, ZFSPermissionDeniedError
from fraisier.zfs.operations import ZFSOperations


class TestZFSCloneCleanup:
    """Test ZFS clone destruction operations."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = MagicMock()
        self.zfs_ops = ZFSOperations(self.runner)

    def test_destroy_clone_basic(self):
        """Test basic clone destruction."""
        # Mock successful destroy command
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Mock get command to verify it's a clone
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="clone\n", stderr=""),  # get type
            MagicMock(returncode=0, stdout="", stderr=""),  # destroy
        ]

        self.zfs_ops.destroy_clone("zroot/clone1")

        # Should call get first to check type, then destroy
        assert self.runner.run.call_count == 2

    def test_destroy_clone_recursive(self):
        """Test recursive clone destruction."""
        # Mock successful destroy command
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Mock get command to verify it's a clone
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="clone\n", stderr=""),  # get type
            MagicMock(returncode=0, stdout="", stderr=""),  # destroy
        ]

        self.zfs_ops.destroy_clone("zroot/clone1", recursive=True)

        # Check that destroy was called with -r
        destroy_call = self.runner.run.call_args_list[1]
        args = destroy_call[0][0]
        assert "-r" in args

    def test_destroy_clone_force(self):
        """Test force clone destruction."""
        # Mock successful destroy command
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Mock get command to verify it's a clone
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="clone\n", stderr=""),  # get type
            MagicMock(returncode=0, stdout="", stderr=""),  # destroy
        ]

        self.zfs_ops.destroy_clone("zroot/clone1", force=True)

        # Check that destroy was called with -f
        destroy_call = self.runner.run.call_args_list[1]
        args = destroy_call[0][0]
        assert "-f" in args

    def test_destroy_clone_recursive_and_force(self):
        """Test recursive and force clone destruction."""
        # Mock successful destroy command
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Mock get command to verify it's a clone
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="clone\n", stderr=""),  # get type
            MagicMock(returncode=0, stdout="", stderr=""),  # destroy
        ]

        self.zfs_ops.destroy_clone("zroot/clone1", recursive=True, force=True)

        # Check that destroy was called with both -r and -f
        destroy_call = self.runner.run.call_args_list[1]
        args = destroy_call[0][0]
        assert "-r" in args
        assert "-f" in args

    def test_destroy_clone_safety_check_non_clone(self):
        """Test refusing to destroy non-clone datasets."""
        # Mock get command showing it's a filesystem, not a clone
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="filesystem\n", stderr=""),  # get type
        ]

        from fraisier.zfs.exceptions import ZFSOperationFailedError

        with pytest.raises(ZFSOperationFailedError, match="not a clone"):
            self.zfs_ops.destroy_clone("zroot/filesystem")

    def test_destroy_clone_dataset_not_found(self):
        """Test destroying non-existent clone."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "get", "-H", "-o", "value", "type", "zroot/nonexistent"],
            stderr="cannot open 'zroot/nonexistent': dataset does not exist",
        )

        with pytest.raises(ZFSDatasetNotFoundError):
            self.zfs_ops.destroy_clone("zroot/nonexistent")

    def test_destroy_clone_permission_denied_on_get(self):
        """Test permission denied when checking clone type."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "get", "-H", "-o", "value", "type", "zroot/clone1"],
            stderr="cannot open 'zroot/clone1': permission denied",
        )

        with pytest.raises(ZFSPermissionDeniedError):
            self.zfs_ops.destroy_clone("zroot/clone1")

    def test_destroy_clone_permission_denied_on_destroy(self):
        """Test permission denied when destroying clone."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="clone\n", stderr=""),  # get type succeeds
            CalledProcessError(
                1,
                ["zfs", "destroy", "zroot/clone1"],
                stderr="cannot destroy 'zroot/clone1': permission denied",
            ),  # destroy fails
        ]

        with pytest.raises(ZFSPermissionDeniedError):
            self.zfs_ops.destroy_clone("zroot/clone1")

    def test_destroy_clone_in_use(self):
        """Test destroying clone that is in use."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="clone\n", stderr=""),  # get type succeeds
            CalledProcessError(
                1,
                ["zfs", "destroy", "zroot/clone1"],
                stderr="cannot destroy 'zroot/clone1': dataset is busy",
            ),  # destroy fails
        ]

        from fraisier.zfs.exceptions import ZFSOperationFailedError

        with pytest.raises(ZFSOperationFailedError, match="dataset is busy"):
            self.zfs_ops.destroy_clone("zroot/clone1")

    def test_destroy_clone_verification_failure(self):
        """Test handling of destroy command failure."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="clone\n", stderr=""),  # get type succeeds
            CalledProcessError(
                1,
                ["zfs", "destroy", "zroot/clone1"],
                stderr="cannot destroy 'zroot/clone1': unknown error",
            ),  # destroy fails
        ]

        from fraisier.zfs.exceptions import ZFSOperationFailedError

        with pytest.raises(ZFSOperationFailedError):
            self.zfs_ops.destroy_clone("zroot/clone1")

    def test_destroy_clone_command_construction(self):
        """Test that destroy commands are constructed correctly."""
        self.runner.run.side_effect = [
            MagicMock(returncode=0, stdout="clone\n", stderr=""),  # get type
            MagicMock(returncode=0, stdout="", stderr=""),  # destroy
        ]

        self.zfs_ops.destroy_clone("zroot/clone1", recursive=True, force=True)

        # Check get command
        get_call = self.runner.run.call_args_list[0]
        get_args = get_call[0][0]
        assert get_args == ["zfs", "get", "-H", "-o", "value", "type", "zroot/clone1"]

        # Check destroy command
        destroy_call = self.runner.run.call_args_list[1]
        destroy_args = destroy_call[0][0]
        assert destroy_args == ["zfs", "destroy", "-r", "-f", "zroot/clone1"]
