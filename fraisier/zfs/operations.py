"""ZFS operations for deployment snapshots and clones."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from fraisier.zfs.dataclasses import Snapshot
from fraisier.zfs.exceptions import (
    ZFSDatasetNotFoundError,
    ZFSError,
    ZFSOperationFailedError,
    ZFSPermissionDeniedError,
    ZFSPoolOfflineError,
)

if TYPE_CHECKING:
    from fraisier.runners import CommandRunner


class ZFSOperations:
    """Manage ZFS snapshots and clones for deployment."""

    def __init__(self, runner: CommandRunner) -> None:
        """Initialize with a command runner.

        Args:
            runner: CommandRunner instance for executing ZFS commands
        """
        self._cmd = ZFSCommand(runner)

    def _validate_dataset_name(self, dataset: str) -> None:
        """Validate dataset name format."""
        if not dataset or not isinstance(dataset, str):
            raise ZFSOperationFailedError(f"Invalid dataset name: {dataset!r}")
        if "@" in dataset:
            raise ZFSOperationFailedError(
                f"Dataset name cannot contain '@': {dataset!r}"
            )

    def _validate_snapshot_reference(self, snapshot: str) -> None:
        """Validate snapshot reference format (dataset@snapshot)."""
        if not snapshot or not isinstance(snapshot, str):
            raise ZFSOperationFailedError(f"Invalid snapshot reference: {snapshot!r}")
        if snapshot.count("@") != 1:
            raise ZFSOperationFailedError(
                f"Snapshot reference must contain exactly one '@': {snapshot!r}"
            )
        dataset, snap_name = snapshot.split("@")
        if not dataset or not snap_name:
            raise ZFSOperationFailedError(
                f"Invalid snapshot reference format: {snapshot!r}"
            )

    def create_snapshot(
        self,
        dataset: str,
        snapshot_name: str | None = None,
        recursive: bool = False,
        prefix: str = "snap",
    ) -> str:
        """Create a ZFS snapshot. Returns snapshot full path.

        Args:
            dataset: Dataset to snapshot
            snapshot_name: Name for the snapshot. If None, generates timestamped name
            recursive: Whether to create recursive snapshot
            prefix: Prefix for auto-generated snapshot names

        Returns:
            Full snapshot path (dataset@snapshot_name)

        Raises:
            ZFSError: If snapshot creation fails
        """
        self._validate_dataset_name(dataset)

        if snapshot_name is not None and not snapshot_name:
            raise ZFSOperationFailedError("Snapshot name cannot be empty")

        if snapshot_name is None:
            # Generate timestamped snapshot name
            timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            snapshot_name = f"{prefix}_{timestamp}"

        # Retry logic for transient failures (like "snapshot already exists"
        # race conditions)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                cmd = self._cmd._build_snapshot_cmd(dataset, snapshot_name, recursive)
                self._cmd._run_command(cmd)
                return f"{dataset}@{snapshot_name}"
            except ZFSOperationFailedError as e:
                error_msg = str(e).lower()
                if "already exists" in error_msg and attempt < max_retries - 1:
                    # Exponential backoff: 0.1s, 0.2s, 0.4s
                    delay = 0.1 * (2**attempt)
                    time.sleep(delay)
                    timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
                    snapshot_name = f"{prefix}_{timestamp}"
                    continue
                raise
        # This should never be reached, but satisfies type checker
        raise ZFSOperationFailedError("Snapshot creation failed after all retries")

    def create_snapshot_and_clone(
        self,
        dataset: str,
        clone_dataset: str,
        snapshot_name: str | None = None,
        clone_properties: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Atomically create snapshot and clone. Returns (snapshot_path, clone_path).

        This provides transaction-like semantics: if clone creation fails,
        the snapshot remains for manual cleanup or retention policies.

        Args:
            dataset: Dataset to snapshot
            clone_dataset: Target clone dataset name
            snapshot_name: Name for the snapshot. If None, generates timestamped name
            clone_properties: Optional ZFS properties to set on clone

        Returns:
            Tuple of (snapshot_full_path, clone_path)

        Raises:
            ZFSError: If snapshot or clone creation fails
        """
        # Create snapshot first
        snapshot_path = self.create_snapshot(dataset, snapshot_name=snapshot_name)

        try:
            # Create clone from the snapshot
            self.clone_snapshot(
                snapshot_path, clone_dataset, properties=clone_properties
            )
            return snapshot_path, clone_dataset
        except Exception:
            # Clone failed - snapshot remains for cleanup by retention policies
            logger.warning(
                f"Clone creation failed, snapshot {snapshot_path} left for cleanup"
            )
            raise

    @contextmanager
    def temporary_clone(
        self,
        snapshot: str,
        clone_dataset: str,
        properties: dict[str, str] | None = None,
    ):
        """Context manager for temporary clones that are automatically cleaned up.

        Args:
            snapshot: Snapshot to clone from
            clone_dataset: Temporary clone dataset name
            properties: Optional ZFS properties for the clone

        Yields:
            The clone dataset path

        Raises:
            ZFSError: If clone creation fails
        """
        # Create the temporary clone
        self.clone_snapshot(snapshot, clone_dataset, properties=properties)

        try:
            yield clone_dataset
        finally:
            # Always attempt cleanup, even if an exception occurred
            try:
                self.destroy_clone(clone_dataset)
            except Exception as e:
                logger.warning(
                    f"Failed to cleanup temporary clone {clone_dataset}: {e}"
                )
                # Don't re-raise cleanup errors

    def clone_snapshot(
        self,
        snapshot: str,
        clone_dataset: str,
        properties: dict[str, str] | None = None,
    ) -> str:
        """Clone a snapshot to new dataset. Returns clone path.

        Args:
            snapshot: Snapshot to clone from (format: dataset@snapshot)
            clone_dataset: Target clone dataset name
            properties: Optional ZFS properties to set on clone

        Returns:
            Full clone dataset path

        Raises:
            ZFSError: If cloning fails
        """
        self._validate_snapshot_reference(snapshot)
        self._validate_dataset_name(clone_dataset)

        cmd = self._cmd._build_clone_cmd(snapshot, clone_dataset, properties)
        self._cmd._run_command(cmd)
        return clone_dataset

    def list_snapshots(self, dataset: str, prefix: str | None = None) -> list[Snapshot]:
        """List snapshots for a dataset.

        Args:
            dataset: Dataset to list snapshots for
            prefix: Optional prefix to filter snapshots by

        Returns:
            List of Snapshot objects, sorted by creation time

        Raises:
            ZFSError: If listing fails
        """
        cmd = self._cmd._build_list_cmd(dataset)
        result = self._cmd._run_command(cmd)
        snapshots = self._cmd._parse_list_output(result.stdout)

        # Convert to Snapshot objects
        snapshot_objects = []
        for snap_data in snapshots:
            try:
                snapshot = Snapshot(
                    name=snap_data["name"],
                    creation_time=int(snap_data["creation"]),
                    used=snap_data["used"],
                    referenced=snap_data["referenced"],
                )
                snapshot_objects.append(snapshot)
            except (KeyError, ValueError) as e:
                logger.warning(f"Failed to parse snapshot data {snap_data}: {e}")
                continue

        # Filter by prefix if specified
        if prefix:
            snapshot_objects = [
                s for s in snapshot_objects if s.snapshot_name.startswith(prefix)
            ]

        # Sort by creation time (should already be sorted by zfs, but ensure it)
        snapshot_objects.sort(key=lambda s: s.creation_time)

        return snapshot_objects

    def destroy_clone(
        self, clone_dataset: str, recursive: bool = False, force: bool = False
    ) -> None:
        """Destroy a clone dataset safely.

        Args:
            clone_dataset: Clone dataset to destroy
            recursive: Whether to destroy recursively
            force: Whether to force destroy (unmount, etc.)

        Raises:
            ZFSError: If destruction fails or safety checks fail
        """
        # Safety check: verify this is actually a clone
        try:
            result = self._cmd._run_command(
                self._cmd._build_get_cmd(clone_dataset, ["type"])
            )
            dataset_type = self._cmd._parse_get_output(result.stdout, ["type"])
            if dataset_type.get("type") != "clone":
                actual = dataset_type.get("type")
                raise ZFSOperationFailedError(
                    f"Dataset '{clone_dataset}' is not a clone (type: {actual})"
                )
        except ZFSDatasetNotFoundError:
            raise  # Re-raise dataset not found
        except ZFSOperationFailedError:
            raise  # Re-raise if not a clone
        except Exception as e:
            logger.warning(f"Could not verify dataset type for '{clone_dataset}': {e}")
            # Continue with destroy anyway - let ZFS handle the error

        # Destroy the clone
        cmd = self._cmd._build_destroy_cmd(
            clone_dataset, recursive=recursive, force=force
        )
        self._cmd._run_command(cmd)

    def cleanup_old_snapshots(
        self,
        dataset: str,
        keep_count: int = 10,
        prefix: str | None = None,
        max_age_days: float | None = None,
    ) -> list[str]:
        """Remove old snapshots, keep N most recent.

        Args:
            dataset: Dataset to clean snapshots for
            keep_count: Number of most recent snapshots to keep
            prefix: Optional prefix to filter snapshots by
            max_age_days: Optional maximum age in days for snapshots to keep

        Returns:
            List of deleted snapshot names

        Raises:
            ZFSError: If listing or deletion fails
        """
        # List snapshots matching criteria
        snapshots = self.list_snapshots(dataset, prefix=prefix)

        # Separate snapshots by age criteria
        import time

        current_time = int(time.time())
        snapshots_to_delete = []

        if max_age_days is not None:
            # Delete snapshots older than max_age_days
            max_age_seconds = max_age_days * 24 * 60 * 60
            snapshots_to_delete.extend(
                s for s in snapshots if current_time - s.creation_time > max_age_seconds
            )

        # Remove age-filtered snapshots from consideration for count-based cleanup
        remaining_snapshots = [s for s in snapshots if s not in snapshots_to_delete]

        # Apply count-based cleanup to remaining snapshots
        if len(remaining_snapshots) > keep_count or keep_count == 0:
            sorted_remaining = sorted(
                remaining_snapshots, key=lambda s: s.creation_time
            )
            if keep_count == 0:
                count_based_deletions = sorted_remaining
            else:
                count_based_deletions = sorted_remaining[:-keep_count]
            snapshots_to_delete.extend(count_based_deletions)

        deleted_snapshots = []

        # Delete snapshots in order (oldest first)
        for snapshot in snapshots_to_delete:
            try:
                cmd = self._cmd._build_destroy_cmd(
                    snapshot.name, recursive=False, force=False
                )
                self._cmd._run_command(cmd)
                deleted_snapshots.append(snapshot.name)
            except Exception as e:
                logger.warning(f"Failed to delete snapshot {snapshot.name}: {e}")
                # Continue with other snapshots even if one fails
                continue

        return deleted_snapshots


