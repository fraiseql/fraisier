"""A project learns its names need quotes before a deploy is refused.

confiture's policy (fraiseql/confiture#505) is that an identifier PostgreSQL
cannot write bare is unsupported wherever confiture *compares or generates* a
schema.  That includes the DDL side of live drift — ``migrate validate
--check-live-drift`` — which is exactly what ``post_migrate_check`` runs.  The
refusal is ``DIFFER_403``, exit 5, and it is unconditional: there is no opt-out
flag, and a ``lint --baseline`` that absorbs ``naming_003``/``naming_004``
does not absorb it.

Measured, not read from a changelog: a database built verbatim from its own DDL
— zero drift by construction — carrying ``"createdAt"`` and ``"userName"`` gates
``passed=True exit=0`` on confiture 1.25.1 and ``passed=False exit=5`` with #505
merged (``.phases/2026-09-28-confiture-next-probe/probe_quoted.py``).

So the deploy that breaks is a *correct* one, and it breaks after the migrations
have been applied.  This check moves that discovery to ``fraisier doctor``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from packaging.version import Version

if TYPE_CHECKING:
    from pathlib import Path

    from fraisier.config import FraisierConfig
    from fraisier.doctor import CheckResult

CHECK = "post_migrate_check_names_conform"

#: A confiture that refuses.  Detection tests pin it so they keep testing *which
#: names are offenders* rather than silently passing the day the gate moves.
REFUSING = Version("1.26.0")

BARE = "CREATE TABLE app.tb_user (\n    id BIGINT PRIMARY KEY,\n    created_at TIMESTAMPTZ\n);\n"

QUOTED = (
    "CREATE TABLE app.tb_user (\n"
    "    id BIGINT PRIMARY KEY,\n"
    '    "createdAt" TIMESTAMPTZ NOT NULL,\n'
    '    "userName" TEXT NOT NULL\n'
    ");\n"
)


def _cfg(tmp_path: Path, *, checks: str = "[live-drift]") -> FraisierConfig:
    """A project with one enabled live-drift gate over a buildable DDL tree."""
    from fraisier.config import FraisierConfig

    app = tmp_path / "app"
    (app / "db" / "environments").mkdir(parents=True)
    (app / "db" / "schema").mkdir(parents=True)
    (app / "db" / "environments" / "production.yaml").write_text(
        "name: production\ninclude_dirs:\n  - db/schema\n"
    )
    (app / "db" / "schema" / "010_tables.sql").write_text(BARE)

    path = tmp_path / "fraises.yaml"
    path.write_text(f"""
name: myproj
scaffold:
  deploy_user: fraisier
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: {app}
        database:
          name: db
          strategy: migrate
          confiture_config: db/environments/production.yaml
          post_migrate_check:
            enabled: true
            checks: {checks}
