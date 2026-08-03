"""Shared test fixtures and configuration."""

import asyncio
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.config import FraisierConfig, reset_config
from fraisier.database import FraisierDB
from fraisier.dbops._url import replace_db_name
from tests.fixtures.git_env import git_deploy_env as git_deploy_env  # noqa: PLC0414


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Clear rate limiter state between tests."""
    from fraisier.webhook_rate_limit import reset

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    """Keep deployment locks out of /run/fraisier.

    ``file_deployment_lock`` mkdirs its lock directory, which needs root for the
    real ``/run/fraisier`` and does not exist at all on most dev machines. Any
    code path that takes a deployment lock — the webhook, and ``db restore``
    since #310 — would otherwise fail in tests for purely environmental reasons.

    Redirected at the single choke point rather than at each source of the path:
    the directory arrives variously from ``DEFAULT_LOCK_DIR``, the
    ``DeploymentConfig`` schema default, and a hardcoded literal in the config
    loader. Only a ``/run/`` path is rewritten, so a directory a test supplies
    itself is honoured and ``test_file_lock.py`` keeps controlling its own.
    """
    from fraisier import locking

    real = locking.file_deployment_lock
    safe = tmp_path / ".locks"

    def _redirected(fraise_name, lock_dir=None):
        if lock_dir is None or str(lock_dir).startswith("/run/"):
            lock_dir = safe
        return real(fraise_name, lock_dir=lock_dir)

    monkeypatch.setattr(locking, "file_deployment_lock", _redirected)
    monkeypatch.setattr(locking, "DEFAULT_LOCK_DIR", safe)


@pytest.fixture(autouse=True)
def _reset_delivery_dedupe():
    """Clear webhook delivery-ID dedupe store between tests."""
    from fraisier.git.github import _delivery_dedupe

    _delivery_dedupe._store.clear()
    yield
    _delivery_dedupe._store.clear()


@pytest.fixture(autouse=True)
def _fast_strategy_time(monkeypatch, request):
    """Make asyncio.sleep advance time instantly for deployment strategy tests."""
    # Only apply to test files that test deployment strategies
    test_module = request.node.module.__name__
    strategy_modules = {
        "tests.test_e2e_deployments",
    }
    if test_module not in strategy_modules:
        return

    import time as time_module

    _time_offset = [0.0]
    _real_time = time_module.time

    def fast_time():
        return _real_time() + _time_offset[0]

    original_sleep = asyncio.sleep

    async def fast_sleep(delay, result=None):
        _time_offset[0] += delay
        await original_sleep(0)
        return result

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(time_module, "time", fast_time)


@pytest.fixture
def test_db() -> FraisierDB:
    """Create test database with trinity schema.

    Initializes empty database with trinity pattern tables:
    - tb_fraise_state (pk_fraise_state, id UUID, identifier business key)
    - tb_deployment (pk_deployment, id UUID, identifier, fk_fraise_state)
    - tb_webhook_event (pk_webhook_event, id UUID, fk_deployment)

    Uses the isolated DB path provided by _isolated_db autouse fixture.
    """
    import fraisier.database

    db = FraisierDB()
    fraisier.database._db = db
    return db


@pytest.fixture
def sample_config(tmp_path: Path) -> FraisierConfig:
    """Create sample fraises.yaml configuration."""
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  my_api:
    type: api
    description: Test API service
    environments:
      development:
        app_path: /tmp/test-api-dev
        systemd_service: test-api-dev.service
        health_check:
          url: http://localhost:8000/health
          timeout: 10
      production:
        app_path: /tmp/test-api-prod
        systemd_service: test-api-prod.service
        git_repo: https://github.com/test/api.git
        health_check:
          url: https://api.example.com/health
          timeout: 30
        database:
          tool: alembic
          strategy: apply

  data_pipeline:
    type: etl
    description: Data ETL pipeline
    environments:
      production:
        app_path: /var/etl
        script_path: scripts/pipeline.py
        database:
          tool: alembic
          strategy: apply

  backup_job:
    type: scheduled
    description: Hourly backup
    environments:
      production:
        systemd_service: backup.service
        systemd_timer: backup.timer
        script_path: /usr/local/bin/backup.sh
"""
    )
    return FraisierConfig(str(config_file))


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run for testing."""
    with patch("subprocess.run") as mock:
        mock.return_value = MagicMock(
            returncode=0,
            stdout="test output\n",
            stderr="",
        )
        yield mock


@pytest.fixture
def mock_requests():
    """Mock urllib health checks used by HTTPHealthChecker."""
    with patch("urllib.request.urlopen") as mock:
        response = MagicMock()
        response.status = 200
        mock.return_value = response
        yield mock


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    """Reset global config singleton between tests."""
    import fraisier.config

    old = fraisier.config._config
    yield
    fraisier.config._config = old
    reset_config()


@pytest.fixture(autouse=True)
def _unpoison_cli_get_config():
    """Undo a mock leaked into ``fraisier.cli.main`` by import-under-patch.

    ``fraisier/cli/main.py`` binds ``get_config`` with a ``from`` import at
    module scope. A test that does::

        with patch("fraisier.config.get_config", return_value=cfg):
            from fraisier.cli.main import main

    binds the *mock* into ``fraisier.cli.main`` if that import is the first
    one in the session — and ``patch`` cannot undo it, because the name it
    restores lives in ``fraisier.config``. Every later CLI test then runs
    against whichever config that first test happened to build, ignoring even
    an explicit ``-c``.

    That made CLI results depend on collection order, invisibly: until #331
    an unresolvable host provisioned everything, so a test reading the wrong
    config still exited 0 and still passed.
    """
    yield

    import sys

    from fraisier.config import get_config as real_get_config

    # Via sys.modules, not ``import fraisier.cli.main``: the package's
    # ``from .main import main`` shadows the submodule with the click Group.
    cli_main = sys.modules.get("fraisier.cli.main")
    if cli_main is not None and getattr(cli_main, "get_config", None) is not (
        real_get_config
    ):
        cli_main.get_config = real_get_config  # ty: ignore[unresolved-attribute]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Ensure every test gets a fresh, isolated SQLite database.

    Patches get_db_path() so that any code path (get_db(), get_connection(),
    FraisierDB()) uses a per-test temp directory.  Also resets the global _db
    singleton so no state leaks between tests.
    """
    import fraisier.database

    db_path = tmp_path / "test_fraisier.db"
    monkeypatch.setattr(fraisier.database, "get_db_path", lambda: db_path)

    old_db = fraisier.database._db
    fraisier.database._db = None
    yield
    fraisier.database._db = old_db


