"""Tests for RebuildStrategy three-phase apply and _apply_sql (Issues #32, #38, #39)."""

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.strategies import RebuildStrategy


@dataclass
class FakeSplitResult:
    """Minimal stand-in for confiture SplitBuildResult."""

    success: bool = True
    superuser_pre_path: str = "/tmp/schema_test_superuser_pre.sql"
    app_path: str = "/tmp/schema_test_app.sql"
    superuser_post_path: str = "/tmp/schema_test_superuser_post.sql"
    superuser_pre_files: int = 0
    app_files: int = 1
    superuser_post_files: int = 0
    superuser_pre_size_bytes: int = 0
    app_size_bytes: int = 100
    superuser_post_size_bytes: int = 0
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
        patch("fraisier.strategies._core.terminate_backends"),
        patch("fraisier.strategies._core.drop_db"),
        patch("fraisier.strategies._core.create_db", return_value=(0, "", "")),
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


class TestRebuildThreePhaseApply:
    """RebuildStrategy uses build_split() for three-phase apply."""

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
        """No superuser psql call when superuser_pre_files == 0."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult(
            superuser_pre_files=0,
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
            superuser_pre_files=2,
            superuser_pre_path="/tmp/schema_test_superuser_pre.sql",
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
            superuser_pre_files=1,
        )

        strategy = RebuildStrategy()  # no admin_url
        strategy.execute(Path("confiture.yaml"))

        assert mocks["apply_sql"].call_count == 2
        su_call = mocks["apply_sql"].call_args_list[0]
        # Falls back to database_url
        assert su_call[0][0] == "postgresql://appuser@localhost/myapp"

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_superuser_post_phase_runs_after_app(self, _mock_rebuild_deps):
        """Phase 3 (superuser_post) runs after app phase via admin_url."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult(
            superuser_pre_files=1,
            superuser_post_files=2,
            superuser_post_path="/tmp/schema_test_superuser_post.sql",
        )

        admin = "postgresql://postgres@localhost/postgres"
        strategy = RebuildStrategy(admin_url=admin)
        strategy.execute(Path("confiture.yaml"))

        # Three calls: superuser_pre + app + superuser_post
        assert mocks["apply_sql"].call_count == 3

        su_pre_call = mocks["apply_sql"].call_args_list[0]
        assert su_pre_call[0][0] == "postgresql://postgres@localhost/myapp"

        app_call = mocks["apply_sql"].call_args_list[1]
        assert app_call[0][0] == "postgresql://appuser@localhost/myapp"

        su_post_call = mocks["apply_sql"].call_args_list[2]
        assert su_post_call[0][0] == "postgresql://postgres@localhost/myapp"
        assert su_post_call[0][1] == Path("/tmp/schema_test_superuser_post.sql")

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_skips_superuser_post_when_no_files(self, _mock_rebuild_deps):
        """No post-schema psql call when superuser_post_files == 0."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult(
            superuser_pre_files=1,
            superuser_post_files=0,
        )

        admin = "postgresql://postgres@localhost/postgres"
        strategy = RebuildStrategy(admin_url=admin)
        strategy.execute(Path("confiture.yaml"))

        # Two calls: superuser_pre + app (no post)
        assert mocks["apply_sql"].call_count == 2

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_superuser_post_falls_back_to_database_url(self, _mock_rebuild_deps):
        """Without admin_url, superuser_post SQL uses database_url."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult(
            superuser_pre_files=0,
            superuser_post_files=1,
        )

        strategy = RebuildStrategy()  # no admin_url
        strategy.execute(Path("confiture.yaml"))

        # Two calls: app + superuser_post (no pre since superuser_pre_files=0)
        assert mocks["apply_sql"].call_count == 2
        post_call = mocks["apply_sql"].call_args_list[1]
        assert post_call[0][0] == "postgresql://appuser@localhost/myapp"


