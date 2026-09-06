"""The integration harness must find the server CI actually provides (#358).

Nine tests proving a truncated dump fails the restore, and nine proving the
actuation receipt round-trips through real SQL, reported green for a release
without ever running: the harness discovered PostgreSQL over a unix socket
only, and every CI workflow provides it as a service container over TCP with no
shared socket. The fixture skipped, pytest counted a skip as not-a-failure, and
the checkmark said nothing about the behaviour being shipped.

These are unit tests over the discovery itself — they need no database — so the
harness's own reachability logic is pinned by tests that always run.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from _pytest.outcomes import Failed, Skipped

from tests.integration import conftest
from tests.integration.conftest import PgTarget, _discover_target, unavailable
from tests.test_preflight_e2e import _get_admin_url

_WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

#: A pytest invocation that is not a ``--version`` probe. Line continuations are
#: folded first, so a multi-line ``uv run pytest tests/ \`` reads as one command.
_PYTEST_RUN = re.compile(r"(?<![\w-])pytest(?![\w-])")


def _runs_the_suite(run: str) -> bool:
    """Does this workflow step's ``run:`` script execute the test suite?"""
    folded = run.replace("\\\n", " ")
    return any(
        _PYTEST_RUN.search(line) and "--version" not in line
        for line in folded.splitlines()
    )


class TestPgTargetDsn:
    """One target, two transports, one DSN builder."""

    def test_a_socket_target_addresses_the_directory(self):
        target = PgTarget(host="/run/postgresql")

        assert target.over_socket is True
        assert (
            target.dsn("fraisier_it")
            == "postgresql:///fraisier_it?host=/run/postgresql"
        )

    def test_a_tcp_target_carries_host_port_and_credentials(self):
        """CI's service container, exactly as the workflows declare it."""
        target = PgTarget(
            host="localhost", port=5432, user="fraisier", password="fraisier"
        )

        assert target.over_socket is False
        assert (
            target.dsn("fraisier_it")
            == "postgresql://fraisier:fraisier@localhost:5432/fraisier_it"
        )

    def test_credentials_are_percent_encoded(self):
        """A password is data, not URL syntax."""
        target = PgTarget(
            host="db.internal", port=5432, user="a b", password="p@ss/w:d"
        )

        assert target.dsn("x") == "postgresql://a%20b:p%40ss%2Fw%3Ad@db.internal:5432/x"

    def test_a_tcp_target_without_credentials_omits_the_userinfo(self):
        target = PgTarget(host="localhost", port=5432)

        assert target.dsn("x") == "postgresql://localhost:5432/x"


class TestDiscovery:
    """Which server the fixtures hand the tests."""

    def test_the_ci_service_container_is_discovered(self, monkeypatch):
        """FRAISIER_TEST_PG_URL is a candidate, so CI's TCP server is found.

        This is the whole bug: without it ``_discover_target`` returns None on
        every CI runner and the tests carrying #358's guarantee skip.
        """
        monkeypatch.setenv(
            "FRAISIER_TEST_PG_URL",
            "postgresql://fraisier:fraisier@localhost:5432/fraisier_test",
        )
        monkeypatch.setattr(
            "tests.integration.conftest._probe", lambda _dsn: "/var/run/postgresql"
        )
        monkeypatch.setattr(
            "tests.integration.conftest.shutil.which", lambda tool: tool
        )

        target = _discover_target()

        assert target is not None
        assert target == PgTarget(
            host="localhost", port=5432, user="fraisier", password="fraisier"
        )
        # Addressed over TCP even though the probe reported a socket directory:
        # the server's own socket is inside the service container and is not
        # shared with the runner.
        assert target.dsn("postgres").startswith("postgresql://fraisier:")

    def test_an_unusable_env_url_falls_back_to_the_local_socket(self, monkeypatch):
        """A stale FRAISIER_TEST_PG_URL must not hide a working local server."""
        monkeypatch.setenv("FRAISIER_TEST_PG_URL", "postgresql://nobody@127.0.0.1:1/db")
        monkeypatch.setattr(
            "tests.integration.conftest.shutil.which", lambda tool: tool
        )
        monkeypatch.setattr(
            "tests.integration.conftest._probe",
            lambda dsn: None if "127.0.0.1" in dsn else "/run/postgresql",
        )

        assert _discover_target() == PgTarget(host="/run/postgresql")

    def test_no_env_url_still_discovers_a_socket(self, monkeypatch):
        """The developer's path is unchanged."""
        monkeypatch.delenv("FRAISIER_TEST_PG_URL", raising=False)
        monkeypatch.setattr(
            "tests.integration.conftest.shutil.which", lambda tool: tool
        )
        monkeypatch.setattr(
            "tests.integration.conftest._probe", lambda _dsn: "/run/postgresql"
        )

        assert _discover_target() == PgTarget(host="/run/postgresql")

    def test_missing_client_tools_are_not_a_reachable_server(self, monkeypatch):
        monkeypatch.setenv("FRAISIER_TEST_PG_URL", "postgresql://f:f@localhost:5432/d")
        monkeypatch.setattr(
            "tests.integration.conftest.shutil.which", lambda _tool: None
        )

        assert _discover_target() is None

    @pytest.mark.parametrize("url", ["", "not-a-url", "postgresql:///db"])
    def test_an_env_url_naming_no_host_is_no_candidate(self, url, monkeypatch):
        """Only a URL with a host describes a server reachable over TCP."""
        monkeypatch.setenv("FRAISIER_TEST_PG_URL", url)
        monkeypatch.setattr(
            "tests.integration.conftest.shutil.which", lambda tool: tool
        )
        monkeypatch.setattr(
            "tests.integration.conftest._probe",
            lambda dsn: (
                "/run/postgresql" if "host=" in dsn or dsn.count("/") == 3 else None
            ),
        )

        assert _discover_target() == PgTarget(host="/run/postgresql")


