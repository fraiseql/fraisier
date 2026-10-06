"""``extra_object`` and the ``--extra-objects all`` opt-in (confiture 1.30.0).

By default confiture reports a stray object (a policy, a domain, a schema, …)
only for a kind the DDL declares, and grades it ``info`` -- which escalation
never reads.  Under ``--extra-objects all`` every stray object is a ``warning``,
so ``escalate`` can fail a deploy on one.  So escalating ``extra_object`` means
nothing without the opt-in, and the gate refuses that pairing rather than run
a check that cannot fire.

The two payloads are the wire bytes of live confiture 1.30.0 runs, preserved in
``.phases/2026-10-06-python-3-14-floor/wire-extra-object.json`` by
``capture_extra_object_wire.py``; nothing here is composed by hand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.drift import ESCALATABLE_KINDS, check_schema_drift

_REPORT = {
    "check": "live_drift",
    "database_name": "fraisier_probe_130_wire",
    "has_drift": True,
    "has_critical_drift": False,
    "critical_count": 0,
    "info_count": 0,
    "tables_checked": 1,
    "columns_checked": 2,
    "indexes_checked": 0,
    "constraints_checked": 1,
    "constraint_definitions_compared": 1,
    "objects_checked": 0,
    "detection_time_ms": 0,
    "ok": True,
    "command": "migrate validate",
    "parser": {"pglast": "8.4", "pg_major": 18},
}

#: ``policy_undeclared`` under ``--extra-objects all``: a policy on a table, in
#: a tree that declares no policy.  Graded ``warning``.
STRAY_POLICY = {
    **_REPORT,
    "warning_count": 1,
    "drift_items": [
        {
            "type": "extra_object",
            "severity": "warning",
            "object": "app.tb_item.stray_pol",
            "expected": None,
            "actual": "app.tb_item.stray_pol",
            "message": (
                "Policy 'app.tb_item.stray_pol' exists but is not in expected schema"
            ),
            "subject": {
                "schema": "app",
                "relation": None,
                "name": "tb_item.stray_pol",
                "arguments": None,
                "role": None,
                "kind": "policy",
            },
        }
    ],
}

#: ``domain_declared`` at the default: a second domain in a tree that declares
#: one.  Graded ``info`` -- the grade escalation never reads.
STRAY_DOMAIN_INFO = {
    **_REPORT,
    "has_drift": True,
    "warning_count": 0,
    "info_count": 1,
    "drift_items": [
        {
            "type": "extra_object",
            "severity": "info",
            "object": "app.stray_dom",
            "expected": None,
            "actual": "app.stray_dom",
            "message": "Domain 'app.stray_dom' exists but is not in expected schema",
            "subject": {
                "schema": "app",
                "relation": None,
                "name": "stray_dom",
                "arguments": None,
                "role": None,
                "kind": "domain",
            },
        }
    ],
}

CLEAN = {**_REPORT, "has_drift": False, "warning_count": 0, "drift_items": []}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "db/environments").mkdir(parents=True)
    (tmp_path / "db/environments/production.yaml").write_text(
        "name: production\ndatabase_url: postgresql:///app\ninclude_dirs:\n"
        "  - db/schema\n"
    )
    (tmp_path / "db/schema").mkdir()
    (tmp_path / "db/schema/000.sql").write_text("CREATE TABLE t (id int);\n")
    return tmp_path


def _fake(payload: dict):
    def run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "build":
            Path(cmd[cmd.index("--output") + 1]).write_text("CREATE TABLE t ();")
            return MagicMock(
                returncode=0,
                stdout=json.dumps({"success": True, "warnings": [], "duplicates": []}),
                stderr="",
            )
        return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

    return run


def _gate(project: Path, payload: dict, **kwargs: Any):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = _fake(payload)
        result = check_schema_drift(
            project_dir=project,
            confiture_config=project / "db/environments/production.yaml",
            checks=["live-drift"],
            **kwargs,
        )
    return result, mock_run


def _validate_argv(mock_run: MagicMock) -> list[str]:
    return next(c.args[0] for c in mock_run.call_args_list if c.args[0][1] == "migrate")


def test_extra_object_is_an_escalatable_kind() -> None:
    assert "extra_object" in ESCALATABLE_KINDS


class TestTheFlag:
    def test_the_default_sends_no_extra_objects_flag(self, project: Path) -> None:
        _, mock_run = _gate(project, CLEAN)
        assert "--extra-objects" not in _validate_argv(mock_run)

    def test_all_is_passed_through_to_live_drift(self, project: Path) -> None:
        _, mock_run = _gate(project, CLEAN, extra_objects="all")
        argv = _validate_argv(mock_run)
        assert argv[argv.index("--extra-objects") + 1] == "all"

    def test_a_signatures_only_gate_never_sends_it(self, project: Path) -> None:
        """``--extra-objects`` is ``--check-live-drift``'s; elsewhere it claims a scope."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _fake(CLEAN)
            check_schema_drift(
                project_dir=project,
                confiture_config=project / "db/environments/production.yaml",
                checks=["signatures"],
                extra_objects="all",
            )
        assert "--extra-objects" not in _validate_argv(mock_run)

    def test_declared_is_the_default_spelt_out_and_sends_nothing(
        self, project: Path
    ) -> None:
        _, mock_run = _gate(project, CLEAN, extra_objects="declared")
        assert "--extra-objects" not in _validate_argv(mock_run)

    def test_an_unknown_mode_refuses_the_run(self, project: Path) -> None:
        result, mock_run = _gate(project, CLEAN, extra_objects="every")
        assert not result.passed
        assert not result.ran
        assert "every" in str(result.error)
        assert mock_run.call_count == 0


class TestEscalation:
    def test_a_stray_policy_fails_the_gate_when_asked_for(self, project: Path) -> None:
        result, _ = _gate(
            project, STRAY_POLICY, extra_objects="all", escalate=("extra_object",)
        )
        assert not result.passed
        assert result.ran
        assert [i.object_name for i in result.critical] == ["app.tb_item.stray_pol"]

    def test_unescalated_it_is_a_warning_and_the_gate_passes(
        self, project: Path
    ) -> None:
        result, _ = _gate(project, STRAY_POLICY, extra_objects="all")
        assert result.passed
        assert [i.kind for i in result.warnings] == ["extra_object"]

    def test_escalating_without_the_opt_in_is_refused_not_inert(
        self, project: Path
    ) -> None:
        """At the default an extra object is ``info``, which escalation never reads.

        The same payload the default produces for a declared kind, so the
        refusal is not hypothetical: the gate would pass it, and the operator
        who wrote ``escalate: [extra_object]`` would believe it stopped them.
        """
        result, mock_run = _gate(project, STRAY_DOMAIN_INFO, escalate=("extra_object",))
        assert not result.passed
        assert not result.ran
        assert "extra_objects" in str(result.error)
        assert mock_run.call_count == 0

    def test_an_info_extra_object_never_fails_the_default_gate(
        self, project: Path
    ) -> None:
        result, _ = _gate(project, STRAY_DOMAIN_INFO)
        assert result.passed
