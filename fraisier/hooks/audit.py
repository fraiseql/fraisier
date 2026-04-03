"""Post-deploy audit hook with HMAC-signed records."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fraisier.hooks.base import HookResult

if TYPE_CHECKING:
    from fraisier.hooks.base import HookContext

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS fraisier_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fraise TEXT NOT NULL,
    environment TEXT NOT NULL,
    phase TEXT NOT NULL,
    old_version TEXT,
    new_version TEXT,
    error_message TEXT,
    executed_at TEXT NOT NULL,
    signature TEXT NOT NULL
)
"""


class AuditHook:
    """Write HMAC-signed audit records to a SQLite database."""

    def __init__(
        self,
        database_path: str,
        signing_key: str,
    ):
        self.database_path = database_path
        self.signing_key = signing_key

    @property
    def name(self) -> str:
        return "audit"

    def execute(self, context: HookContext) -> HookResult:
        """Write a signed audit record."""
        try:
            timestamp = datetime.now(UTC).isoformat()
            record = {
                "fraise": context.fraise_name,
                "environment": context.environment,
                "phase": context.phase.value,
                "old_version": context.old_version,
                "new_version": context.new_version,
                "error_message": context.error_message,
                "executed_at": timestamp,
            }
            signature = self._sign(record)

            conn = sqlite3.connect(self.database_path)
            try:
                conn.execute(_CREATE_TABLE)
                conn.execute(
                    """INSERT INTO fraisier_audit_log
                    (fraise, environment, phase, old_version,
                     new_version, error_message, executed_at, signature)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        context.fraise_name,
                        context.environment,
                        context.phase.value,
                        context.old_version,
                        context.new_version,
                        context.error_message,
                        timestamp,
                        signature,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            logger.info(
                "Audit record written for %s/%s (%s)",
                context.fraise_name,
                context.environment,
                context.phase.value,
            )
            return HookResult(success=True, hook_name=self.name)
        except (sqlite3.Error, OSError) as exc:
            return HookResult(
                success=False,
                hook_name=self.name,
                error=f"Audit write failed: {exc}",
            )

    def _sign(self, record: dict) -> str:
        """Produce HMAC-SHA256 signature of the record."""
        payload = json.dumps(record, sort_keys=True)
        return hmac.new(
            self.signing_key.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def verify_signature(record: dict, signature: str, key: str) -> bool:
        """Verify a record's HMAC-SHA256 signature."""
        payload = json.dumps(record, sort_keys=True)
        expected = hmac.new(
            key.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature, expected)
