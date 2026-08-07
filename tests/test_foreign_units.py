"""Units a host should no longer have: reported by default, pruned on request.

Making host scoping fraise-aware (#336) does not retroactively clean the
hosts that were mis-scoped. A box that installed its neighbour's units
still has them on disk, enabled, quite possibly running.

Removing them as a side effect of ``scaffold-install`` was considered and
rejected (decision 5). The precedent cuts both ways — ``install.sh`` does
remove stale pre-0.7.1 socket units — but those are units fraisier named
itself under a superseded scheme. A neighbouring fraise's unit is another
application's service, possibly serving traffic, and auto-stopping it
would turn a routine ``scaffold-install`` into an outage on precisely the
configs #336 describes. So: report by default, act only when typed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from fraisier.config import FraisierConfig
from fraisier.errors import ValidationError
from fraisier.scaffold.foreign import (
    ForeignUnit,
    find_foreign_units,
    prune_foreign_units,
)
from tests.test_install_plan_golden import _SCOPED_HOSTS


@pytest.fixture
def config(tmp_path: Path) -> FraisierConfig:
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(_SCOPED_HOSTS)
    return FraisierConfig(cfg)


def _install(root: Path, *units: str) -> None:
    """Pretend *units* are already installed under a rerooted /etc."""
    systemd = root / "etc/systemd/system"
    systemd.mkdir(parents=True, exist_ok=True)
    for unit in units:
        (systemd / unit).write_text("[Unit]\n")


class TestFindForeignUnits:
    """Installed here, owned by a fraise that does not run here."""

    def test_a_neighbours_unit_is_foreign(self, config, tmp_path):
        root = tmp_path / "root"
        _install(root, "worker.service", "api.service")

        foreign = find_foreign_units(config, server="a.example.io", root=root)

        assert [f.unit_name for f in foreign] == ["worker.service"]
        assert foreign[0].owner_fraise == "worker"
        assert foreign[0].environment == "production"

    def test_a_unit_this_host_owns_is_not_foreign(self, config, tmp_path):
        root = tmp_path / "root"
        _install(root, "api.service")

        foreign = find_foreign_units(config, server="a.example.io", root=root)

        assert foreign == []

    def test_a_unit_that_is_not_installed_is_not_reported(self, config, tmp_path):
        """Foreign means *present*, not merely possible."""
        root = tmp_path / "root"
        root.mkdir()

        foreign = find_foreign_units(config, server="a.example.io", root=root)

        assert foreign == []

    def test_env_owned_units_are_never_foreign(self, config, tmp_path):
        """They have no owning fraise to be foreign to (decision 4).

        The unit-installer helper is one per (project, environment); a host
        declaring that environment is entitled to it whichever fraise
        declared the environment.
        """
        root = tmp_path / "root"
        _install(root, "fraisier-proj-production-unit-installer.socket")

        foreign = find_foreign_units(config, server="a.example.io", root=root)

        assert foreign == []

    def test_an_unresolvable_host_reports_nothing(self, config, tmp_path, monkeypatch):
        """Off-server, naming an arbitrary host's units answers no question.

        Same reasoning as the webhook entry ``scaffold-diff`` omits when it
        cannot tell which machine it is on: reporting nothing beats
        reporting a phantom.
        """
        monkeypatch.setattr(
            "fraisier.scaffold.renderer.local_hostnames", lambda: ["nowhere"]
        )
        root = tmp_path / "root"
        _install(root, "worker.service", "api.service")

        assert find_foreign_units(config, root=root) == []


class TestPruneForeign:
    """Dangerous by design, so it is typed and it re-checks its own input."""

    def test_prune_foreign_disables_and_removes_them(self, config, tmp_path):
        root = tmp_path / "root"
        _install(root, "worker.service")
        foreign = find_foreign_units(config, server="a.example.io", root=root)
        calls: list[list[str]] = []

        prune_foreign_units(config, foreign, server="a.example.io", runner=calls.append)

        assert calls == [
            ["sudo", "systemctl", "disable", "--now", "worker.service"],
            ["sudo", "rm", "-f", str(root / "etc/systemd/system/worker.service")],
            ["sudo", "systemctl", "daemon-reload"],
        ]

    def test_prune_foreign_never_touches_a_unit_this_host_owns(self, config, tmp_path):
        """The guard is in the prune, not only in the caller that built the list.

        A caller that assembled the list from a stale render, or from the
        wrong server, must not be able to talk this into disabling a
        service the host is actually running.
        """
        mine = ForeignUnit(
            source="systemd/api.service",
            installed_path=tmp_path / "etc/systemd/system/api.service",
            owner_fraise="api",
            environment="production",
        )
        calls: list[list[str]] = []

        with pytest.raises(ValidationError, match="api/production"):
            prune_foreign_units(
                config, [mine], server="a.example.io", runner=calls.append
            )

        assert calls == []

    def test_nothing_to_prune_runs_nothing(self, config):
        calls: list[list[str]] = []

        prune_foreign_units(config, [], server="a.example.io", runner=calls.append)

        assert calls == []


class TestScaffoldDiffReport:
    """The default surface: named, with its owner, and acted on by nobody."""

    def _invoke(self, tmp_path, monkeypatch, foreign):
        monkeypatch.setattr(
            "fraisier.scaffold.foreign.find_foreign_units", lambda *_a, **_k: foreign
        )
        monkeypatch.setattr(
            "fraisier.scaffold.diff.compute_scaffold_diff", lambda **_k: []
        )
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCOPED_HOSTS)
        from fraisier.cli.main import main

        return CliRunner().invoke(main, ["-c", str(cfg), "scaffold-diff"])

    def test_scaffold_diff_reports_a_foreign_unit_with_its_owner(
        self, tmp_path, monkeypatch
    ):
        foreign = [
            ForeignUnit(
                source="systemd/worker.service",
                installed_path=Path("/etc/systemd/system/worker.service"),
                owner_fraise="worker",
                environment="production",
            )
        ]

        result = self._invoke(tmp_path, monkeypatch, foreign)

        assert "worker.service" in result.output
        assert "worker" in result.output
        assert "foreign" in result.output.lower()

    def test_it_says_what_to_do_about_them(self, tmp_path, monkeypatch):
        foreign = [
            ForeignUnit(
                source="systemd/worker.service",
                installed_path=Path("/etc/systemd/system/worker.service"),
                owner_fraise="worker",
                environment="production",
            )
        ]

        result = self._invoke(tmp_path, monkeypatch, foreign)

        assert "--prune-foreign" in result.output

    def test_no_foreign_units_prints_no_section(self, tmp_path, monkeypatch):
        result = self._invoke(tmp_path, monkeypatch, [])

        assert "foreign" not in result.output.lower()


class TestDoctor:
    def test_doctor_lists_foreign_units(self, tmp_path, monkeypatch):
        from fraisier.doctor import DOCTOR_CHECKS

        monkeypatch.setattr(
            "fraisier.scaffold.foreign.find_foreign_units",
            lambda *_a, **_k: [
                ForeignUnit(
                    source="systemd/worker.service",
                    installed_path=Path("/etc/systemd/system/worker.service"),
                    owner_fraise="worker",
                    environment="production",
                )
            ],
        )
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCOPED_HOSTS)

        result = DOCTOR_CHECKS["foreign_units"].fn(FraisierConfig(cfg))

        assert result.status == "warn"
        assert "worker.service" in result.detail
        assert "worker" in result.detail

    def test_a_clean_host_passes(self, tmp_path, monkeypatch):
        from fraisier.doctor import DOCTOR_CHECKS

        monkeypatch.setattr(
            "fraisier.scaffold.foreign.find_foreign_units", lambda *_a, **_k: []
        )
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCOPED_HOSTS)

        result = DOCTOR_CHECKS["foreign_units"].fn(FraisierConfig(cfg))

        assert result.status == "pass"


class TestScaffoldInstallDefault:
    """The load-bearing one: a default install must not stop a service."""

    def test_scaffold_install_does_not_touch_foreign_units_by_default(
        self, tmp_path, monkeypatch
    ):
        from fraisier.cli.main import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCOPED_HOSTS)
        script = tmp_path / "out" / "install.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)

        monkeypatch.setattr(
            "fraisier.scaffold.foreign.find_foreign_units",
            lambda *_a, **_k: [
                ForeignUnit(
                    source="systemd/worker.service",
                    installed_path=Path("/etc/systemd/system/worker.service"),
                    owner_fraise="worker",
                    environment="production",
                )
            ],
        )
        pruned: list[object] = []
        monkeypatch.setattr(
            "fraisier.scaffold.foreign.prune_foreign_units",
            lambda *args, **_k: pruned.append(args),
        )

        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(cfg),
                "scaffold-install",
                "--yes",
                "--dry-run",
                "--output-dir",
                str(script.parent),
            ],
        )

        assert result.exit_code == 0
        assert pruned == [], "a default scaffold-install pruned a foreign unit"

    def test_prune_foreign_is_reachable_only_with_the_flag(self, tmp_path, monkeypatch):
        from fraisier.cli.main import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCOPED_HOSTS)
        script = tmp_path / "out" / "install.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)

        found = [
            ForeignUnit(
                source="systemd/worker.service",
                installed_path=Path("/etc/systemd/system/worker.service"),
                owner_fraise="worker",
                environment="production",
            )
        ]
        monkeypatch.setattr(
            "fraisier.scaffold.foreign.find_foreign_units", lambda *_a, **_k: found
        )
        pruned: list[object] = []

        def _fake_prune(_config, units, **_kwargs):
            pruned.extend(units)
            return list(units)

        monkeypatch.setattr(
            "fraisier.scaffold.foreign.prune_foreign_units", _fake_prune
        )

        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(cfg),
                "scaffold-install",
                "--yes",
                "--prune-foreign",
                "--output-dir",
                str(script.parent),
            ],
        )

        assert result.exit_code == 0
        assert pruned == found
