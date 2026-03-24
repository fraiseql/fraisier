"""Rate limiter for webhook requests."""

import time
from collections import OrderedDict

from fraisier._env import get_int_env

_MAX_TRACKED_IPS = 256
_RATE_LIMIT = get_int_env("FRAISIER_WEBHOOK_RATE_LIMIT", default=10, min_value=1)
_request_times: OrderedDict[str, list[float]] = OrderedDict()


def check_rate_limit(client_ip: str) -> bool:
    """Return True if the request is within the rate limit (per minute)."""
    now = time.time()
    while len(_request_times) > _MAX_TRACKED_IPS:
        _request_times.popitem(last=False)
    window = [t for t in _request_times.get(client_ip, []) if now - t < 60]
    _request_times[client_ip] = window
    _request_times.move_to_end(client_ip)
    if len(window) >= _RATE_LIMIT:
        return False
    _request_times[client_ip].append(now)
    return True


def reset() -> None:
    """Reset rate limiter state (for testing)."""
    _request_times.clear()