class TestTheHarnessIsTheOnlyDoor:
    """An integration module gets its server from ``pg_target``, never itself.

    ``test_confiture_integration.py`` read ``FRAISIER_TEST_PG_URL`` directly and
    paid for it twice: it *skipped* silently when the variable was unset, the
    #358 failure mode this file exists to prevent, and its setup dropped every
    table in ``public`` of whatever the variable named — so pointing it at a
    real database emptied that database (#386). The harness knows about
    discovery, about ``unavailable()``, and about giving a test a database of
    its own; a module that goes around it knows none of that.
    """

    @staticmethod
    def _integration_modules() -> list[Path]:
        directory = Path(__file__).resolve().parent / "integration"
        return sorted(
            path
            for path in directory.glob("*.py")
            if path.name not in {"conftest.py", "__init__.py"}
        )

    def test_there_are_modules_to_check(self):
        assert self._integration_modules()

    @staticmethod
    def _names_the_variable_in_code(path: Path) -> bool:
        """Is the variable *used* here, rather than merely written about?

        An exact string constant is a lookup; a docstring that explains the
        history contains it as a substring of something longer, and is not.
        """
        return any(
            isinstance(node, ast.Constant) and node.value == "FRAISIER_TEST_PG_URL"
            for node in ast.walk(ast.parse(path.read_text()))
        )

    def test_no_integration_module_reads_the_env_url_itself(self):
        offenders = [
            path.name
            for path in self._integration_modules()
            if self._names_the_variable_in_code(path)
        ]
        assert offenders == [], (
            f"{offenders} read FRAISIER_TEST_PG_URL directly; request the "
            "`pg_target` fixture instead (#386)"
        )