class TestApplySqlLogsStderr:
    """_apply_sql logs stderr before raising (#38)."""

    @patch("subprocess.run")
    def test_logs_stderr_on_failure(self, mock_run, caplog):
        """stderr from psql is logged at ERROR level when the command fails."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="some stdout output",
            stderr="ERROR: relation does not exist",
        )

        with (
            caplog.at_level(logging.ERROR, logger="fraisier.strategies"),
            pytest.raises(subprocess.CalledProcessError),
        ):
            RebuildStrategy._apply_sql("postgresql://u@h/db", Path("/tmp/s.sql"))

        assert "ERROR: relation does not exist" in caplog.text

    @patch("subprocess.run")
    def test_called_process_error_includes_stdout(self, mock_run):
        """CalledProcessError.output contains stdout from psql."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="stdout content",
            stderr="stderr content",
        )

        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            RebuildStrategy._apply_sql("postgresql://u@h/db", Path("/tmp/s.sql"))

        assert exc_info.value.output == "stdout content"
        assert exc_info.value.stderr == "stderr content"

    @patch("subprocess.run")
    def test_no_log_on_success(self, mock_run, caplog):
        """No error log when psql succeeds."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with caplog.at_level(logging.ERROR, logger="fraisier.strategies"):
            RebuildStrategy._apply_sql("postgresql://u@h/db", Path("/tmp/s.sql"))

        assert caplog.text == ""


class TestRebuildStrategyTemplateCreation:
    """RebuildStrategy create_template snapshots the rebuilt DB."""

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_execute_no_template_skips_template_ops(self, _mock_rebuild_deps):
        """Without create_template, terminate/drop/create are called once each (for the main DB)."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends") as mock_term,
            patch("fraisier.strategies._core.drop_db") as mock_drop,
            patch(
                "fraisier.strategies._core.create_db", return_value=(0, "", "")
            ) as mock_create,
        ):
            strategy = RebuildStrategy(
                create_template=False,
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        # Only the main DB: terminate once, drop once, create once
        assert mock_term.call_count == 1
        assert mock_drop.call_count == 1
        assert mock_create.call_count == 1

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_execute_with_template_calls_create_db_with_template(
        self, _mock_rebuild_deps
    ):
        """With create_template=True, create_db is called with template=<db_name>."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch(
                "fraisier.strategies._core.create_db", return_value=(0, "", "")
            ) as mock_create,
        ):
            strategy = RebuildStrategy(
                create_template=True,
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        # Second call is template creation: create_db(template_name, template=db_name)
        assert mock_create.call_count == 2
        template_call = mock_create.call_args_list[1]
        assert template_call[0][0] == "template_myapp"
        assert template_call[1]["template"] == "myapp"

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_execute_with_template_terminates_source_before_clone(
        self, _mock_rebuild_deps
    ):
        """Source DB connections are terminated before cloning to template."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        terminate_calls = []

        with (
            patch(
                "fraisier.strategies._core.terminate_backends",
                side_effect=lambda db, **_kw: terminate_calls.append(db),
            ),
            patch("fraisier.strategies._core.drop_db"),
            patch("fraisier.strategies._core.create_db", return_value=(0, "", "")),
        ):
            strategy = RebuildStrategy(
                create_template=True,
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        # Expected order: terminate myapp (initial rebuild), terminate template_myapp,
        # terminate myapp (pre-stamp), terminate myapp (pre-clone, belt-and-suspenders).
        assert "myapp" in terminate_calls
        assert "template_myapp" in terminate_calls
        # myapp terminated three times: initial drop, pre-stamp, pre-clone.
        assert terminate_calls.count("myapp") == 3

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_execute_with_template_drops_existing_template_first(
        self, _mock_rebuild_deps
    ):
        """Existing template DB is dropped before recreating."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        drop_calls = []

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch(
                "fraisier.strategies._core.drop_db",
                side_effect=lambda db, **_kw: drop_calls.append(db),
            ),
            patch("fraisier.strategies._core.create_db", return_value=(0, "", "")),
        ):
            strategy = RebuildStrategy(
                create_template=True,
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        # Both main DB and template DB are dropped
        assert "myapp" in drop_calls
        assert "template_myapp" in drop_calls

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_execute_with_default_template_name(self, _mock_rebuild_deps):
        """Default template name is template_<db_name>."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch(
                "fraisier.strategies._core.create_db", return_value=(0, "", "")
            ) as mock_create,
        ):
            strategy = RebuildStrategy(
                create_template=True,
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        template_call = mock_create.call_args_list[1]
        assert template_call[0][0] == "template_myapp"

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_execute_with_explicit_template_name(self, _mock_rebuild_deps):
        """Explicit template_name overrides the default naming."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch(
                "fraisier.strategies._core.create_db", return_value=(0, "", "")
            ) as mock_create,
        ):
            strategy = RebuildStrategy(
                create_template=True,
                template_name="custom_snapshot",
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        template_call = mock_create.call_args_list[1]
        assert template_call[0][0] == "custom_snapshot"

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_execute_template_creation_failure_raises(self, _mock_rebuild_deps):
        """A non-zero exit from create_db during template step raises CalledProcessError."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        create_returns = iter(
            [(0, "", ""), (1, "", "createdb: error: template clone failed")]
        )

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch("fraisier.strategies._core.create_db", side_effect=create_returns),
            pytest.raises(subprocess.CalledProcessError),
        ):
            strategy = RebuildStrategy(
                create_template=True,
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))