""")
    return FraisierConfig(path)


def _run(
    cfg: FraisierConfig,
    schema: str,
    *,
    installed: Version | None = REFUSING,
    returncode: int = 0,
) -> CheckResult:
    """Drive the check with *schema* standing in for what confiture builds.

    The stub sits at the **subprocess** boundary, so the check's own resolution
    of the environment name, its temporary file and its handling of a failed
    build all still run.  Stubbing the check's own reader instead would test the
    stub.
    """
    from fraisier import doctor
    from fraisier.dbops import drift

    def fake_build(
        *, project_dir: Path, env_name: str, output: Path
    ) -> subprocess.CompletedProcess[str]:
        _ = (project_dir, env_name)
        # A build that fails still leaves what it managed to emit.  Writing it
        # on both branches is what makes the failing case test the *exit code*
        # rather than the file's absence.
        output.write_text(schema)
        return subprocess.CompletedProcess(
            args=["confiture", "build"], returncode=returncode, stdout="", stderr=""
        )

    with (
        patch.object(drift, "build_expected_schema", fake_build),
        patch.object(doctor, "_confiture_cli_version", return_value=installed),
    ):
        return doctor.DOCTOR_CHECKS[CHECK].fn(cfg)


class TestTheCheckSeesAQuotedName:
    """Cycle 3.1 — the built schema is scanned, and the predicate is the point."""

    def test_registered(self) -> None:
        from fraisier import doctor

        assert CHECK in doctor.DOCTOR_CHECKS

    def test_a_quoted_camel_case_column_is_reported(self, tmp_path: Path) -> None:
        result = _run(_cfg(tmp_path), QUOTED)
        assert result.status == "warn"
        assert "createdAt" in result.detail

    def test_a_bare_snake_case_schema_is_silent(self, tmp_path: Path) -> None:
        assert _run(_cfg(tmp_path), BARE).status == "pass"

    def test_a_quoted_name_postgres_writes_bare_is_not_an_offender(
        self, tmp_path: Path
    ) -> None:
        """``"users"`` is quoted in the DDL but needs no quotes — confiture takes it.

        The rule is "a name only as PostgreSQL writes it bare", not "a name never
        written with quotes".  Scanning for quotes alone would fail every ORM
        tree that quotes defensively.
        """
        schema = 'CREATE TABLE app."users" (id BIGINT PRIMARY KEY);\n'
        assert _run(_cfg(tmp_path), schema).status == "pass"

    def test_a_reserved_word_is_an_offender(self, tmp_path: Path) -> None:
        """``user`` is lower-case and alphanumeric, and still needs quotes.

        A predicate built only from "has a capital or punctuation" misses this
        whole class.  PostgreSQL 18 reserves 78 words; this is one.
        """
        schema = 'CREATE TABLE app.tb_account (\n    "user" TEXT NOT NULL\n);\n'
        result = _run(_cfg(tmp_path), schema)
        assert result.status == "warn"
        assert "user" in result.detail

    def test_a_dotted_name_is_reported_as_misparsing(self, tmp_path: Path) -> None:
        """naming_003 is a different failure from naming_004 and says so.

        A dot does not merely need quotes: ``"a.b"`` *misreads* as ``a``.``b``,
        so what refers to the name resolves somewhere else.  Both are refused
        identically, but the reason is worth keeping in the message.
        """
        schema = 'CREATE TABLE app."tb.user" (id BIGINT PRIMARY KEY);\n'
        result = _run(_cfg(tmp_path), schema)
        assert result.status == "warn"
        assert "misread" in result.detail.lower()


class TestTheRefusalIsVersionGated:
    """Cycle 3.2 — a project is not told about a refusal it will not meet.

    fraisier's floor admits confitures on both sides of #505, so the check
    reports what *this* project's binary does.  The version comes from the
    ``confiture`` on PATH rather than installed metadata, because the gate
    shells out and the two genuinely differ — on this machine, today, PATH
    carries 1.19.0 while the project venv has 1.25.1.
    """

    def test_a_confiture_that_still_compares_is_a_skip(self, tmp_path: Path) -> None:
        result = _run(_cfg(tmp_path), QUOTED, installed=Version("1.25.1"))
        assert result.status == "skip"
        assert "1.25.1" in result.detail

    def test_the_release_that_refuses_warns(self, tmp_path: Path) -> None:
        assert _run(_cfg(tmp_path), QUOTED, installed=REFUSING).status == "warn"

    def test_a_later_release_still_warns(self, tmp_path: Path) -> None:
        """The refusal is permanent, so the gate is ``>=`` and not ``==``."""
        result = _run(_cfg(tmp_path), QUOTED, installed=Version("1.31.0"))
        assert result.status == "warn"

    def test_an_unreadable_version_still_reports(self, tmp_path: Path) -> None:
        """Unknown means unknown; the sibling check errs the same way.

        A warning costs seconds to dismiss.  The failure it predicts arrives
        mid-deploy, after the migrations have been applied.
        """
        assert _run(_cfg(tmp_path), QUOTED, installed=None).status == "warn"

    def test_the_refusing_release_is_pinned(self) -> None:
        """#505 is unreleased, so this number is expected rather than measured.

        confiture bumps its version in the release PR, which is why both probe
        venvs built from the PR branches self-report 1.25.1.  When confiture
        tags the release that carries #505, re-measure and move this — the test
        exists so that is a decision rather than an oversight.
        """
        from fraisier import doctor

        assert Version("1.26.0") == doctor._QUOTED_NAMES_REFUSED_IN
        assert Version("1.25.1") < doctor._QUOTED_NAMES_REFUSED_IN, (
            "1.25.1 is the last confiture measured to compare a quoted-name "
            "schema without refusing"
        )

    def test_a_signatures_only_gate_is_unaffected(self, tmp_path: Path) -> None:
        """``--check-signatures`` compares no built schema, so #505 cannot bite."""
        cfg = _cfg(tmp_path, checks="[signatures]")
        assert _run(cfg, QUOTED).status == "skip"

    def test_a_gate_that_cannot_build_says_nothing(self, tmp_path: Path) -> None:
        """An unbuildable tree is another check's finding, not this one's."""
        result = _run(_cfg(tmp_path), QUOTED, returncode=1)
        assert result.status == "skip"