logger = logging.getLogger(__name__)


class ZFSCommand:
    """Low-level ZFS command execution wrapper."""

    def __init__(self, runner: CommandRunner) -> None:
        """Initialize with a command runner.

        Args:
            runner: CommandRunner instance for executing ZFS commands
        """
        self.runner = runner

    def _run_command(
        self,
        cmd: list[str],
        *,
        timeout: int = 30,
        check: bool = True,
    ) -> Any:
        """Execute a ZFS command and return the result.

        Args:
            cmd: Command arguments
            timeout: Command timeout in seconds
            check: Whether to raise on non-zero exit code

        Returns:
            CompletedProcess result

        Raises:
            ZFSError: If command fails with ZFS-specific error
        """
        try:
            return self.runner.run(cmd, timeout=timeout, check=check)
        except Exception as e:
            logger.error(f"ZFS command failed: {' '.join(cmd)}")
            error_msg = self._build_error_message(cmd, e)
            raise self._classify_error(error_msg) from e

    def _build_error_message(self, cmd: list[str], e: Exception) -> str:
        """Build detailed error message with context.

        Args:
            cmd: The command that failed
            e: The original exception

        Returns:
            Detailed error message
        """
        operation = cmd[1] if len(cmd) > 1 else "unknown"
        dataset = self._extract_dataset_from_cmd(cmd)

        error_msg = f"ZFS {operation} operation failed"
        if dataset:
            error_msg += f" on dataset '{dataset}'"

        stderr = getattr(e, "stderr", None)
        if stderr:
            error_msg += f": {stderr.strip()}"
        else:
            error_msg += f": {e!s}"

        return error_msg

    def _extract_dataset_from_cmd(self, cmd: list[str]) -> str | None:
        """Extract dataset name from ZFS command.

        Args:
            cmd: ZFS command list

        Returns:
            Dataset name if found, None otherwise
        """
        # Look for dataset patterns in the command
        for arg in cmd[2:]:  # Skip 'zfs' and operation
            if "@" in arg:  # snapshot reference
                return arg.split("@")[0]
            if "/" in arg and not arg.startswith("-"):  # dataset path
                return arg
        return None

    def _classify_error(self, error_msg: str) -> ZFSError:
        """Classify error message into specific ZFS exception type.

        Args:
            error_msg: The error message to classify

        Returns:
            Appropriate ZFSError subclass
        """
        lower_msg = error_msg.lower()

        # Check for permission denied first (most specific)
        if "permission denied" in lower_msg:
            return ZFSPermissionDeniedError(error_msg)
        # Check for pool offline/suspended
        elif "pool" in lower_msg and (
            "offline" in lower_msg
            or "suspended" in lower_msg
            or "i/o is currently" in lower_msg
        ):
            return ZFSPoolOfflineError(error_msg)
        # Check for dataset not found
        elif "dataset does not exist" in lower_msg or "cannot open" in lower_msg:
            return ZFSDatasetNotFoundError(error_msg)
        else:
            return ZFSOperationFailedError(error_msg)

    def _build_snapshot_cmd(
        self,
        dataset: str,
        snapshot_name: str,
        recursive: bool = False,
    ) -> list[str]:
        """Build zfs snapshot command.

        Args:
            dataset: Dataset name
            snapshot_name: Snapshot name
            recursive: Whether to create recursive snapshot

        Returns:
            Command list
        """
        cmd = ["zfs", "snapshot"]
        if recursive:
            cmd.append("-r")
        cmd.append(f"{dataset}@{snapshot_name}")
        return cmd

    def _build_clone_cmd(
        self,
        snapshot: str,
        clone_dataset: str,
        properties: dict[str, str] | None = None,
    ) -> list[str]:
        """Build zfs clone command.

        Args:
            snapshot: Snapshot to clone from
            clone_dataset: Target clone dataset
            properties: Optional ZFS properties to set

        Returns:
            Command list
        """
        cmd = ["zfs", "clone"]
        if properties:
            for key, value in properties.items():
                cmd.extend(["-o", f"{key}={value}"])
        cmd.extend([snapshot, clone_dataset])
        return cmd

    def _build_destroy_cmd(
        self,
        dataset: str,
        recursive: bool = False,
        force: bool = False,
    ) -> list[str]:
        """Build zfs destroy command.

        Args:
            dataset: Dataset to destroy
            recursive: Whether to destroy recursively
            force: Whether to force destroy

        Returns:
            Command list
        """
        cmd = ["zfs", "destroy"]
        if recursive:
            cmd.append("-r")
        if force:
            cmd.append("-f")
        cmd.append(dataset)
        return cmd

    def _build_list_cmd(self, dataset: str) -> list[str]:
        """Build zfs list command for snapshots.

        Args:
            dataset: Dataset to list snapshots for

        Returns:
            Command list
        """
        return [
            "zfs",
            "list",
            "-H",  # No headers
            "-o",
            "name,creation,used,referenced",
            "-t",
            "snapshot",
            dataset,
        ]

    def _build_get_cmd(self, dataset: str, properties: list[str]) -> list[str]:
        """Build zfs get command.

        Args:
            dataset: Dataset to get properties for
            properties: List of properties to get

        Returns:
            Command list
        """
        return [
            "zfs",
            "get",
            "-H",  # No headers
            "-o",
            "value",
            ",".join(properties),
            dataset,
        ]

    def _parse_list_output(self, output: str) -> list[dict[str, str]]:
        """Parse zfs list output.

        Args:
            output: Raw output from zfs list

        Returns:
            List of parsed snapshot info
        """
        if not output.strip():
            return []

        lines = output.strip().split("\n")
        result = []
        for line in lines:
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) == 4:
                result.append(
                    {
                        "name": parts[0],
                        "creation": parts[1],
                        "used": parts[2],
                        "referenced": parts[3],
                    }
                )
        return result

    def _parse_get_output(self, output: str, properties: list[str]) -> dict[str, str]:
        """Parse zfs get output.

        Args:
            output: Raw output from zfs get
            properties: List of properties that were requested

        Returns:
            Dictionary of property values
        """
        if not output.strip():
            return {}

        lines = output.strip().split("\n")
        result = {}
        for i, line in enumerate(lines):
            if i < len(properties):
                result[properties[i]] = line.strip()
        return result
