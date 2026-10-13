"""``tview_data: keep | exclude``, the opt-in to leave TVIEW rows out of a dump (#471).

Validated in both places it can be set: the top-level ``backup:`` block (for
``fraisier backup run``) and ``database.pre_migrate_dump``. A typo must not
quietly mean ``keep``: someone who asked for lean dumps would get full ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fraisier.config._validation import (
    ValidationError,
    backup_tview_data,
    validate_one_fraise_environment,
)
from fraisier.validation import _collect_all_validation_errors
from tests.test_backup_retention_config import write_config

if TYPE_CHECKING:
    from pathlib import Path


class TestBackupBlock:
    @pytest.mark.parametrize("value", ["keep", "exclude"])
    def test_each_value_is_read(self, value: str) -> None:
        assert backup_tview_data({"tview_data": value}) == value

    @pytest.mark.parametrize("backup", [None, {}, {"compression": "zstd:9"}])
    def test_the_default_is_keep(self, backup: object) -> None:
        assert backup_tview_data(backup) == "keep"

    @pytest.mark.parametrize("bad", ["Exclude", "lean", True, None, 1])
    def test_anything_else_is_rejected(self, bad: object) -> None:
        with pytest.raises(ValidationError, match=r"backup\.tview_data"):
            backup_tview_data({"tview_data": bad})

    def test_fraisier_validate_reports_it(self, tmp_path: Path) -> None:
        config = write_config(tmp_path, "\nbackup:\n  tview_data: lean\n")

        paths = [path for path, _exc in _collect_all_validation_errors(config)]

        assert "backup.tview_data" in paths

    def test_the_loader_carries_the_validated_value(self, tmp_path: Path) -> None:
        config = write_config(tmp_path, "\nbackup:\n  tview_data: exclude\n")

        assert config.backup_tview_data == "exclude"


def _env(**dump: object) -> dict:
    return {"database": {"pre_migrate_dump": dump}}


class TestPreMigrateDump:
    @pytest.mark.parametrize("value", ["keep", "exclude"])
    def test_each_value_is_accepted(self, value: str) -> None:
        validate_one_fraise_environment(
            "api", "production", _env(enabled=True, tview_data=value)
        )

    @pytest.mark.parametrize("bad", ["Exclude", "lean", True, None])
    def test_anything_else_is_rejected(self, bad: object) -> None:
        with pytest.raises(ValidationError, match=r"pre_migrate_dump\.tview_data"):
            validate_one_fraise_environment(
                "api", "production", _env(enabled=True, tview_data=bad)
            )

    def test_it_is_validated_even_when_the_gate_is_off(self) -> None:
        with pytest.raises(ValidationError, match="tview_data"):
            validate_one_fraise_environment(
                "api", "production", _env(enabled=False, tview_data="lean")
            )
