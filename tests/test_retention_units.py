"""Retention units are rendered, installed, enabled — and actually fire (#339).

`install.sh` copies timers and never enables them: `backup.timer` and
`deploy-checker.timer` are installed and inert on every host today. A
retention timer classified `PLAIN` would be rendered, installed, hashed,
drift-checked, and would never run — the incident's own failure mode (the
artifact exists, the work does not happen) reproduced inside the system
built to prevent it. Hence `Disposition.TIMER`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from fraisier.config import FraisierConfig
from fraisier.naming import retention_unit_names
from fraisier.scaffold.renderer import ScaffoldRenderer

PROJECT = "myproj"

_BASE: dict[str, Any] = {
    "name": PROJECT,
    "scaffold": {"deploy_user": "fraisier"},
    "fraises": {
        "api": {
            "type": "api",
            "environments": {
                "development": {
                    "app_path": "/var/www/api",
                    "git_repo": "/srv/git/api.git",
                }
            },
        }
    },
}

RETAIN = {
    "dir": "/backup/production",
    "match": "*_full_*.dump",
    "retention_days": 3,
    "keep_minimum": 3,
    "schedule": "*-*-* 05:30:00 UTC",
    "name": "production-full",
}


def render(tmp_path, *entries: dict, env: str = "development", user: str | None = None):
    """Render a scaffold whose *env* declares *entries*; return the output dir."""
    config = dict(_BASE)
    config["scaffold"] = {
        "deploy_user": user or "fraisier",
        "output_dir": str(tmp_path / "output"),
    }
    if entries:
        config["backup"] = {"environments": {env: {"retain": [*entries]}}}
    path = tmp_path / "fraises.yaml"
    path.write_text(yaml.safe_dump(config))
    ScaffoldRenderer(FraisierConfig(path)).render()
    return tmp_path / "output"


@pytest.fixture
def rendered(tmp_path):
    return render(tmp_path, RETAIN)


def unit_names(entry_name: str = "production-full", env: str = "development"):
    return retention_unit_names(PROJECT, env, entry_name)


def directives(text: str, key: str) -> list[str]:
    return [
        line.strip() for line in text.splitlines() if line.strip().startswith(f"{key}=")
    ]


class TestRetentionService:
    def test_retention_service_execstart_carries_env_and_name(self, rendered):
        """The unit carries a selector, not a policy (decision 8).

        Directory, glob, retention and floor stay in fraises.yaml. Two
        consequences, both wanted: the injection surface reaching a unit
        file is two validated identifiers, and a policy change re-renders
        the unit only when it moves ReadWritePaths= or the schedule.
        """
        service, _timer = unit_names()
        text = (rendered / "systemd" / service).read_text()

        (exec_start,) = directives(text, "ExecStart")
        assert "backup prune" in exec_start
        assert "--env development" in exec_start
        assert "--name production-full" in exec_start
        # The policy itself is NOT in the unit.
        assert "*_full_*.dump" not in exec_start
        assert "keep" not in exec_start.lower()

    def test_retention_service_readwritepaths_covers_the_corpus_dir(self, rendered):
        """Without it, ProtectSystem=strict makes every prune a silent no-op.

        That is #317's shape: the unit runs, reports success, and deletes
        nothing because the filesystem it was told to write to is read-only.
        """
        service, _timer = unit_names()
        text = (rendered / "systemd" / service).read_text()

        assert "ProtectSystem=strict" in text
        assert "ReadWritePaths=/backup/production" in directives(text, "ReadWritePaths")

    def test_readwritepaths_grants_the_corpus_and_nothing_above_it(self, rendered):
        service, _timer = unit_names()
        text = (rendered / "systemd" / service).read_text()

        granted = {
            d.removeprefix("ReadWritePaths=")
            for d in directives(text, "ReadWritePaths")
        }
        assert "/backup" not in granted
        assert "/" not in granted

    def test_retention_service_runs_as_the_configured_user(self, tmp_path):
        """The corpus is owned by whoever receives the rsync push."""
        output = render(tmp_path, {**RETAIN, "user": "postgres"})
        service, _timer = unit_names()
        text = (output / "systemd" / service).read_text()

        assert directives(text, "User") == ["User=postgres"]

    def test_the_user_defaults_to_the_deploy_user(self, tmp_path):
        output = render(tmp_path, RETAIN, user="deployer")
        service, _timer = unit_names()

        assert directives((output / "systemd" / service).read_text(), "User") == [
            "User=deployer"
        ]

    def test_the_service_is_oneshot(self, rendered):
        """A timer-activated prune that systemd thinks is long-running would
        be restarted, not scheduled."""
        service, _timer = unit_names()
        text = (rendered / "systemd" / service).read_text()

        assert "Type=oneshot" in text

    def test_the_service_has_no_install_section(self, rendered):
        """The timer is what gets enabled; a service with WantedBy= would run
        the prune once at boot as well as on schedule."""
        service, _timer = unit_names()
        text = (rendered / "systemd" / service).read_text()

        assert "[Install]" not in text

    def test_every_directive_sits_on_its_own_line(self, rendered):
        """A `{#-` comment eats the newline of the directive ABOVE it.

        Caught in development: `{#- … -#}` before ExecStart produced
        `Environment=PYTHONDONTWRITEBYTECODE=1ExecStart=/home/…`, one line
        systemd reads as a single malformed Environment= and an ExecStart
        that does not exist. The unit would install, the timer would fire,
        and the prune would never run — the exact shape this release is
        closing. trim_blocks/lstrip_blocks already drop a standalone
        comment line, so the `-` markers were pure downside.
        """
        service, timer = unit_names()
        for unit in (service, timer):
            for line in (rendered / "systemd" / unit).read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "[")):
                    continue
                key = stripped.split("=", 1)[0]
                assert key.isidentifier(), (
                    f"{unit}: {stripped!r} is not one directive — two were "
                    f"glued together by Jinja whitespace control"
                )


class TestRetentionTimer:
    def test_retention_timer_uses_the_configured_schedule_and_one_oncalendar(
        self, rendered
    ):
        """`OnCalendar=` accumulates — systemd resets the list only on an
        empty assignment, so two non-empty ones give two triggers (#311)."""
        _service, timer = unit_names()
        text = (rendered / "systemd" / timer).read_text()

        assert directives(text, "OnCalendar") == ["OnCalendar=*-*-* 05:30:00 UTC"]

    def test_retention_timer_is_persistent(self, rendered):
        """A host down at 05:30 still prunes on boot."""
        _service, timer = unit_names()
        text = (rendered / "systemd" / timer).read_text()

        assert "Persistent=true" in text

    def test_the_timer_is_wanted_by_timers_target(self, rendered):
        """Without it, `systemctl enable` has nothing to hook the timer onto."""
        _service, timer = unit_names()
        text = (rendered / "systemd" / timer).read_text()

        assert "[Install]" in text
        assert "WantedBy=timers.target" in text

    def test_the_timer_names_its_service(self, rendered):
        """Stem resolution is implicit; naming it is not more code, it is the
        difference between a rename being caught and being silent."""
        service, timer = unit_names()
        text = (rendered / "systemd" / timer).read_text()

        assert f"Unit={service}" in text


class TestRenderScope:
    def test_no_retain_config_renders_no_retention_units(self, tmp_path):
        """Purely additive: a config with no `retain:` renders what it did."""
        output = render(tmp_path)

        assert not list((output / "systemd").glob("*-retain-*"))

    def test_one_pair_per_entry(self, tmp_path):
        output = render(
            tmp_path,
            RETAIN,
            {**RETAIN, "dir": "/backup/staging", "name": "production-slim"},
        )

        assert len(list((output / "systemd").glob("*-retain-*"))) == 4

    def test_renderer_and_manifest_read_the_same_authority(self, tmp_path, monkeypatch):
        """Deferred from cycle 7.1 — it needs the renderer to exist.

        Monkeypatch the naming helper; the rendered filename and the
        manifest destination must move together. One fact, one authority:
        the drift #337 was filed for, pinned before it can happen here.
        """
        import fraisier.naming

        def renamed(project, env, entry):
            return (
                f"zz-{project}-{env}-{entry}.service",
                f"zz-{project}-{env}-{entry}.timer",
            )

        monkeypatch.setattr(fraisier.naming, "retention_unit_names", renamed)
        output = render(tmp_path, RETAIN)

        rendered_names = {p.name for p in (output / "systemd").glob("zz-*")}
        assert rendered_names == {
            "zz-myproj-development-production-full.service",
            "zz-myproj-development-production-full.timer",
        }, "the renderer did not read the authority"

        manifest = json.loads((output / "artifact-manifest.json").read_text())
        destinations = {
            a["destination"]
            for a in manifest["artifacts"]
            if a["destination"] and "zz-" in a["destination"]
        }
        assert destinations == {
            "/etc/systemd/system/zz-myproj-development-production-full.service",
            "/etc/systemd/system/zz-myproj-development-production-full.timer",
        }, "the manifest did not read the authority"


class TestHostGating:
    def test_retention_units_gate_on_env_active_not_scope_active(self, rendered):
        """Decision 4 and decision 7 meeting.

        A received corpus has no owning fraise — it arrives by rsync from
        somewhere else. Routing it through the fraise-keyed gate would mean
        inventing an owner for it, which is how a second host authority
        gets born.
        """
        manifest = json.loads((rendered / "artifact-manifest.json").read_text())
        retention = [
            a for a in manifest["artifacts"] if "-retain-" in (a["source"] or "")
        ]

        assert retention, "no retention artifacts in the manifest"
        for artifact in retention:
            assert artifact["fraise"] is None, (
                f"{artifact['source']} claims fraise {artifact['fraise']!r}; a "
                "received corpus has no owning fraise"
            )
            assert artifact["environment"] == "development"

    def test_install_sh_gates_them_with_env_active(self, rendered):
        """The gate guarding each retention install is the env-owned one.

        Checks the `if` line immediately above each `_install_artifact` for
        a retention unit, rather than searching a byte window: the unit
        names also appear in the hash-verification block, which sits among
        unrelated `_scope_active` guards.
        """
        service, timer = unit_names()
        lines = (rendered / "install.sh").read_text().splitlines()

        checked = 0
        for index, line in enumerate(lines):
            for unit in (service, timer):
                if f'_install_artifact "systemd/{unit}"' not in line:
                    continue
                gate = lines[index - 1].strip()
                assert gate == 'if _env_active "development"; then', (
                    f"{unit} is installed under {gate!r}; a received corpus "
                    f"has no owning fraise, so it must not route through "
                    f"_scope_active"
                )
                checked += 1
        assert checked == 2, f"expected 2 retention installs, found {checked}"

    def test_install_sh_gates_the_enable_too(self, rendered):
        """An ungated enable would start the timer on every host in the env."""
        _service, timer = unit_names()
        lines = (rendered / "install.sh").read_text().splitlines()

        (index,) = [
            i
            for i, line in enumerate(lines)
            if f"systemctl enable --now {timer}" in line
        ]
        preceding = [line.strip() for line in lines[max(0, index - 4) : index]]
        assert 'if _env_active "development"; then' in preceding


class TestTimerDisposition:
    def test_retention_units_are_classified(self, rendered):
        """With a retain entry the render does not raise.

        Before the classification existed this failed with
        `UndispositionedArtifacts`, which is the coverage assertion doing
        its job rather than an unrelated error.
        """
        manifest = json.loads((rendered / "artifact-manifest.json").read_text())
        by_source = {a["source"]: a for a in manifest["artifacts"]}
        service, timer = unit_names()

        assert by_source[f"systemd/{service}"]["disposition"] == "timer"
        assert by_source[f"systemd/{timer}"]["disposition"] == "timer"

    def test_timer_disposition_enables_the_unit(self, rendered):
        _service, timer = unit_names()
        install_sh = (rendered / "install.sh").read_text()

        assert f"systemctl enable --now {timer}" in install_sh

    def test_the_service_is_installed_before_the_timer_is_enabled(self, rendered):
        """Enabling a timer whose service is not yet on disk is backup.timer's
        bug with the ordering inverted."""
        service, timer = unit_names()
        install_sh = (rendered / "install.sh").read_text()

        service_copy = install_sh.index(f"systemd/{service}")
        enable = install_sh.index(f"systemctl enable --now {timer}")
        assert service_copy < enable, "the timer is enabled before its service lands"

    def test_daemon_reload_precedes_the_enable(self, rendered):
        """`enable --now` on a unit systemd has not re-read starts the old one."""
        _service, timer = unit_names()
        install_sh = (rendered / "install.sh").read_text()

        enable = install_sh.index(f"systemctl enable --now {timer}")
        reload_before = install_sh.rfind("systemctl daemon-reload", 0, enable)
        assert reload_before != -1, "no daemon-reload precedes the enable"

    def test_existing_timers_are_still_not_enabled(self, rendered):
        """Decision 3: TIMER enables ONLY the retention timers.

        `backup.timer` and `deploy-checker.timer` stay copied-and-inert.
        Enabling backup.timer on upgrade would start `backup.sh` — a legacy
        `pg_dump | gzip` writing to /var/backups/{project} — on every host
        that has ever run scaffold-install, as a side effect of a retention
        fix. That has its own blast radius and its own issue.

        If you are changing this, you are taking on that behaviour change
        for every existing host. Do it deliberately, in its own release.
        """
        install_sh = (rendered / "install.sh").read_text()

        for inert in ("backup.timer", "deploy-checker.timer"):
            assert f"systemctl enable --now {inert}" not in install_sh
            assert f"systemctl enable {inert}" not in install_sh

    def test_restore_staging_units_are_still_uninstalled(self, rendered):
        """The other declared gap keeps its classification too."""
        install_sh = (rendered / "install.sh").read_text()

        assert "systemctl enable --now restore-staging.timer" not in install_sh


class TestNoDeleteReachesTheProducer:
    def test_no_rsync_delete_flag_anywhere_in_the_rendered_tree(self, rendered):
        """Decision 9: deletion runs on the destination, never on the producer.

        A compromised sender key must not be able to erase the corpus it
        pushed. Asserted over the rendered artifacts rather than trusted,
        because the property is only worth anything if nothing reintroduces
        the flag later.
        """
        offenders = []
        for path in rendered.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(errors="ignore")
            if "--delete" in text:
                offenders.append(str(path.relative_to(rendered)))

        assert not offenders, f"rsync --delete* reached: {offenders}"


class TestDiagnostics:
    """Phase 8 — the fix #339 actually asks for.

    A corpus with no retention installed must be *reported*. The plan
    predicted `scaffold-diff` would need no change because it derives from
    `get_install_mapping()`, which derives from the manifest. These assert
    that rather than assuming it: if it had needed a change, that would
    have been a finding about the manifest, not about the diff.
    """

    def test_scaffold_diff_maps_the_retention_units(self, tmp_path):
        """The mapping scaffold-diff compares against carries them."""
        config = dict(_BASE)
        config["scaffold"] = {
            "deploy_user": "fraisier",
            "output_dir": str(tmp_path / "output"),
        }
        config["backup"] = {"environments": {"development": {"retain": [RETAIN]}}}
        path = tmp_path / "fraises.yaml"
        path.write_text(yaml.safe_dump(config))
        renderer = ScaffoldRenderer(FraisierConfig(path))
        renderer.render()

        mapping = renderer.get_install_mapping()
        service, timer = unit_names()

        assert mapping[f"systemd/{service}"].as_posix() == (
            f"/etc/systemd/system/{service}"
        )
        assert mapping[f"systemd/{timer}"].as_posix() == (
            f"/etc/systemd/system/{timer}"
        )

    def test_scaffold_diff_reports_a_missing_retention_unit(self, tmp_path):
        """A host that never installed the pair sees it as missing.

        This is the reported half of the incident: the retention unit the
        destination host was supposed to have was hand-written, lived in the
        consuming repo, and nothing checked it was there. Run through the
        real `compute_scaffold_diff`, whose install targets under
        /etc/systemd/system do not exist on a test machine.
        """
        from fraisier.scaffold.diff import compute_scaffold_diff

        config = dict(_BASE)
        config["scaffold"] = {
            "deploy_user": "fraisier",
            "output_dir": str(tmp_path / "output"),
        }
        config["backup"] = {"environments": {"development": {"retain": [RETAIN]}}}
        path = tmp_path / "fraises.yaml"
        path.write_text(yaml.safe_dump(config))

        diffs = compute_scaffold_diff(FraisierConfig(path))

        service, timer = unit_names()
        by_source = {d.generated_path: d for d in diffs}
        assert by_source[f"systemd/{service}"].status == "missing_installed"
        assert by_source[f"systemd/{timer}"].status == "missing_installed"

    def test_doctor_lists_retention_entries_with_install_state(self, tmp_path):
        """`doctor` names each corpus and whether its timer is installed."""
        from fraisier.scaffold.retention import retention_report

        config = dict(_BASE)
        config["scaffold"] = {
            "deploy_user": "fraisier",
            "output_dir": str(tmp_path / "output"),
        }
        config["backup"] = {"environments": {"development": {"retain": [RETAIN]}}}
        path = tmp_path / "fraises.yaml"
        path.write_text(yaml.safe_dump(config))
        renderer = ScaffoldRenderer(FraisierConfig(path))
        renderer.render()

        installed_root = tmp_path / "etc" / "systemd" / "system"
        installed_root.mkdir(parents=True)

        (report,) = retention_report(renderer, systemd_dir=installed_root)
        assert report.name == "production-full"
        assert report.dir == "/backup/production"
        assert report.environment == "development"
        assert report.timer_installed is False
        assert report.service_installed is False

    def test_doctor_reports_an_installed_pair_as_installed(self, tmp_path):
        from fraisier.scaffold.retention import retention_report

        config = dict(_BASE)
        config["scaffold"] = {
            "deploy_user": "fraisier",
            "output_dir": str(tmp_path / "output"),
        }
        config["backup"] = {"environments": {"development": {"retain": [RETAIN]}}}
        path = tmp_path / "fraises.yaml"
        path.write_text(yaml.safe_dump(config))
        renderer = ScaffoldRenderer(FraisierConfig(path))
        renderer.render()

        installed_root = tmp_path / "etc" / "systemd" / "system"
        installed_root.mkdir(parents=True)
        service, timer = unit_names()
        (installed_root / service).write_text("x")
        (installed_root / timer).write_text("x")

        (report,) = retention_report(renderer, systemd_dir=installed_root)
        assert report.timer_installed is True
        assert report.service_installed is True

    def test_no_retain_config_reports_nothing(self, tmp_path):
        from fraisier.scaffold.retention import retention_report

        config = dict(_BASE)
        config["scaffold"] = {
            "deploy_user": "fraisier",
            "output_dir": str(tmp_path / "output"),
        }
        path = tmp_path / "fraises.yaml"
        path.write_text(yaml.safe_dump(config))
        renderer = ScaffoldRenderer(FraisierConfig(path))
        renderer.render()

        assert retention_report(renderer, systemd_dir=tmp_path) == []
