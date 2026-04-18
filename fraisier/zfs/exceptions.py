"""ZFS-specific exceptions."""

from __future__ import annotations


class ZFSError(Exception):
    """Base exception for ZFS operations."""

    pass


class ZFSDatasetNotFoundError(ZFSError):
    """Raised when a ZFS dataset does not exist."""

    pass


class ZFSPermissionDeniedError(ZFSError):
    """Raised when ZFS operation fails due to insufficient permissions."""

    pass


class ZFSPoolOfflineError(ZFSError):
    """Raised when ZFS pool is offline or suspended."""

    pass


class ZFSOperationFailedError(ZFSError):
    """Raised when a ZFS operation fails for other reasons."""

    pass
