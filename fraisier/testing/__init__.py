"""Test database lifecycle management for fraisier projects.

Provides a pytest fixture factory for template-based test databases
with hash-based change detection.

Usage::

    # conftest.py
    from fraisier.testing import database_fixture

    database = database_fixture(env="test")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fraisier.testing._manager import TemplateInfo, TemplateManager, TemplateStatus

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "TemplateInfo",
    "TemplateManager",
    "TemplateStatus",
    "database_fixture",
]


def database_fixture(
    env: str = "test",
    *,
    project_dir: Path | None = None,
    confiture_config: Path | str | None = None,
    connection_url: str | None = None,
    scope: str = "session",
) -> Any:
    """Create a pytest fixture that provides a test database template.

    Returns a pytest fixture function that manages the template lifecycle:

    - Computes schema hash via confiture's SchemaBuilder
    - Checks for existing template with matching hash
    - Rebuilds only when schema changes
    - Cleans up after the test session

    Args:
        env: Confiture environment name (default: "test").
        project_dir: Project root directory. Defaults to cwd.
        confiture_config: Path to confiture.yaml. Defaults to
            ``project_dir / "confiture.yaml"``.
        connection_url: PostgreSQL connection URL. When ``None``,
            falls back to sudo-based access.
        scope: Pytest fixture scope (default: "session").

    Returns:
        A pytest fixture function yielding a :class:`TemplateInfo`.

    Example::

        # conftest.py
        from fraisier.testing import database_fixture

        database = database_fixture(env="test")
    """
    from pathlib import Path as _Path

    import pytest

    resolved_dir = _Path(project_dir) if project_dir else _Path.cwd()
    resolved_config = (
        _Path(confiture_config) if confiture_config else resolved_dir / "confiture.yaml"
    )

    @pytest.fixture(scope=scope)
    def _database():
        manager = TemplateManager(
            env=env,
            project_dir=resolved_dir,
            confiture_config=resolved_config,
            connection_url=connection_url,
        )
        info = manager.ensure_template()
        yield info
        manager.cleanup()

    return _database