class TestTheContainerFallback:
    """A container is the last resort, and it never decides "no server".

    It exists for a machine with the client tools but no server. Two rules keep
    it from doing harm: it yields ``None`` rather than skipping — a skip decided
    here would be #370 again, silently — and its image follows the host
    client's major version, because an 18 ``pg_dump`` against a 16 server emits
    ``SET transaction_timeout`` and the restore fails for a reason that has
    nothing to do with the code.
    """

    def test_the_client_major_version_is_read_from_pg_dump(self, monkeypatch):
        monkeypatch.setattr(
            conftest.subprocess,
            "run",
            lambda *_a, **_kw: SimpleNamespace(stdout="pg_dump (PostgreSQL) 18.4\n"),
        )
        assert conftest.client_major_version() == 18

    def test_an_unreadable_client_version_is_no_version(self, monkeypatch):
        monkeypatch.setattr(
            conftest.subprocess,
            "run",
            lambda *_a, **_kw: SimpleNamespace(stdout="who knows\n"),
        )
        assert conftest.client_major_version() is None

    def test_the_image_follows_the_client_major_version(self, monkeypatch):
        monkeypatch.setattr(conftest, "_client_tools_present", lambda: True)
        monkeypatch.setattr(conftest, "client_major_version", lambda: 18)
        started: list[str] = []

        class _FakeContainer:
            def __init__(self, image, **_kwargs):
                started.append(image)

            def start(self):
                return self

            def stop(self):
                return None

            def get_connection_url(self):
                return "postgresql://u:p@127.0.0.1:55432/test"

        postgres = pytest.importorskip("testcontainers.postgres")
        monkeypatch.setattr(postgres, "PostgresContainer", _FakeContainer)
        monkeypatch.setattr(conftest, "_probe", lambda _dsn: "/var/run/postgresql")

        with conftest._container_target() as target:
            assert target is not None
            assert target.host == "127.0.0.1"
            assert target.port == 55432

        assert started == ["postgres:18"], (
            "the image must match the host client's major version, or "
            "pg_dump 18 against a 16 server fails on SET transaction_timeout"
        )

    def test_a_container_that_does_not_probe_clean_is_not_used(self, monkeypatch):
        """ "A server with createdb" means the same however it was found."""
        monkeypatch.setattr(conftest, "_client_tools_present", lambda: True)
        monkeypatch.setattr(conftest, "client_major_version", lambda: 18)
        stopped: list[bool] = []

        class _FakeContainer:
            def __init__(self, _image, **_kwargs):
                pass

            def start(self):
                return self

            def stop(self):
                stopped.append(True)

            def get_connection_url(self):
                return "postgresql://u:p@127.0.0.1:55432/test"

        postgres = pytest.importorskip("testcontainers.postgres")
        monkeypatch.setattr(postgres, "PostgresContainer", _FakeContainer)
        monkeypatch.setattr(conftest, "_probe", lambda _dsn: None)

        with conftest._container_target() as target:
            assert target is None
        assert stopped == [True], "a container that is not used is still stopped"

    def test_no_client_tools_means_no_container(self, monkeypatch):
        """A container cannot substitute for the host's pg_dump."""
        monkeypatch.setattr(conftest, "_client_tools_present", lambda: False)
        with conftest._container_target() as target:
            assert target is None

    def test_a_container_that_will_not_start_is_not_a_failure_here(self, monkeypatch):
        monkeypatch.setattr(conftest, "_client_tools_present", lambda: True)
        monkeypatch.setattr(conftest, "client_major_version", lambda: 18)

        def _explode(*_a, **_kw):
            raise RuntimeError("Cannot connect to the Docker daemon")

        postgres = pytest.importorskip("testcontainers.postgres")
        monkeypatch.setattr(postgres, "PostgresContainer", _explode)

        with conftest._container_target() as target:
            assert target is None, (
                "deciding 'no server' belongs to pg_target, which knows about "
                "FRAISIER_INTEGRATION; a skip decided here would be silent"
            )

    def test_discovery_itself_never_starts_a_container(self, monkeypatch):
        """A running server always wins, and the unit tests stay database-free."""
        monkeypatch.setattr(
            conftest, "_container_target", _never_called("_container_target")
        )
        conftest._discover_target()


def _never_called(name: str):
    def _fail(*_a, **_kw):
        raise AssertionError(f"{name} was called")

    return _fail


class TestPreflightAdminUrl:
    """Which server the preflight e2e module creates its databases on (#370).

    That module predates the shared harness and resolved its own admin URL from
    ``FRAISIER_TEST_ADMIN_URL``, defaulting to a passwordless
    ``postgres@localhost``. No workflow set the variable and no CI postgres has
    ever accepted the default, so all ten of its tests skipped on every run
    since they were written. Resolution now goes through the same discovery as
    every other integration module, which reads the ``FRAISIER_TEST_PG_URL``
    the workflows already set.
    """

    def test_the_ci_service_container_supplies_the_admin_url(self, monkeypatch):
        """CI's shape: no admin variable, only FRAISIER_TEST_PG_URL."""
        monkeypatch.delenv("FRAISIER_TEST_ADMIN_URL", raising=False)
        monkeypatch.setattr(
            "tests.test_preflight_e2e._discover_target",
            lambda: PgTarget(
                host="localhost", port=5432, user="fraisier", password="fraisier"
            ),
        )

        assert (
            _get_admin_url() == "postgresql://fraisier:fraisier@localhost:5432/postgres"
        )

    def test_the_maintenance_database_is_the_one_addressed(self, monkeypatch):
        """These tests CREATE and DROP databases, so they connect to ``postgres``.

        Never to the ``fraisier_test`` database named by ``FRAISIER_TEST_PG_URL``
        — dropping the database you are connected to is not a thing.
        """
        monkeypatch.delenv("FRAISIER_TEST_ADMIN_URL", raising=False)
        monkeypatch.setattr(
            "tests.test_preflight_e2e._discover_target",
            lambda: PgTarget(host="/run/postgresql"),
        )

        assert _get_admin_url() == "postgresql:///postgres?host=/run/postgresql"

    def test_an_explicit_admin_url_still_wins(self, monkeypatch):
        """The variable keeps working for anyone who set it."""
        monkeypatch.setenv("FRAISIER_TEST_ADMIN_URL", "postgresql://me@db/postgres")
        monkeypatch.setattr(
            "tests.test_preflight_e2e._discover_target",
            lambda: PgTarget(host="/run/postgresql"),
        )

        assert _get_admin_url() == "postgresql://me@db/postgres"

    def test_no_server_anywhere_resolves_to_nothing(self, monkeypatch):
        monkeypatch.delenv("FRAISIER_TEST_ADMIN_URL", raising=False)
        monkeypatch.setattr("tests.test_preflight_e2e._discover_target", lambda: None)

        assert _get_admin_url() is None


