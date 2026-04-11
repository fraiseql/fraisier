"""Tests for fraisier.constants — centralised timeout and dedupe constants."""

from fraisier.constants import (
    DEFAULT_EXEC_TIMEOUT,
    HEALTH_CHECK_TIMEOUT,
    SSH_CONNECT_TIMEOUT,
    WEBHOOK_DEDUPE_MAX_ENTRIES,
    WEBHOOK_DEDUPE_WINDOW_SECONDS,
)


def test_default_exec_timeout():
    assert DEFAULT_EXEC_TIMEOUT == 300


def test_ssh_connect_timeout():
    assert SSH_CONNECT_TIMEOUT == 30


def test_health_check_timeout():
    assert HEALTH_CHECK_TIMEOUT == 5.0


def test_webhook_dedupe_window():
    assert WEBHOOK_DEDUPE_WINDOW_SECONDS == 600


def test_webhook_dedupe_max_entries():
    assert WEBHOOK_DEDUPE_MAX_ENTRIES == 4096


def test_runners_use_constants():
    """Runners must reference constants, not literal 300/30 for timeouts."""
    import inspect

    from fraisier import runners

    source = inspect.getsource(runners)
    # After centralisation, no bare timeout=300 or timeout=30 literals remain
    assert "timeout=300" not in source
    assert "timeout=30" not in source
    assert "connect_timeout=30" not in source
