"""Tests for ZFS command wrapper functionality."""

from subprocess import CalledProcessError
from unittest.mock import MagicMock, patch

import pytest

from fraisier.zfs.exceptions import (
    ZFSDatasetNotFoundError,
    ZFSOperationFailedError,
    ZFSPermissionDeniedError,
    ZFSPoolOfflineError,
)
from fraisier.zfs.operations import ZFSCommand


class TestZFSCommand:
    """Test ZFS command execution wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = MagicMock()
        self.zfs_cmd = ZFSCommand(self.runner)

    def test_snapshot_command_building(self):
        """Test building zfs snapshot commands."""
        # Test basic snapshot command
        cmd = self.zfs_cmd._build_snapshot_cmd("zroot/data", "test_snap")
        assert cmd == ["zfs", "snapshot", "zroot/data@test_snap"]

        # Test recursive snapshot
        cmd = self.zfs_cmd._build_snapshot_cmd(
            "zroot/data", "test_snap", recursive=True
        )
        assert cmd == ["zfs", "snapshot", "-r", "zroot/data@test_snap"]

    def test_clone_command_building(self):
        """Test building zfs clone commands."""
        # Test basic clone command
        cmd = self.zfs_cmd._build_clone_cmd("zroot/data@snap", "zroot/clone")
        assert cmd == ["zfs", "clone", "zroot/data@snap", "zroot/clone"]

        # Test clone with properties
        cmd = self.zfs_cmd._build_clone_cmd(
            "zroot/data@snap", "zroot/clone", properties={"mountpoint": "/tmp/test"}
        )
        assert cmd == [
            "zfs",
            "clone",
            "-o",
            "mountpoint=/tmp/test",
            "zroot/data@snap",
            "zroot/clone",
        ]

    def test_destroy_command_building(self):
        """Test building zfs destroy commands."""
        # Test basic destroy command
        cmd = self.zfs_cmd._build_destroy_cmd("zroot/clone")
        assert cmd == ["zfs", "destroy", "zroot/clone"]

        # Test recursive destroy
        cmd = self.zfs_cmd._build_destroy_cmd("zroot/clone", recursive=True)
        assert cmd == ["zfs", "destroy", "-r", "zroot/clone"]

        # Test force destroy
        cmd = self.zfs_cmd._build_destroy_cmd("zroot/clone", force=True)
        assert cmd == ["zfs", "destroy", "-f", "zroot/clone"]

        # Test recursive and force
        cmd = self.zfs_cmd._build_destroy_cmd("zroot/clone", recursive=True, force=True)
        assert cmd == ["zfs", "destroy", "-r", "-f", "zroot/clone"]

    def test_list_command_building(self):
        """Test building zfs list commands."""
        # Test basic list command
        cmd = self.zfs_cmd._build_list_cmd("zroot/data")
        assert cmd == [
            "zfs",
            "list",
            "-H",
            "-o",
            "name,creation,used,referenced",
            "-t",
            "snapshot",
            "zroot/data",
        ]

    def test_get_command_building(self):
        """Test building zfs get commands."""
        # Test basic get command
        cmd = self.zfs_cmd._build_get_cmd("zroot/data@snap", ["type", "mountpoint"])
        assert cmd == [
            "zfs",
            "get",
            "-H",
            "-o",
            "value",
            "type,mountpoint",
            "zroot/data@snap",
        ]

    @patch("subprocess.CompletedProcess")
    def test_successful_command_execution(self, mock_process):
        """Test successful command execution."""
        mock_process.returncode = 0
        mock_process.stdout = "success output"
        mock_process.stderr = ""
        self.runner.run.return_value = mock_process

        result = self.zfs_cmd._run_command(["zfs", "list"])

        assert result == mock_process
        self.runner.run.assert_called_once_with(["zfs", "list"], check=True, timeout=30)

    def test_command_execution_with_timeout(self):
        """Test command execution with custom timeout."""
        with patch.object(self.zfs_cmd, "_run_command") as mock_run:
            self.zfs_cmd._run_command(["zfs", "snapshot", "test"], timeout=60)
            mock_run.assert_called_once_with(["zfs", "snapshot", "test"], timeout=60)

    def test_parse_list_output(self):
        """Test parsing zfs list output."""
        output = """zroot/data@snap1\t1640995200\t1.23G\t512M
zroot/data@snap2\t1641081600\t2.34G\t1.02G"""
        result = self.zfs_cmd._parse_list_output(output)
        expected = [
            {
                "name": "zroot/data@snap1",
                "creation": "1640995200",
                "used": "1.23G",
                "referenced": "512M",
            },
            {
                "name": "zroot/data@snap2",
                "creation": "1641081600",
                "used": "2.34G",
                "referenced": "1.02G",
            },
        ]
        assert result == expected

    def test_parse_get_output(self):
        """Test parsing zfs get output."""
        output = """filesystem
/tmp/test"""
        result = self.zfs_cmd._parse_get_output(output, ["type", "mountpoint"])
        expected = {"type": "filesystem", "mountpoint": "/tmp/test"}
        assert result == expected

    def test_dataset_not_found_error(self):
        """Test handling dataset not found errors."""
        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "list", "zroot/nonexistent"],
            stderr="cannot open 'zroot/nonexistent': dataset does not exist",
        )

        with pytest.raises(ZFSDatasetNotFoundError) as exc_info:
            self.zfs_cmd._run_command(["zfs", "list", "zroot/nonexistent"])

        assert "dataset does not exist" in str(exc_info.value)
        assert "zroot/nonexistent" in str(exc_info.value)

    def test_permission_denied_error(self):
        """Test handling permission denied errors."""
        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "snapshot", "zroot/data@test"],
            stderr="cannot open 'zroot/data': permission denied",
        )

        with pytest.raises(ZFSPermissionDeniedError) as exc_info:
            self.zfs_cmd._run_command(["zfs", "snapshot", "zroot/data@test"])

        assert "permission denied" in str(exc_info.value)
        assert "zroot/data" in str(exc_info.value)

    def test_pool_offline_error(self):
        """Test handling pool offline errors."""
        self.runner.run.side_effect = CalledProcessError(
            1,
            ["zfs", "list", "zroot"],
            stderr="cannot open 'zroot': pool I/O is currently suspended",
        )

        with pytest.raises(ZFSPoolOfflineError) as exc_info:
            self.zfs_cmd._run_command(["zfs", "list", "zroot"])

        assert "pool I/O is currently suspended" in str(exc_info.value)
        assert "zroot" in str(exc_info.value)

    def test_stderr_capture(self):
        """Test that stderr is captured and included in errors."""
        self.runner.run.side_effect = CalledProcessError(
            1, ["zfs", "invalid", "command"], stderr="detailed error message from zfs"
        )

        with pytest.raises(ZFSOperationFailedError) as exc_info:
            self.zfs_cmd._run_command(["zfs", "invalid", "command"])

        assert "detailed error message from zfs" in str(exc_info.value)
        assert "invalid operation failed" in str(exc_info.value)

    def test_empty_output_parsing(self):
        """Test parsing empty output."""
        result = self.zfs_cmd._parse_list_output("")
        assert result == []

        result = self.zfs_cmd._parse_get_output("", ["type"])
        assert result == {}
