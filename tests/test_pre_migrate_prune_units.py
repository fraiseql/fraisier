"""The pre-migrate prune timer: rendered, installed, enabled — and gated (#420).

``pre_migrate_dump`` pruned only inside a deploy, so a quiet week left the whole
corpus on disk. ``fraisier backup prune --pre-migrate`` is the prune without the
deploy; this is what schedules it. One pair per (fraise, environment) that has a
gate **and** a retention rule — never for a gate with nothing to prune by, which
would be a timer that exits 1 nightly.

Unlike the retain pair, which is env-owned because a received corpus has no
producing fraise here, this one has an owner: the fraise whose deploys fill the
directory. So it carries ``fraise=`` and is gated by ``_scope_active``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from fraisier.config import FraisierConfig
from fraisier.naming import pre_migrate_prune_unit_names
from fraisier.scaffold.renderer import ScaffoldRenderer

PROJECT = "myproj"
GATE = {
    "enabled": True,
    "output_dir": "/var/lib/postgresql/pre_migrate",
    "retention_hours": 72,
}


def render(tmp_path, gate: dict | None = GATE, *, user: str = "fraisier") -> Any:
    database: dict = {"strategy": "apply", "name": "api"}
    if gate is not None:
        database["pre_migrate_dump"] = gate
    config = {
        "name": PROJECT,
        "scaffold": {"deploy_user": user, "output_dir": str(tmp_path / "output")},
        "fraises": {
            "api": {
                "type": "api",
                "environments": {
                    "production": {
                        "app_path": "/var/www/api",
                        "git_repo": "/srv/git/api.git",
                        "database": database,
                    }
                },
            }
        },
    }
    path = tmp_path / "fraises.yaml"
    path.write_text(yaml.safe_dump(config))
    ScaffoldRenderer(FraisierConfig(path)).render()
    return tmp_path / "output"


@pytest.fixture
def rendered(tmp_path):
    return render(tmp_path)


def names() -> tuple[str, str]:
    return pre_migrate_prune_unit_names(PROJECT, "api", "production")


def directives(text: str, key: str) -> list[str]:
    return [
        line.strip() for line in text.splitlines() if line.strip().startswith(f"{key}=")
    ]


def pre_migrate_units(output) -> list[str]:
    return sorted(p.name for p in (output / "systemd").glob("*pre-migrate-prune*"))


class TestRenderScope:
    def test_a_gate_with_a_rule_gets_a_pair(self, rendered):
        assert pre_migrate_units(rendered) == sorted(names())

    @pytest.mark.parametrize(
        "gate",
        [
            None,
            {"enabled": False, "output_dir": "/x", "retention_hours": 72},
            {"enabled": True, "output_dir": "/x"},
        ],
        ids=["no gate", "disabled gate", "no retention rule"],
    )
    def test_nothing_to_prune_by_renders_nothing(self, tmp_path, gate):
        """A timer that exits 1 every night is worse than no timer."""
        assert pre_migrate_units(render(tmp_path, gate)) == []

    def test_keep_last_alone_is_a_rule(self, tmp_path):
        output = render(tmp_path, {"enabled": True, "output_dir": "/x", "keep_last": 3})

        assert pre_migrate_units(output) == sorted(names())


class TestService:
    def test_execstart_selects_the_fraise_and_environment_only(self, rendered):
        """The unit carries a selector, not a policy: retention stays in config."""
        text = (rendered / "systemd" / names()[0]).read_text()

        (exec_start,) = directives(text, "ExecStart")
        assert exec_start.endswith("backup prune --pre-migrate api --env production")
        assert "72" not in exec_start

    def test_it_runs_as_the_deploy_user(self, tmp_path):
        """The user whose deploys write the dumps — checked, not guessed:
        ``output_dir`` must be writable by ``deploy_user`` (run_backup creates
        nothing), so that is who can delete from it."""
        text = (render(tmp_path, user="deployer") / "systemd" / names()[0]).read_text()

        assert directives(text, "User") == ["User=deployer"]
        assert "/home/deployer/.local/bin/fraisier" in text

    def test_readwritepaths_grants_the_corpus_and_the_lock_directory(self, rendered):
        """Without the corpus, ProtectSystem=strict makes every prune a silent
        no-op (#317's shape); without the lock directory it cannot take the lock."""
        text = (rendered / "systemd" / names()[0]).read_text()

        assert "ProtectSystem=strict" in text
        granted = [
            path
            for d in directives(text, "ReadWritePaths")
            for path in d.removeprefix("ReadWritePaths=").split()
        ]
        assert "/var/lib/postgresql/pre_migrate" in granted
        assert any(g.lstrip("-") == "/run/fraisier" for g in granted)
        assert "/var/lib/postgresql" not in granted, "nothing above the corpus"

    def test_no_protecthome(self, rendered):
        """ExecStart is ~/.local/bin/fraisier, which ProtectHome hides (#341)."""
        for unit in names():
            text = (rendered / "systemd" / unit).read_text()
            assert directives(text, "ProtectHome") == []

    def test_oneshot_without_an_install_section(self, rendered):
        text = (rendered / "systemd" / names()[0]).read_text()

        assert "Type=oneshot" in text
        assert "[Install]" not in text

    def test_every_directive_sits_on_its_own_line(self, rendered):
        """A `{#-` eats the newline of the directive above; a literal `#}` closes
        a comment early. Either installs fine and never prunes."""
        for unit in names():
            for line in (rendered / "systemd" / unit).read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "[")):
                    continue
                assert stripped.split("=", 1)[0].isidentifier(), (
                    f"{unit}: {stripped!r} is not one directive"
                )


