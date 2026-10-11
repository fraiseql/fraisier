"""``view_definition_mismatch`` and ``object_definition_mismatch`` (confiture 1.36.0).

Both are new ``warning`` drift kinds, so each can be escalated. They differ in
where they fire:

* ``view_definition_mismatch`` (a view or matview whose query the database holds
  otherwise) fires **only at the materialised tier**, which the gate reaches
  only when the environment YAML sets ``scratch_url``: fraisier passes no
  ``--scratch-url``. Without one, escalating it promotes nothing, so the gate
  refuses the pairing, as it does ``extra_object`` without ``extra_objects: all``.
* ``object_definition_mismatch`` (a redefined domain, type, policy or rule) fires
  at both tiers. Without a scratch server an expression in one is compared only
  for presence, but a role, a ``NOT NULL`` or a base type is compared, so it
  needs no opt-in.

The payloads are the ``migrate validate --check-live-drift`` bytes of the
published 1.36.0 wheel against pg_tviews 0.1.0-beta.27, never composed:
``.phases/2026-10-12-confiture-1-36-probe/driver14.py --wire``, scenarios
``AD_view_redef+scratch`` and ``AK_policy_roles``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.drift import ESCALATABLE_KINDS, check_schema_drift

FIXTURES = Path(__file__).parent / "fixtures" / "confiture"
VIEW_REDEFINED = json.loads(
    (FIXTURES / "drift-view-definition-mismatch.json").read_text()
)
POLICY_REDEFINED = json.loads(
    (FIXTURES / "drift-object-definition-mismatch.json").read_text()
)

_ENV = (
    "name: production\ndatabase_url: postgresql:///app\ninclude_dirs:\n  - db/schema\n"
)
_SCRATCH = "scratch_url: postgresql:///postgres\n"


def _project(root: Path, *, scratch: bool) -> Path:
    (root / "db/environments").mkdir(parents=True)
    (root / "db/environments/production.yaml").write_text(
        _ENV + (_SCRATCH if scratch else "")
    )
    (root / "db/schema").mkdir()
    (root / "db/schema/000.sql").write_text("CREATE TABLE t (id int);\n")
    return root


@pytest.fixture
def scratch_project(tmp_path: Path) -> Path:
    return _project(tmp_path, scratch=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return _project(tmp_path, scratch=False)


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


@pytest.mark.parametrize(
    "kind", ["view_definition_mismatch", "object_definition_mismatch"]
)
def test_both_are_escalatable_kinds(kind: str) -> None:
    assert kind in ESCALATABLE_KINDS


class TestViewDefinitionMismatch:
    def test_unescalated_it_is_a_warning_and_the_gate_passes(
        self, scratch_project: Path
    ) -> None:
        result, _ = _gate(scratch_project, VIEW_REDEFINED)

        assert result.passed
        assert [i.kind for i in result.warnings] == ["view_definition_mismatch"]

    def test_escalated_with_a_scratch_url_it_fails_the_gate(
        self, scratch_project: Path
    ) -> None:
        result, _ = _gate(
            scratch_project, VIEW_REDEFINED, escalate=("view_definition_mismatch",)
        )

        assert not result.passed
        assert [i.kind for i in result.critical] == ["view_definition_mismatch"]

    def test_escalating_without_a_scratch_url_is_refused_not_inert(
        self, project: Path
    ) -> None:
        """At the structural tier confiture never emits it, so the gate would pass.

        The operator who wrote ``escalate: [view_definition_mismatch]`` would
        believe a redefined view stops the deploy; it never would.
        """
        result, mock_run = _gate(
            project, VIEW_REDEFINED, escalate=("view_definition_mismatch",)
        )

        assert not result.passed
        assert not result.ran
        assert "scratch_url" in str(result.error)
        assert mock_run.call_count == 0

    def test_an_empty_scratch_url_is_no_scratch_url(self, tmp_path: Path) -> None:
        root = _project(tmp_path, scratch=False)
        env = root / "db/environments/production.yaml"
        env.write_text(env.read_text() + "scratch_url:\n")

        result, mock_run = _gate(
            root, VIEW_REDEFINED, escalate=("view_definition_mismatch",)
        )

        assert not result.ran
        assert mock_run.call_count == 0


class TestObjectDefinitionMismatch:
    def test_unescalated_it_is_a_warning_and_the_gate_passes(
        self, project: Path
    ) -> None:
        result, _ = _gate(project, POLICY_REDEFINED)

        assert result.passed
        assert [i.kind for i in result.warnings] == ["object_definition_mismatch"]

    def test_escalated_it_fails_the_gate_without_a_scratch_url(
        self, project: Path
    ) -> None:
        result, _ = _gate(
            project, POLICY_REDEFINED, escalate=("object_definition_mismatch",)
        )

        assert not result.passed
        assert [i.kind for i in result.critical] == ["object_definition_mismatch"]
