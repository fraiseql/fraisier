"""Tests for RebuildStrategy two-phase apply (Issue #32)."""

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.strategies import RebuildStrategy


@dataclass
class FakeSplitResult:
    """Minimal stand-in for confiture SplitBuildResult."""

    success: bool = True
    superuser_path: str = "/tmp/schema_test_superuser.sql"
    app_path: str = "/tmp/schema_test_app.sql"
    superuser_files: int = 0
    app_files: int = 1
    superuser_size_bytes: int = 0
    app_size_bytes: int = 100
    hash: str | None = None
    execution_time_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@pytest.fixture()
def _mock_rebuild_deps():
    """Patch all external dependencies of RebuildStrategy.execute()."""
    fake_env = MagicMock()
    fake_env.name = "test"
    fake_env.database_url = "postgresql://appuser@localhost/myapp"

    with (
        patch("builtins.open", MagicMock()),
        patch(
            "pathlib.Path.read_text",
            return_value=(
                "name: test\n"
                "database_url: postgresql://appuser@localhost/myapp\n"
                "include_dirs: [db/schema]\n"
            ),
        ),
        patch(
            "confiture.config.environment.Environment.model_validate",
            return_value=fake_env,
        ) as mock_validate,
        patch("confiture.core.builder.SchemaBuilder") as mock_builder_cls,
        patch("confiture.core.migrator.Migrator.from_config") as mock_migrator,
        patch("fraisier.strategies.terminate_backends"),
        patch("fraisier.strategies.drop_db"),
        patch("fraisier.strategies.create_db", return_value=(0, "", "")),
        patch.object(RebuildStrategy, "_apply_sql") as mock_apply_sql,
        patch("tempfile.mkdtemp", return_value="/tmp/fraisier_rebuild_test"),
        patch("shutil.rmtree"),
    ):
        mock_migrator_ctx = MagicMock()
        mock_migrator.return_value.__enter__ = MagicMock(return_value=mock_migrator_ctx)
        mock_migrator.return_value.__exit__ = MagicMock(return_value=False)

        yield {
            "env": fake_env,
            "builder_cls": mock_builder_cls,
            "apply_sql": mock_apply_sql,
            "validate": mock_validate,
        }


class TestRebuildTwoPhaseApply:
    """RebuildStrategy uses build_split() for two-phase apply."""

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_calls_build_split(self, _mock_rebuild_deps):
        """execute() calls build_split() instead of build()."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        strategy = RebuildStrategy()
        result = strategy.execute(Path("confiture.yaml"))

        assert result.success
        builder_instance.build_split.assert_called_once()
        builder_instance.build.assert_not_called()

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_skips_superuser_phase_when_no_files(self, _mock_rebuild_deps):
        """No superuser psql call when superuser_files == 0."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult(
            superuser_files=0,
        )

        strategy = RebuildStrategy()
        strategy.execute(Path("confiture.yaml"))

        # Only app phase — one call to _apply_sql
        assert mocks["apply_sql"].call_count == 1
        call_args = mocks["apply_sql"].call_args
        assert call_args[0][0] == "postgresql://appuser@localhost/myapp"

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_superuser_phase_uses_admin_url(self, _mock_rebuild_deps):
        """Superuser SQL is applied via admin_url rewritten to app db."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult(
            superuser_files=2,
            superuser_path="/tmp/schema_test_superuser.sql",
        )

        admin = "postgresql://postgres@localhost/postgres"
        strategy = RebuildStrategy(admin_url=admin)
        strategy.execute(Path("confiture.yaml"))

        # Two calls: superuser + app
        assert mocks["apply_sql"].call_count == 2

        su_call = mocks["apply_sql"].call_args_list[0]
        # admin_url should be rewritten to target the app database
        assert su_call[0][0] == "postgresql://postgres@localhost/myapp"

        app_call = mocks["apply_sql"].call_args_list[1]
        assert app_call[0][0] == "postgresql://appuser@localhost/myapp"

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_superuser_phase_falls_back_to_database_url(self, _mock_rebuild_deps):
        """Without admin_url, superuser SQL uses database_url."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult(
            superuser_files=1,
        )

        strategy = RebuildStrategy()  # no admin_url
        strategy.execute(Path("confiture.yaml"))

        assert mocks["apply_sql"].call_count == 2
        su_call = mocks["apply_sql"].call_args_list[0]
        # Falls back to database_url
        assert su_call[0][0] == "postgresql://appuser@localhost/myapp"