class TestIntegrationRequired:
    """``FRAISIER_INTEGRATION=1`` says a database is supposed to be here.

    A skip then does not mean "inapplicable"; it means the harness failed to
    find what the caller provided, and it has to be loud. #370 is what a quiet
    one costs: ten tests reporting green on every release, including the ones
    that published to PyPI.
    """

    def test_a_required_run_fails_instead_of_skipping(self, monkeypatch):
        monkeypatch.setenv("FRAISIER_INTEGRATION", "1")

        with pytest.raises(Failed, match="no database"):
            unavailable("no database")

    def test_an_optional_run_still_skips(self, monkeypatch):
        monkeypatch.delenv("FRAISIER_INTEGRATION", raising=False)

        with pytest.raises(Skipped, match="no database"):
            unavailable("no database")

    def test_any_other_value_is_not_a_declaration(self, monkeypatch):
        """Only ``1`` arms it — an inherited ``FRAISIER_INTEGRATION=0`` must not."""
        monkeypatch.setenv("FRAISIER_INTEGRATION", "0")

        with pytest.raises(Skipped):
            unavailable("no database")


class TestWorkflowsCarryTheHarnessEnv:
    """Every workflow that runs the suite must give it the server it needs.

    Nothing tied the suite's requirements to the workflows, so a variable the
    tests read could go unset in all three at once and no check noticed (#370).
    This is that tie.
    """

    #: What a pytest step has to put in the environment for the integration
    #: tests to actually execute rather than skip themselves away.
    REQUIRED = ("FRAISIER_TEST_PG_URL", "FRAISIER_INTEGRATION")

    @staticmethod
    def _test_steps() -> list[tuple[str, str, dict]]:
        """Every workflow step that runs the test suite, as (file, job, step)."""
        import yaml

        found = []
        for path in sorted(_WORKFLOW_DIR.glob("*.yml")):
            workflow = yaml.safe_load(path.read_text())
            found.extend(
                (path.name, job_name, step)
                for job_name, job in (workflow.get("jobs") or {}).items()
                for step in job.get("steps") or []
                if _runs_the_suite(step.get("run") or "")
            )
        return found

    def test_the_workflows_are_found_at_all(self):
        """A glob that matched nothing would pass every assertion below."""
        steps = self._test_steps()

        assert {name for name, _, _ in steps} == {
            "publish.yml",
            "python-version-matrix.yml",
            "quality-gate.yml",
        }

    def test_every_pytest_step_declares_the_harness_env(self):
        missing = {
            f"{name}:{job}": [
                v for v in self.REQUIRED if v not in (step.get("env") or {})
            ]
            for name, job, step in self._test_steps()
            if any(v not in (step.get("env") or {}) for v in self.REQUIRED)
        }

        assert not missing, f"workflow test steps missing harness env: {missing}"

    def test_integration_is_declared_on_and_not_merely_present(self):
        """Only ``1`` arms the suite; ``FRAISIER_INTEGRATION: 0`` skips it away.

        Asserting the key exists would let the variable be set to a value that
        restores exactly the silence #370 is about.
        """
        wrong = {
            f"{name}:{job}": (step.get("env") or {}).get("FRAISIER_INTEGRATION")
            for name, job, step in self._test_steps()
            if str((step.get("env") or {}).get("FRAISIER_INTEGRATION")) != "1"
        }

        assert not wrong, f"workflow test steps not declaring integration: {wrong}"

    def test_a_version_probe_is_not_a_test_run(self):
        """``uv run pytest --version`` checks the install; it runs no tests."""
        assert _runs_the_suite("uv run pytest --version") is False
        assert _runs_the_suite("uv run pytest tests/ -v") is True
        assert (
            _runs_the_suite("uv run pytest \\\n  tests/ \\\n  --cov=fraisier") is True
        )
        assert _runs_the_suite("echo no tests here") is False
