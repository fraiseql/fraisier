"""Tests for mtime-based config auto-reload in ``get_config`` (#278)."""

from __future__ import annotations

import os

from fraisier.config import loader


def _write(path, name: str, mtime: float) -> None:
    """Write a minimal valid fraises.yaml carrying *name*, with a fixed mtime.

    mtime is set explicitly (not via wall clock) so the staleness check is
    deterministic regardless of filesystem timestamp resolution.
    """
    path.write_text(f"name: {name}\n")
    os.utime(path, (mtime, mtime))


def test_get_config_reloads_when_mtime_advances(tmp_path):
    """A newer on-disk mtime makes the next no-arg get_config() re-read."""
    loader.reset_config()
    p = tmp_path / "fraises.yaml"
    _write(p, "alpha", 1000)

    assert loader.get_config(p).project_name == "alpha"

    _write(p, "beta", 2000)
    # No explicit path, no reset_config(): the mtime bump alone forces reload.
    assert loader.get_config().project_name == "beta"


def test_get_config_stable_when_unchanged(tmp_path):
    """Two calls with no file change return the same object (no needless reload)."""
    loader.reset_config()
    p = tmp_path / "fraises.yaml"
    _write(p, "alpha", 1000)

    first = loader.get_config(p)
    second = loader.get_config()
    assert first is second


def test_get_config_survives_stat_error(tmp_path):
    """A missing tracked file (mid-atomic-replace) never crashes get_config()."""
    loader.reset_config()
    p = tmp_path / "fraises.yaml"
    _write(p, "alpha", 1000)
    cfg = loader.get_config(p)

    p.unlink()
    again = loader.get_config()
    assert again is cfg
    assert again.project_name == "alpha"


def test_get_config_keeps_previous_on_invalid_reload(tmp_path):
    """An invalid new config keeps the last-good singleton — no raise, no thrash.

    A bad ``fraises.yaml`` sync must not take down a running webhook: every
    get_config() consumer would otherwise start raising where the cached
    config used to serve.
    """
    loader.reset_config()
    p = tmp_path / "fraises.yaml"
    _write(p, "alpha", 1000)
    cfg = loader.get_config(p)
    assert cfg.project_name == "alpha"

    # Syntactically broken YAML, newer mtime → reload attempt raises internally.
    p.write_text("name: [unterminated\n")
    os.utime(p, (2000, 2000))

    again = loader.get_config()
    assert again is cfg
    assert again.project_name == "alpha"

    # The offending mtime is stamped, so a second call returns immediately
    # instead of rebuilding+raising on every access.
    assert loader.get_config() is cfg


def test_reset_config_clears_mtime(tmp_path):
    """reset_config() drops both the singleton and its stamped mtime."""
    loader.reset_config()
    p = tmp_path / "fraises.yaml"
    _write(p, "alpha", 1000)
    loader.get_config(p)
    assert loader._config_mtime is not None

    loader.reset_config()
    assert loader._config is None
    assert loader._config_mtime is None