# ---------------------------------------------------------------------------
# Integration test fixtures (testcontainers + PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_container():
    """Start a PostgreSQL 16 container for the test session.

    Skips gracefully when testcontainers or Docker is unavailable.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed")  # ty: ignore[too-many-positional-arguments]

    try:
        container = PostgresContainer("postgres:16", driver=None)
        container.start()
    except Exception as exc:
        pytest.skip(f"Docker unavailable: {exc.__class__.__name__}: {exc}")  # ty: ignore[too-many-positional-arguments]

    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def pg_superuser_url(pg_container):
    """Superuser connection URL for the session container."""
    return pg_container.get_connection_url()


@pytest.fixture
def pg_test_db(pg_superuser_url):
    """Create a fresh database per test, drop on teardown."""
    import psycopg

    db_name = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {db_name}")  # ty: ignore[no-matching-overload]

    test_url = replace_db_name(pg_superuser_url, db_name)
    yield test_url, db_name

    with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
        # Terminate any lingering connections before dropping
        conn.execute(  # ty: ignore[no-matching-overload]
            "SELECT pg_terminate_backend(pid) "
            "FROM pg_stat_activity "
            f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
        )
        conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")  # ty: ignore[no-matching-overload]


@pytest.fixture
def confiture_project(pg_test_db, tmp_path):
    """Set up a confiture project directory with config pointing at the test DB.

    Creates the full directory layout that both confiture's Migrator (reads
    confiture.yaml) and SchemaBuilder (reads db/environments/<name>.yaml)
    expect.
    """
    import textwrap

    test_url, db_name = pg_test_db

    # Write confiture.yaml (used by Migrator / strategy execute)
    config_path = tmp_path / "confiture.yaml"
    config_path.write_text(
        textwrap.dedent(f"""\
        name: test
        database_url: "{test_url}"
        include_dirs:
          - "{tmp_path / "db" / "0_schema"}"
        """)
    )

    # Write db/environments/test.yaml (used by SchemaBuilder.load)
    env_dir = tmp_path / "db" / "environments"
    env_dir.mkdir(parents=True)
    (env_dir / "test.yaml").write_text(
        textwrap.dedent(f"""\
        name: test
        database_url: "{test_url}"
        include_dirs:
          - "{tmp_path / "db" / "0_schema"}"
        """)
    )

    # Create migration directory structure
    migrations_dir = tmp_path / "db" / "migrations"
    migrations_dir.mkdir(parents=True)

    # Create a simple schema directory
    schema_dir = tmp_path / "db" / "0_schema" / "01_public"
    schema_dir.mkdir(parents=True)
    (schema_dir / "011_tb_example.sql").write_text(
        "CREATE TABLE IF NOT EXISTS public.tb_example (\n"
        "    id serial PRIMARY KEY,\n"
        "    name text NOT NULL\n"
        ");\n"
    )

    # Write a migration pair
    (migrations_dir / "001_initial.up.sql").write_text(
        "CREATE TABLE IF NOT EXISTS public.tb_example (\n"
        "    id serial PRIMARY KEY,\n"
        "    name text NOT NULL\n"
        ");\n"
    )
    (migrations_dir / "001_initial.down.sql").write_text(
        "DROP TABLE IF EXISTS public.tb_example;\n"
    )

    return config_path, migrations_dir, test_url, db_name


@pytest.fixture
def fraisier_config_fixture(tmp_path: Path) -> FraisierConfig:
    """Create a FraisierConfig fixture for testing manifest creation."""
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(
        """
scaffold:
  output_dir: scripts/generated
  deploy_user: fraisier
  config_path: /opt/fraisier/fraises.yaml

fraises:
  my_api:
    type: api
    description: Test API service
    environments:
      development:
        app_path: /var/www/my_api/dev
        git_repo: /var/repos/my_api.git
        systemd_service: my_api-dev.service
      production:
        app_path: /var/www/my_api/prod
        git_repo: /var/repos/my_api.git
        systemd_service: my_api-prod.service
"""
    )
    return FraisierConfig(str(config_file))


@pytest.fixture
def fraisier_config_with_install_user_fixture(tmp_path: Path) -> FraisierConfig:
    """Create a FraisierConfig with install.user different from deploy_user."""
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(
        """
scaffold:
  output_dir: scripts/generated
  deploy_user: fraisier
  config_path: /opt/fraisier/fraises.yaml

fraises:
  my_api:
    type: api
    description: Test API service
    install:
      user: appuser
    environments:
      production:
        app_path: /var/www/my_api/prod
        git_repo: /var/repos/my_api.git
        systemd_service: my_api-prod.service
"""
    )
    return FraisierConfig(str(config_file))
