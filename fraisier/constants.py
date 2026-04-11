"""Shared numeric constants for fraisier.

All timeout values are in seconds.
"""

# Execution timeouts
DEFAULT_EXEC_TIMEOUT: int = 300
SSH_CONNECT_TIMEOUT: int = 30
HEALTH_CHECK_TIMEOUT: float = 5.0

# Webhook replay-protection dedupe window
WEBHOOK_DEDUPE_WINDOW_SECONDS: int = 600  # 10 min — covers GitHub's retry window
WEBHOOK_DEDUPE_MAX_ENTRIES: int = 4096
