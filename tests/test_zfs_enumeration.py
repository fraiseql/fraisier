"""Tests for ZFS snapshot enumeration operations."""

from unittest.mock import MagicMock

import pytest

from fraisier.zfs.exceptions import ZFSDatasetNotFoundError
from fraisier.zfs.operations import ZFSOperations


class TestZFSSnapshotEnumeration:
    """Test ZFS snapshot listing and enumeration operations."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = MagicMock()
        self.zfs_ops = ZFSOperations(self.runner)

    def test_list_snapshots_basic(self):
        """Test basic snapshot listing."""
        mock_output = (
            "zroot/data@snap1\t1640995200\t1.23G\t512M\n"
            "zroot/data@snap2\t1641081600\t2.34G\t1.02G\n"
            "zroot/data@snap3\t1641168000\t3.45G\t2.01G\n"
        )
        self.runner.run.return_value = MagicMock(
            returncode=0, stdout=mock_output, stderr=""
        )

        snapshots = self.zfs_ops.list_snapshots("zroot/data")

        assert len(snapshots) == 3
        assert snapshots[0].name == "zroot/data@snap1"
        assert snapshots[1].name == "zroot/data@snap2"
        assert snapshots[2].name == "zroot/data@snap3"

    def test_list_snapshots_with_prefix_filter(self):
        """Test snapshot listing with prefix filtering."""
        mock_output = (
            "zroot/data@prod_001\t1640995200\t1.23G\t512M\n"
            "zroot/data@prod_002\t1641081600\t2.34G\t1.02G\n"
            "zroot/data@backup_001\t1641168000\t3.45G\t2.01G\n"
        )
        self.runner.run.return_value = MagicMock(
            returncode=0, stdout=mock_output, stderr=""
        )

        snapshots = self.zfs_ops.list_snapshots("zroot/data", prefix="prod_")

        assert len(snapshots) == 2
        assert snapshots[0].name == "zroot/data@prod_001"
        assert snapshots[1].name == "zroot/data@prod_002"

    def test_list_snapshots_empty_result(self):
        """Test snapshot listing when no snapshots exist."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        snapshots = self.zfs_ops.list_snapshots("zroot/data")

        assert snapshots == []

    def test_list_snapshots_dataset_not_found(self):
        """Test snapshot listing for non-existent dataset."""
        from subprocess import CalledProcessError

        self.runner.run.side_effect = CalledProcessError(
            1,
            [
                "zfs",
                "list",
                "-H",
                "-o",
                "name,creation,used,referenced",
                "-t",
                "snapshot",
                "zroot/nonexistent",
            ],
            stderr="cannot open 'zroot/nonexistent': dataset does not exist",
        )

        with pytest.raises(ZFSDatasetNotFoundError):
            self.zfs_ops.list_snapshots("zroot/nonexistent")

    def test_list_snapshots_sorting_by_creation_time(self):
        """Test that snapshots are sorted by creation time."""
        # Note: zfs list already returns snapshots sorted by creation time
        mock_output = (
            "zroot/data@oldest\t1640995200\t1.23G\t512M\n"
            "zroot/data@newer\t1641081600\t2.34G\t1.02G\n"
            "zroot/data@newest\t1641168000\t3.45G\t2.01G\n"
        )
        self.runner.run.return_value = MagicMock(
            returncode=0, stdout=mock_output, stderr=""
        )

        snapshots = self.zfs_ops.list_snapshots("zroot/data")

        assert len(snapshots) == 3
        assert (
            snapshots[0].creation_time
            <= snapshots[1].creation_time
            <= snapshots[2].creation_time
        )

    def test_list_snapshots_parsing_sizes(self):
        """Test parsing of snapshot sizes."""
        mock_output = (
            "zroot/data@snap1\t1640995200\t1.23G\t512M\n"
            "zroot/data@snap2\t1641081600\t0\t0\n"
            "zroot/data@snap3\t1641168000\t500K\t200K\n"
        )
        self.runner.run.return_value = MagicMock(
            returncode=0, stdout=mock_output, stderr=""
        )

        snapshots = self.zfs_ops.list_snapshots("zroot/data")

        assert len(snapshots) == 3
        assert snapshots[0].used == "1.23G"
        assert snapshots[0].referenced == "512M"
        assert snapshots[1].used == "0"
        assert snapshots[1].referenced == "0"
        assert snapshots[2].used == "500K"
        assert snapshots[2].referenced == "200K"

    def test_list_snapshots_malformed_output(self):
        """Test handling of malformed zfs list output."""
        # Mix of good and bad output
        mock_output = (
            "zroot/data@snap1\t1640995200\t1.23G\t512M\n"  # Good
            "zroot/data@snap2\t1641081600\t2.34G\n"  # Missing referenced
            "zroot/data@snap3\t1641168000\t3.45G\t2.01G\tgarbage\n"  # Extra column
            "zroot/data@snap4\tnotanumber\t1.00G\t500M\n"  # Invalid creation time
        )
        self.runner.run.return_value = MagicMock(
            returncode=0, stdout=mock_output, stderr=""
        )

        snapshots = self.zfs_ops.list_snapshots("zroot/data")

        # Should only include snapshots with complete, valid data
        assert len(snapshots) == 1
        assert snapshots[0].name == "zroot/data@snap1"

    def test_list_snapshots_with_timestamps(self):
        """Test parsing creation timestamps."""
        import time

        mock_output = f"zroot/data@snap1\t{int(time.time())}\t1.23G\t512M\n"
        self.runner.run.return_value = MagicMock(
            returncode=0, stdout=mock_output, stderr=""
        )

        snapshots = self.zfs_ops.list_snapshots("zroot/data")

        assert len(snapshots) == 1
        assert isinstance(snapshots[0].creation_time, int)
        assert snapshots[0].creation_time > 0

    def test_list_snapshots_command_construction(self):
        """Test that the correct zfs list command is constructed."""
        self.runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        self.zfs_ops.list_snapshots("zroot/data")

        self.runner.run.assert_called_once()
        args = self.runner.run.call_args[0][0]
        expected = [
            "zfs",
            "list",
            "-H",  # No headers
            "-o",
            "name,creation,used,referenced",
            "-t",
            "snapshot",
            "zroot/data",
        ]
        assert args == expected