class TestOnlyIdentifiersAreScanned:
    """Cycle 3.3 — a quote that is not an identifier must not read as one.

    The built schema confiture emits is not bare DDL: it carries a generated
    header and a ``/* File: … */`` banner per input file, and the DDL itself
    carries comments and string literals.  Every one of those can hold a
    double quote, and a scan that treats each as an identifier reports a
    project that has no problem — the worst outcome for a check whose whole
    purpose is to be believed.
    """

    def test_a_name_named_in_a_line_comment_is_not_an_offender(
        self, tmp_path: Path
    ) -> None:
        schema = (
            '-- dropped the old "createdAt" column in 2026\n'
            "CREATE TABLE app.tb_user (id BIGINT PRIMARY KEY);\n"
        )
        assert _run(_cfg(tmp_path), schema).status == "pass"

    def test_a_name_named_in_a_block_comment_is_not_an_offender(
        self, tmp_path: Path
    ) -> None:
        """confiture's own banner is a block comment, above every included file."""
        schema = (
            '/* ====\n * File: 010_orm.sql — was "userName"\n * ==== */\n'
            "CREATE TABLE app.tb_user (id BIGINT PRIMARY KEY);\n"
        )
        assert _run(_cfg(tmp_path), schema).status == "pass"

    def test_a_quote_inside_a_string_literal_is_not_an_offender(
        self, tmp_path: Path
    ) -> None:
        schema = (
            "CREATE TABLE app.tb_user (\n"
            "    id BIGINT PRIMARY KEY,\n"
            "    note TEXT DEFAULT 'he said \"createdAt\" once'\n"
            ");\n"
        )
        assert _run(_cfg(tmp_path), schema).status == "pass"

    def test_an_offender_beside_a_comment_is_still_found(self, tmp_path: Path) -> None:
        """Stripping must not swallow the line it precedes."""
        schema = (
            "-- the ORM generated this\n"
            'CREATE TABLE app.tb_user ("createdAt" TIMESTAMPTZ);\n'
        )
        result = _run(_cfg(tmp_path), schema)
        assert result.status == "warn"
        assert "createdAt" in result.detail


class TestTheAdviceIsActionable:
    """Cycle 3.3 — the message names a rename and stays readable at scale."""

    def test_the_message_offers_a_conforming_rename(self, tmp_path: Path) -> None:
        result = _run(_cfg(tmp_path), QUOTED)
        assert "createdat" in result.detail
        assert result.fix_hint is not None
        assert "naming_004" in result.fix_hint

    def test_a_reserved_word_is_not_renamed_to_itself(self, tmp_path: Path) -> None:
        """Folding ``"user"`` to ``user`` would advise the same broken name."""
        schema = 'CREATE TABLE app.tb_account ("user" TEXT);\n'
        result = _run(_cfg(tmp_path), schema)
        assert "→ user," not in result.detail
        assert "user_" in result.detail

    def test_hundreds_of_offenders_stay_one_readable_line(self, tmp_path: Path) -> None:
        columns = ",\n".join(f'    "col{n}X" TEXT' for n in range(200))
        schema = f"CREATE TABLE app.tb_wide (\n{columns}\n);\n"
        result = _run(_cfg(tmp_path), schema)
        assert result.status == "warn"
        assert "200 identifier(s)" in result.detail
        assert "and 195 more" in result.detail
        assert len(result.detail) < 500

    def test_the_same_name_across_tables_is_counted_once(self, tmp_path: Path) -> None:
        """One rename, one finding — twenty repeats of a name is still one job."""
        schema = "".join(
            f'CREATE TABLE app.tb_{n} ("createdAt" TIMESTAMPTZ);\n' for n in range(20)
        )
        result = _run(_cfg(tmp_path), schema)
        assert "1 identifier(s)" in result.detail


class TestAFunctionBodyIsNotADeclaration:
    """A ``$$ … $$`` body references names; it does not declare them.

    A DDL tree with views and PL/pgSQL is ordinary, and a body that reads a
    quoted column from a table this schema does not own would otherwise be
    reported as this project's problem to rename. The declarations are what
    confiture grades, and they are all outside the body.
    """

    def test_a_quoted_name_inside_a_body_is_not_an_offender(
        self, tmp_path: Path
    ) -> None:
        schema = (
            "CREATE FUNCTION app.fn_sync() RETURNS void AS $$\n"
            '    SELECT "createdAt" FROM legacy.tb_import;\n'
            "$$ LANGUAGE sql;\n"
        )
        assert _run(_cfg(tmp_path), schema).status == "pass"

    def test_a_tagged_body_is_stripped_too(self, tmp_path: Path) -> None:
        schema = (
            "CREATE FUNCTION app.fn_sync() RETURNS void AS $body$\n"
            '    SELECT "userName" FROM legacy.tb_import;\n'
            "$body$ LANGUAGE plpgsql;\n"
        )
        assert _run(_cfg(tmp_path), schema).status == "pass"

    def test_the_function_name_itself_is_still_an_offender(
        self, tmp_path: Path
    ) -> None:
        """The declaration sits outside the body, so stripping must not hide it."""
        schema = (
            'CREATE FUNCTION app."syncNow"() RETURNS void AS $$\n'
            "    SELECT 1;\n"
            "$$ LANGUAGE sql;\n"
        )
        result = _run(_cfg(tmp_path), schema)
        assert result.status == "warn"
        assert "syncNow" in result.detail

    def test_a_declaration_after_a_body_is_still_found(self, tmp_path: Path) -> None:
        """Stripping must end at the closing tag, not run to end of file."""
        schema = (
            "CREATE FUNCTION app.fn_sync() RETURNS void AS $$\n"
            "    SELECT 1;\n"
            "$$ LANGUAGE sql;\n"
            'CREATE TABLE app.tb_user ("createdAt" TIMESTAMPTZ);\n'
        )
        result = _run(_cfg(tmp_path), schema)
        assert result.status == "warn"
        assert "createdAt" in result.detail
