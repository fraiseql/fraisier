"""Config change detection and hashing."""

import hashlib
import logging
from pathlib import Path

import yaml

logger = logging.getLogger("fraisier")


class ConfigWatcher:
    """Tracks fraises.yaml — and any custom template tree — via SHA256."""

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
        """Compute hash of fraises.yaml plus any ``scaffold.template_dir`` tree.

        The template tree is included because a commit that customises a
        template without touching fraises.yaml is still a change the server
        must re-render for — hashing the config alone meant regeneration never
        ran for it, so the customisation could not take effect even once the
        templates were being synced (#312).

        Returns:
            Hexadecimal SHA256 hash

        Raises:
            FileNotFoundError: If fraises.yaml doesn't exist
        """
        if not self.config_file.exists():
            raise FileNotFoundError(f"Config not found: {self.config_file}")

        hasher = hashlib.sha256()
        with self.config_file.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)

        self._hash_template_dir(hasher)
        return hasher.hexdigest()

    def _template_dir(self) -> Path | None:
        """Resolve ``scaffold.template_dir`` relative to the project directory.

        Parsed straight from the YAML rather than through FraisierConfig: this
        runs before regeneration decides anything, and a config that fails to
        fully validate must not make change detection raise.
        """
        try:
            raw = yaml.safe_load(self.config_file.read_text()) or {}
            configured = (raw.get("scaffold") or {}).get("template_dir")
        except Exception:
            logger.debug("Could not read template_dir for hashing", exc_info=True)
            return None
        if not configured:
            return None
        path = Path(configured)
        return path if path.is_absolute() else self.project_dir / path

    def _hash_template_dir(self, hasher) -> None:
        """Fold every template's path and content into *hasher*.

        Sorted, and the relative path is hashed alongside the bytes, so the
        digest is order-independent and a rename counts as a change.
        """
        template_dir = self._template_dir()
        if template_dir is None or not template_dir.is_dir():
            return

        for path in sorted(p for p in template_dir.rglob("*") if p.is_file()):
            hasher.update(str(path.relative_to(template_dir)).encode())
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hasher.update(chunk)

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
