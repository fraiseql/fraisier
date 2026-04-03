"""Pre-deploy backup hook using pg_dump."""

from __future__ import annotations

import gzip
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fraisier.hooks.base import HookResult

if TYPE_CHECKING:
    from fraisier.hooks.base import HookContext

logger = logging.getLogger(__name__)


class BackupHook:
    """Run pg_dump before deployment, with optional compression and retention."""

    def __init__(
        self,
        backup_dir: str | Path,
        database_url: str,
        compress: bool = True,
        max_backups: int = 10,
    ):
        self.backup_dir = Path(backup_dir)
        self.database_url = database_url
        self.compress = compress
        self.max_backups = max_backups

    @property
    def name(self) -> str:
        return "backup"

    def execute(self, context: HookContext) -> HookResult:
        """Run pg_dump and enforce retention policy."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        filename = (
            f"{context.fraise_name}_{context.environment}"
            f"_{timestamp}.sql"
        )
        if self.compress:
            filename += ".gz"

        backup_path = self.backup_dir / filename

        try:
            result = subprocess.run(
                ["pg_dump", self.database_url],
                capture_output=True,
                check=True,
            )
            if self.compress:
                backup_path.write_bytes(gzip.compress(result.stdout))
            else:
                backup_path.write_bytes(result.stdout)

            logger.info("Backup written: %s", backup_path)
            self._enforce_retention()

            return HookResult(success=True, hook_name=self.name)
        except subprocess.CalledProcessError as exc:
            error = exc.stderr.decode(errors="replace") if exc.stderr else str(exc)
            return HookResult(
                success=False,
                hook_name=self.name,
                error=f"pg_dump failed: {error}",
            )
        except OSError as exc:
            return HookResult(
                success=False,
                hook_name=self.name,
                error=f"Backup write failed: {exc}",
            )

    def _enforce_retention(self) -> None:
        """Remove oldest backups beyond max_backups."""
        pattern = "*.sql.gz" if self.compress else "*.sql"
        backups = sorted(
            self.backup_dir.glob(pattern),
            key=lambda p: p.stat().st_mtime,
        )
        for old in backups[: -self.max_backups]:
            old.unlink()
            logger.info("Removed old backup: %s", old.name)