class TestRebuildStrategyVersionStamp:
    """RebuildStrategy stamps the source DB before cloning the template (#198)."""

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_stamps_with_explicit_app_version(self, _mock_rebuild_deps):
        """Explicit app_version is written into public.tb_version via run_psql."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch("fraisier.strategies._core.create_db", return_value=(0, "", "")),
            patch(
                "fraisier.strategies._core.run_psql",
                return_value=(0, "UPDATE 1", ""),
            ) as mock_run_psql,
        ):
            strategy = RebuildStrategy(
                create_template=True,
                app_version="1.2.3",
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        stamp_calls = [
            c
            for c in mock_run_psql.call_args_list
            if "UPDATE public.tb_version" in c.args[0]
        ]
        assert len(stamp_calls) == 1
        call = stamp_calls[0]
        assert "SET app_version = '1.2.3'" in call.args[0]
        assert call.kwargs["db_name"] == "myapp"

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_stamps_source_db_not_template_db(self, _mock_rebuild_deps):
        """The stamp targets the source DB, not the template DB (pre-clone)."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch("fraisier.strategies._core.create_db", return_value=(0, "", "")),
            patch(
                "fraisier.strategies._core.run_psql",
                return_value=(0, "UPDATE 1", ""),
            ) as mock_run_psql,
        ):
            strategy = RebuildStrategy(
                create_template=True,
                app_version="1.2.3",
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        stamp_calls = [
            c
            for c in mock_run_psql.call_args_list
            if "UPDATE public.tb_version" in c.args[0]
        ]
        assert all(c.kwargs["db_name"] == "myapp" for c in stamp_calls)
        assert not any(
            c.kwargs["db_name"] == "template_myapp" for c in stamp_calls
        )

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_race_free_ordering(self, _mock_rebuild_deps):
        """terminate(template) → drop(template) → terminate(src) → stamp → terminate(src) → create(template)."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        sequence: list[tuple[str, str]] = []

        def _record_term(db, **_kw):
            sequence.append(("terminate", db))

        def _record_drop(db, **_kw):
            sequence.append(("drop", db))

        def _record_create(db, **kw):
            template = kw.get("template")
            sequence.append(("create", db if template is None else f"{db}<-{template}"))
            return (0, "", "")

        def _record_psql(sql, **kw):
            if "UPDATE public.tb_version" in sql:
                sequence.append(("stamp", kw["db_name"]))
            return (0, "UPDATE 1", "")

        with (
            patch(
                "fraisier.strategies._core.terminate_backends",
                side_effect=_record_term,
            ),
            patch("fraisier.strategies._core.drop_db", side_effect=_record_drop),
            patch(
                "fraisier.strategies._core.create_db", side_effect=_record_create
            ),
            patch(
                "fraisier.strategies._core.run_psql", side_effect=_record_psql
            ),
        ):
            strategy = RebuildStrategy(
                create_template=True,
                app_version="1.2.3",
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        # Filter to just the template-block operations (after the initial rebuild).
        # The initial DB rebuild produces: terminate(myapp), drop(myapp), create(myapp).
        # Then the template block runs:
        #   terminate(template_myapp), drop(template_myapp),
        #   terminate(myapp), stamp(myapp), terminate(myapp),
        #   create(template_myapp<-myapp)
        # Trim the leading 3 entries from the initial rebuild.
        template_block = sequence[3:]
        assert template_block == [
            ("terminate", "template_myapp"),
            ("drop", "template_myapp"),
            ("terminate", "myapp"),
            ("stamp", "myapp"),
            ("terminate", "myapp"),
            ("create", "template_myapp<-myapp"),
        ]

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_stamp_uses_env_database_url(self, _mock_rebuild_deps):
        """The stamp uses env.database_url (app role), not admin_url."""
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch("fraisier.strategies._core.create_db", return_value=(0, "", "")),
            patch(
                "fraisier.strategies._core.run_psql",
                return_value=(0, "UPDATE 1", ""),
            ) as mock_run_psql,
        ):
            strategy = RebuildStrategy(
                create_template=True,
                app_version="1.2.3",
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        stamp_calls = [
            c
            for c in mock_run_psql.call_args_list
            if "UPDATE public.tb_version" in c.args[0]
        ]
        assert len(stamp_calls) == 1
        assert (
            stamp_calls[0].kwargs["connection_url"]
            == "postgresql://appuser@localhost/myapp"
        )

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_discovers_version_from_version_json(
        self, _mock_rebuild_deps, tmp_path, caplog
    ):
        """With version.json discovery, the UPDATE carries the value and INFO logs the source."""
        import logging

        # The shared fixture globally patches ``Path.read_text`` to return a
        # YAML config blob, which breaks real version.json reads here. Mock
        # ``resolve_app_version`` directly to pin the wiring: constructor
        # forwards project_dir, the resolver result drives the UPDATE and log.
        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch("fraisier.strategies._core.create_db", return_value=(0, "", "")),
            patch(
                "fraisier.strategies._core.run_psql",
                return_value=(0, "UPDATE 1", ""),
            ) as mock_run_psql,
            patch(
                "fraisier.versioning.resolve_app_version",
                return_value=("0.4.2", "version.json"),
            ) as mock_resolve,
            caplog.at_level(logging.INFO, logger="fraisier.strategies._core"),
        ):
            strategy = RebuildStrategy(
                create_template=True,
                project_dir=tmp_path,
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        mock_resolve.assert_called_once_with(tmp_path, override=None)
        stamp_calls = [
            c
            for c in mock_run_psql.call_args_list
            if "UPDATE public.tb_version" in c.args[0]
        ]
        assert len(stamp_calls) == 1
        assert "SET app_version = '0.4.2'" in stamp_calls[0].args[0]
        assert "from version.json" in caplog.text

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_discovers_version_from_pyproject(
        self, _mock_rebuild_deps, tmp_path, caplog
    ):
        """pyproject.toml fallback drives the UPDATE and INFO log."""
        import logging

        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch("fraisier.strategies._core.create_db", return_value=(0, "", "")),
            patch(
                "fraisier.strategies._core.run_psql",
                return_value=(0, "UPDATE 1", ""),
            ) as mock_run_psql,
            patch(
                "fraisier.versioning.resolve_app_version",
                return_value=("0.0.7", "pyproject.toml"),
            ),
            caplog.at_level(logging.INFO, logger="fraisier.strategies._core"),
        ):
            strategy = RebuildStrategy(
                create_template=True,
                project_dir=tmp_path,
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        stamp_calls = [
            c
            for c in mock_run_psql.call_args_list
            if "UPDATE public.tb_version" in c.args[0]
        ]
        assert len(stamp_calls) == 1
        assert "SET app_version = '0.0.7'" in stamp_calls[0].args[0]
        assert "from pyproject.toml" in caplog.text

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_no_version_resolvable_warns_and_clones(
        self, _mock_rebuild_deps, caplog
    ):
        """When no version resolves, warn and continue with the clone."""
        import logging

        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch(
                "fraisier.strategies._core.create_db", return_value=(0, "", "")
            ) as mock_create,
            patch(
                "fraisier.strategies._core.run_psql",
                return_value=(0, "UPDATE 1", ""),
            ) as mock_run_psql,
            patch(
                "fraisier.versioning.resolve_app_version", return_value=None
            ),
            caplog.at_level(logging.WARNING, logger="fraisier.strategies._core"),
        ):
            strategy = RebuildStrategy(
                create_template=True,
                admin_url="postgresql://postgres@localhost/postgres",
            )
            result = strategy.execute(Path("confiture.yaml"))

        assert result.success
        stamp_calls = [
            c
            for c in mock_run_psql.call_args_list
            if "UPDATE public.tb_version" in c.args[0]
        ]
        assert stamp_calls == []
        # Template clone still happens: create_db called twice (db + template).
        assert mock_create.call_count == 2
        template_call = mock_create.call_args_list[1]
        assert template_call[0][0] == "template_myapp"
        assert template_call[1]["template"] == "myapp"
        assert "Skipping pre-clone version stamp" in caplog.text

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_psql_failure_warns_and_clones(self, _mock_rebuild_deps, caplog):
        """psql non-zero exit logs WARNING with stderr and continues with clone."""
        import logging

        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch(
                "fraisier.strategies._core.create_db", return_value=(0, "", "")
            ) as mock_create,
            patch(
                "fraisier.strategies._core.run_psql",
                return_value=(1, "", "relation tb_version does not exist"),
            ),
            caplog.at_level(logging.WARNING, logger="fraisier.strategies._core"),
        ):
            strategy = RebuildStrategy(
                create_template=True,
                app_version="1.2.3",
                admin_url="postgresql://postgres@localhost/postgres",
            )
            result = strategy.execute(Path("confiture.yaml"))

        assert result.success
        assert mock_create.call_count == 2
        assert "Could not stamp" in caplog.text
        assert "relation tb_version does not exist" in caplog.text

    @pytest.mark.usefixtures("_mock_rebuild_deps")
    def test_empty_tb_version_warns_and_clones(self, _mock_rebuild_deps, caplog):
        """UPDATE 0 against an empty tb_version warns and skips the INFO success log."""
        import logging

        mocks = _mock_rebuild_deps
        builder_instance = mocks["builder_cls"].return_value
        builder_instance.build_split.return_value = FakeSplitResult()

        with (
            patch("fraisier.strategies._core.terminate_backends"),
            patch("fraisier.strategies._core.drop_db"),
            patch(
                "fraisier.strategies._core.create_db", return_value=(0, "", "")
            ) as mock_create,
            patch(
                "fraisier.strategies._core.run_psql",
                return_value=(0, "UPDATE 0", ""),
            ),
            caplog.at_level(logging.DEBUG, logger="fraisier.strategies._core"),
        ):
            strategy = RebuildStrategy(
                create_template=True,
                app_version="1.2.3",
                admin_url="postgresql://postgres@localhost/postgres",
            )
            strategy.execute(Path("confiture.yaml"))

        assert mock_create.call_count == 2
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "tb_version" in r.message
        ]
        assert len(warning_records) == 1
        # No INFO "Stamped" log for this run.
        stamped_info = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO
            and r.message.startswith("Stamped ")
        ]
        assert stamped_info == []
