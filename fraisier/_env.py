"""Safe environment variable helpers."""

import logging
import os

logger = logging.getLogger(__name__)


def get_int_env(key: str, default: int, *, min_value: int = 0) -> int:
    """Read an integer from an environment variable with safe fallback.

    Returns *default* when the variable is missing, not a valid integer,
    or below *min_value*.
    """
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using default %d", key, raw, default)
        return default
    if value < min_value:
        logger.warning(
            "%s=%d below minimum %d, using default %d",
            key,
            value,
            min_value,
            default,
        )
        return default
    return value