class TestTimer:
    def test_it_defaults_to_daily_with_exactly_one_oncalendar(self, rendered):
        """OnCalendar= accumulates: a second non-empty assignment is a second firing (#311)."""
        text = (rendered / "systemd" / names()[1]).read_text()

        assert directives(text, "OnCalendar") == ["OnCalendar=daily"]

    def test_prune_schedule_overrides_it(self, tmp_path):
        output = render(tmp_path, {**GATE, "prune_schedule": "*-*-* 04:15:00 UTC"})

        text = (output / "systemd" / names()[1]).read_text()
        assert directives(text, "OnCalendar") == ["OnCalendar=*-*-* 04:15:00 UTC"]

    def test_persistent_wanted_by_timers_and_names_its_service(self, rendered):
        text = (rendered / "systemd" / names()[1]).read_text()

        assert "Persistent=true" in text
        assert "WantedBy=timers.target" in text
        assert f"Unit={names()[0]}" in text


class TestInstallation:
    def manifest(self, rendered) -> list[dict]:
        data = json.loads((rendered / "artifact-manifest.json").read_text())
        return [
            a for a in data["artifacts"] if "pre-migrate-prune" in (a["source"] or "")
        ]

    def test_both_units_are_in_the_manifest_as_timers(self, rendered):
        artifacts = self.manifest(rendered)

        assert len(artifacts) == 2
        assert {a["disposition"] for a in artifacts} == {"timer"}

    def test_they_are_gated_on_the_owning_fraise_and_environment(self, rendered):
        """The fraise's deploys fill the directory, so only a host that deploys
        that fraise there may prune it — and may enable the timer."""
        for artifact in self.manifest(rendered):
            assert (artifact["fraise"], artifact["environment"]) == (
                "api",
                "production",
            )

    def test_install_sh_gates_install_and_enable_with_scope_active(self, rendered):
        lines = (rendered / "install.sh").read_text().splitlines()
        service, timer = names()

        for unit in (service, timer):
            (index,) = [
                i
                for i, line in enumerate(lines)
                if f'_install_artifact "systemd/{unit}"' in line
            ]
            assert (
                lines[index - 1].strip() == 'if _scope_active "api" "production"; then'
            )
        (index,) = [
            i
            for i, line in enumerate(lines)
            if f"systemctl enable --now {timer}" in line
        ]
        preceding = [line.strip() for line in lines[max(0, index - 4) : index]]
        assert 'if _scope_active "api" "production"; then' in preceding

    def test_renderer_and_manifest_read_one_naming_authority(
        self, tmp_path, monkeypatch
    ):
        import fraisier.naming

        def renamed(project, fraise, env):
            return (
                f"zz-{project}-{fraise}-{env}.service",
                f"zz-{project}-{fraise}-{env}.timer",
            )

        monkeypatch.setattr(fraisier.naming, "pre_migrate_prune_unit_names", renamed)
        output = render(tmp_path)

        assert {p.name for p in (output / "systemd").glob("zz-*")} == {
            "zz-myproj-api-production.service",
            "zz-myproj-api-production.timer",
        }
        manifest = json.loads((output / "artifact-manifest.json").read_text())
        assert {
            a["destination"]
            for a in manifest["artifacts"]
            if a["destination"] and "zz-" in a["destination"]
        } == {
            "/etc/systemd/system/zz-myproj-api-production.service",
            "/etc/systemd/system/zz-myproj-api-production.timer",
        }


class TestDoctorSeesIt:
    """``backup_retention`` answers "is anything pruning what this host keeps?".

    A gate's corpus is one of those, and the timer for it exists only after
    ``scaffold-install`` — so an upgrade that adds the timer changes nothing on
    a host until someone installs it, and nothing else would say so.
    """

    def renderer(self, tmp_path):
        render(tmp_path)
        config = FraisierConfig(tmp_path / "fraises.yaml")
        return ScaffoldRenderer(config)

    def test_a_gate_with_a_rule_is_reported_not_installed_until_it_is(self, tmp_path):
        from fraisier.scaffold.retention import pre_migrate_prune_report

        systemd = tmp_path / "etc-systemd"
        systemd.mkdir()

        (before,) = pre_migrate_prune_report(
            self.renderer(tmp_path), systemd_dir=systemd
        )
        for unit in names():
            (systemd / unit).write_text("x")
        (after,) = pre_migrate_prune_report(
            self.renderer(tmp_path), systemd_dir=systemd
        )

        assert before.installed is False
        assert after.installed is True
        assert "pre_migrate_dump:api" in before.detail
        assert "/var/lib/postgresql/pre_migrate" in before.detail

    def test_the_doctor_check_warns_and_says_how_to_install(
        self, tmp_path, monkeypatch
    ):
        from fraisier import doctor

        render(tmp_path)
        monkeypatch.setattr(
            "fraisier.scaffold.retention.SYSTEMD_DIR", str(tmp_path / "none")
        )

        result = doctor._check_backup_retention(
            FraisierConfig(tmp_path / "fraises.yaml")
        )

        assert result.status == "warn"
        assert "pre_migrate_dump:api" in result.detail
        assert "scaffold-install" in (result.fix_hint or "")
        assert "/var/lib/postgresql/pre_migrate" in (result.fix_hint or "")

    def test_it_passes_once_the_pair_is_installed(self, tmp_path, monkeypatch):
        from fraisier import doctor

        render(tmp_path)
        systemd = tmp_path / "etc-systemd"
        systemd.mkdir()
        for unit in names():
            (systemd / unit).write_text("x")
        monkeypatch.setattr("fraisier.scaffold.retention.SYSTEMD_DIR", str(systemd))

        result = doctor._check_backup_retention(
            FraisierConfig(tmp_path / "fraises.yaml")
        )

        assert result.status == "pass"
