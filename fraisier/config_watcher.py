"""Config change detection and hashing."""

import hashlib
from pathlib import Path


class ConfigWatcher:
    """Tracks fraises.yaml changes via SHA256 hashing."""

    HASH_FILENAME = ".config_hash"
    HASH_ALGORITHM = "sha256"

    def __init__(self, project_dir: Path) -> None:
        """Initialize config watcher.

        Args:
            project_dir: Project directory (e.g., /opt/my_project)
        """
        self.project_dir = Path(project_dir)
        self.config_file = self.project_dir / "fraises.yaml"
        self.hash_file = self.project_dir / self.HASH_FILENAME

    def compute_hash(self) -> str:
        """Compute hash of current fraises.yaml.

        Returns:
            Hexadecimal SHA256 hash of file contents

        Raises:
            FileNotFoundError: If fraises.yaml doesn't exist
        """
        if not self.config_file.exists():
            raise FileNotFoundError(f"Config not found: {self.config_file}")

        hasher = hashlib.sha256()
        with self.config_file.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)

        return hasher.hexdigest()

    def get_previous_hash(self) -> str | None:
        """Get stored hash from previous deployment.

        Returns:
            Previous hash if exists, None otherwise
        """
        if not self.hash_file.exists():
            return None

        try:
            return self.hash_file.read_text().strip()
        except OSError:
            return None

    def has_changed(self) -> bool:
        """Check if config has changed since last deployment.

        Returns:
            True if config changed or first run, False if unchanged
        """
        try:
            current = self.compute_hash()
            previous = self.get_previous_hash()

            if previous is None:
                return True  # First run

            return current != previous
        except FileNotFoundError:
            return True  # Config doesn't exist, treat as change

    def save_hash(self) -> None:
        """Save current hash to disk for next comparison.

        Raises:
            OSError: If unable to write hash file
        """
        try:
            current = self.compute_hash()
            self.hash_file.write_text(current)
        except OSError as e:
            raise OSError(f"Failed to save config hash: {e}") from e
