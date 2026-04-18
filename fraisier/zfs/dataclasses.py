"""ZFS dataclasses for structured data."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Snapshot:
    """Represents a ZFS snapshot with metadata."""

    name: str
    creation_time: int
    used: str
    referenced: str

    @property
    def dataset_name(self) -> str:
        """Extract dataset name from snapshot name."""
        return self.name.split("@")[0]

    @property
    def snapshot_name(self) -> str:
        """Extract snapshot name from full snapshot name."""
        return self.name.split("@")[1]

    def age_days(self, current_time: int | None = None) -> float:
        """Calculate age of snapshot in days."""
        import time

        current = current_time or int(time.time())
        return (current - self.creation_time) / (24 * 60 * 60)

    def size_mb(self) -> float | None:
        """Parse used size into MB (approximate)."""
        return self._parse_size_to_mb(self.used)

    def referenced_mb(self) -> float | None:
        """Parse referenced size into MB (approximate)."""
        return self._parse_size_to_mb(self.referenced)

    def _parse_size_to_mb(self, size_str: str) -> float | None:
        """Parse ZFS size string to MB."""
        if not size_str or size_str == "0":
            return 0.0

        try:
            # Handle suffixes: K, M, G, T
            size_str = size_str.strip()
            if size_str.endswith("K"):
                return float(size_str[:-1]) / 1024
            elif size_str.endswith("M"):
                return float(size_str[:-1])
            elif size_str.endswith("G"):
                return float(size_str[:-1]) * 1024
            elif size_str.endswith("T"):
                return float(size_str[:-1]) * 1024 * 1024
            else:
                # Assume bytes if no suffix
                return float(size_str) / (1024 * 1024)
        except (ValueError, IndexError):
            return None
