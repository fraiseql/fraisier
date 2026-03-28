"""Rate limiter for webhook requests.

The in-process rate limiter tracks requests per IP with an LRU eviction
policy.  For production multi-worker deployments behind a reverse proxy,
use nginx ``limit_req`` as the primary rate limiter — this module serves
as a per-worker safety net.
"""

import os
import time
from collections import OrderedDict

from fraisier._env import get_int_env

_MAX_TRACKED_IPS = 256
_RATE_LIMIT = get_int_env("FRAISIER_WEBHOOK_RATE_LIMIT", default=10, min_value=1)
_request_times: OrderedDict[str, list[float]] = OrderedDict()

# Trusted proxy IPs whose X-Forwarded-For header we honour
_TRUSTED_PROXIES: set[str] = set(
    filter(None, os.getenv("FRAISIER_TRUSTED_PROXIES", "").split(","))
)


def get_client_ip(
    remote_addr: str,
    *,
    headers: dict[str, str] | None = None,
    trusted_proxies: set[str] | None = None,
) -> str:
    """Extract the real client IP, respecting trusted proxy headers.

    When *remote_addr* is a trusted proxy, the rightmost non-proxy IP
    from ``X-Forwarded-For`` (or ``X-Real-IP``) is used instead.
    """
    proxies = trusted_proxies if trusted_proxies is not None else _TRUSTED_PROXIES
    if not proxies or remote_addr not in proxies or not headers:
        return remote_addr

    # X-Real-IP takes precedence (single value, set by nginx)
    real_ip = headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    # X-Forwarded-For: client, proxy1, proxy2 — take the leftmost
    xff = headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()

    return remote_addr


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
