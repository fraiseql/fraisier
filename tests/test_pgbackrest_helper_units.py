"""The pgBackRest helper's units: rendered, installed, and gated per fraise (#424).

One socket-activated root helper per ``(fraise, environment)`` that restores from
pgBackRest, with the stanza, repository, cluster and target **baked into
``ExecStart``**.  They are not read from ``fraises.yaml`` at run time: that file
sits under a directory the deploy user owns, and a root daemon that trusted it
would let anyone who could write it retarget a cluster restore.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from fraisier.config import FraisierConfig
from fraisier.naming import pgbackrest_helper_socket_path, pgbackrest_helper_unit_names
from fraisier.scaffold.renderer import ScaffoldRenderer

PROJECT = "myproj"
PGBACKREST = {"stanza": "main", "repo": 1, "cluster": "18/staging", "target": "latest"}


def render(tmp_path, restore: dict | None, *, user: str = "fraisier") -> Any:
    database: dict = {
        "strategy": "restore_migrate",
        "name": "app",
        "admin_url": "postgresql://postgres@localhost:5433/postgres",
    }
    if restore is not None:
        database["restore"] = restore
    config = {
        "name": PROJECT,
        "scaffold": {"deploy_user": user, "output_dir": str(tmp_path / "output")},
        "fraises": {
            "api": {
                "type": "api",
                "environments": {
                    "staging": {
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
    return render(tmp_path, {"source": "pgbackrest", "pgbackrest": PGBACKREST})


def names() -> tuple[str, str]:
    return pgbackrest_helper_unit_names(PROJECT, "api", "staging")


def directives(text: str, key: str) -> list[str]:
    return [
        line.strip() for line in text.splitlines() if line.strip().startswith(f"{key}=")
    ]


def helper_units(output) -> list[str]:
    return sorted(p.name for p in (output / "systemd").glob("*pgbackrest-helper*"))


class TestRenderScope:
    def test_a_pgbackrest_environment_gets_a_socket_and_a_service(self, rendered):
        assert helper_units(rendered) == sorted(names())

    @pytest.mark.parametrize(
        "restore",
        [None, {"backup_dir": "/backup"}, {"source": "dump", "backup_dir": "/backup"}],
        ids=["no restore", "implicit dump", "explicit dump"],
    )
    def test_the_dump_source_renders_no_helper(self, tmp_path, restore):
        """Nothing changes for an existing config."""
        assert helper_units(render(tmp_path, restore)) == []


class TestService:
    def exec_start(self, rendered) -> str:
        (line,) = directives(
            (rendered / "systemd" / names()[1]).read_text(), "ExecStart"
        )
        return line

    def test_it_runs_as_root_so_it_can_stop_and_start_a_cluster(self, rendered):
        text = (rendered / "systemd" / names()[1]).read_text()

        assert directives(text, "User") == []

    def test_the_whole_spec_is_baked_into_execstart(self, rendered):
        line = self.exec_start(rendered)

        assert "fraisier-pgbackrest-helper" in line
        assert "--deploy-user fraisier" in line
        assert "--stanza main" in line
        assert "--repo 1" in line
        assert "--cluster 18/staging" in line
        assert "--target latest" in line
        assert "--timeout 21600" in line

    def test_a_timestamp_target_and_a_timeout_are_baked_too(self, tmp_path):
        output = render(
            tmp_path,
            {
                "source": "pgbackrest",
                "pgbackrest": {
                    **PGBACKREST,
                    "target": "2026-10-01 14:18:37+00",
                    "timeout_seconds": 7200,
                },
            },
        )

        (line,) = directives((output / "systemd" / names()[1]).read_text(), "ExecStart")
        assert (
            "--target '2026-10-01 14:18:37+00'" in line
            or '--target "2026-10-01' in line
        )
        assert "--timeout 7200" in line

    def test_the_helper_is_not_given_the_config_file_to_read(self, rendered):
        """No ``--config``: it does not read ``fraises.yaml`` when it runs."""
        assert "--config" not in self.exec_start(rendered)

    def test_no_protecthome_because_the_binary_lives_under_home(self, rendered):
        for unit in names():
            text = (rendered / "systemd" / unit).read_text()
            assert directives(text, "ProtectHome") == []

    def test_it_is_hardened_as_far_as_a_cluster_restore_allows(self, rendered):
        text = (rendered / "systemd" / names()[1]).read_text()

        for directive in (
            "NoNewPrivileges=true",
            "PrivateDevices=true",
            "ProtectKernelModules=true",
            "ProtectControlGroups=true",
        ):
            assert directive in text

    def test_every_directive_sits_on_its_own_line(self, rendered):
        for unit in names():
            for line in (rendered / "systemd" / unit).read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "[")):
                    continue
                assert stripped.split("=", 1)[0].isidentifier(), (
                    f"{unit}: {stripped!r} is not one directive"
                )

    def test_the_socket_only_activates_for_the_deploy_user(self, rendered):
        text = (rendered / "systemd" / names()[0]).read_text()

        assert directives(text, "ListenStream") == [
            f"ListenStream={pgbackrest_helper_socket_path(PROJECT, 'api', 'staging')}"
        ]
        assert directives(text, "SocketUser") == ["SocketUser=fraisier"]
        assert directives(text, "SocketMode") == ["SocketMode=0600"]


class TestInstallation:
    def manifest(self, rendered) -> list[dict]:
        data = json.loads((rendered / "artifact-manifest.json").read_text())
        return [
            a for a in data["artifacts"] if "pgbackrest-helper" in (a["source"] or "")
        ]

    def test_both_units_are_classified_as_the_pgbackrest_helper(self, rendered):
        artifacts = self.manifest(rendered)

        assert len(artifacts) == 2
        assert {a["disposition"] for a in artifacts} == {"pgbackrest_helper"}

    def test_they_are_gated_on_the_owning_fraise_and_environment(self, rendered):
        for artifact in self.manifest(rendered):
            assert (artifact["fraise"], artifact["environment"]) == ("api", "staging")

    def test_install_sh_rebakes_it_in_the_helper_sequence_under_scope_active(
        self, rendered
    ):
        lines = (rendered / "install.sh").read_text().splitlines()
        socket_unit, service_unit = names()

        (marker,) = [
            i for i, line in enumerate(lines) if "Installing pgBackRest helper" in line
        ]
        assert lines[marker - 1].strip() == 'if _scope_active "api" "staging"; then'
        block = "\n".join(lines[marker : marker + 10])
        # #279's sequence: the running .service holds the OLD argv, so it is
        # stopped and the socket restarted rather than `enable --now`-ed.
        assert block.index("daemon-reload") < block.index(f"stop {service_unit}")
        assert block.index(f"stop {service_unit}") < block.index(
            f"enable {socket_unit}"
        )
        assert block.index(f"enable {socket_unit}") < block.index(
            f"restart {socket_unit}"
        )

    def test_the_rebake_is_strict(self, rendered):
        """A half-applied re-bake leaves a root helper with a stale target."""
        text = (rendered / "install.sh").read_text()
        socket_unit, service_unit = names()

        assert f"_run_strict sudo systemctl stop {service_unit}" in text
        assert f"_run_strict sudo systemctl restart {socket_unit}" in text

    def test_renderer_and_manifest_read_one_naming_authority(
        self, tmp_path, monkeypatch
    ):
        import fraisier.naming

        def renamed(project, fraise, env):
            return (
                f"zz-{project}-{fraise}-{env}.socket",
                f"zz-{project}-{fraise}-{env}.service",
            )

        monkeypatch.setattr(fraisier.naming, "pgbackrest_helper_unit_names", renamed)
        output = render(tmp_path, {"source": "pgbackrest", "pgbackrest": PGBACKREST})

        assert {p.name for p in (output / "systemd").glob("zz-*")} == {
            "zz-myproj-api-staging.socket",
            "zz-myproj-api-staging.service",
        }
        manifest = json.loads((output / "artifact-manifest.json").read_text())
        assert {
            a["destination"]
            for a in manifest["artifacts"]
            if a["destination"] and "zz-" in a["destination"]
        } == {
            "/etc/systemd/system/zz-myproj-api-staging.socket",
            "/etc/systemd/system/zz-myproj-api-staging.service",
        }

    def test_the_client_looks_for_the_socket_the_unit_listens_on(self):
        """One authority: the unit's ListenStream= and the client's connect path."""
        assert str(pgbackrest_helper_socket_path(PROJECT, "api", "staging")) == (
            "/run/fraisier/pgbackrest-myproj-api-staging.sock"
        )


class TestDoctorSeesIt:
    """A pgBackRest refresh needs the helper installed *and* listening.

    The helper exists on a host only after ``scaffold-install``, and a refresh
    that finds no socket stops the service for nothing — so this is told before
    the nightly timer finds out.
    """

    def config(self, tmp_path):
        render(tmp_path, {"source": "pgbackrest", "pgbackrest": PGBACKREST})
        return FraisierConfig(tmp_path / "fraises.yaml")

    def install(self, tmp_path, *, units: bool, socket: bool):
        systemd = tmp_path / "etc-systemd"
        systemd.mkdir()
        run = tmp_path / "run"
        run.mkdir()
        if units:
            for unit in names():
                (systemd / unit).write_text("x")
        if socket:
            (run / "pgbackrest.sock").write_text("")
        return systemd, run / "pgbackrest.sock"

    def check(self, tmp_path, monkeypatch, *, units: bool, socket: bool):
        from fraisier import doctor

        config = self.config(tmp_path)
        systemd, sock = self.install(tmp_path, units=units, socket=socket)
        monkeypatch.setattr("fraisier.scaffold.retention.SYSTEMD_DIR", str(systemd))
        monkeypatch.setattr(
            "fraisier.naming.pgbackrest_helper_socket_path", lambda *_a: sock
        )
        return doctor._check_pgbackrest_helper(config)

    def test_no_pgbackrest_source_means_nothing_to_check(self, tmp_path):
        from fraisier import doctor

        render(tmp_path, {"backup_dir": "/backup"})

        result = doctor._check_pgbackrest_helper(
            FraisierConfig(tmp_path / "fraises.yaml")
        )

        assert result.status == "skip"

    def test_a_missing_unit_warns_and_names_the_remedy(self, tmp_path, monkeypatch):
        result = self.check(tmp_path, monkeypatch, units=False, socket=False)

        assert result.status == "warn"
        assert "api/staging" in result.detail
        assert "scaffold-install" in (result.fix_hint or "")

    def test_installed_but_not_listening_warns_differently(self, tmp_path, monkeypatch):
        result = self.check(tmp_path, monkeypatch, units=True, socket=False)

        assert result.status == "warn"
        assert "not listening" in result.detail
        assert "systemctl" in (result.fix_hint or "")

    def test_installed_and_listening_passes(self, tmp_path, monkeypatch):
        result = self.check(tmp_path, monkeypatch, units=True, socket=True)

        assert result.status == "pass"

    def test_it_is_registered_and_documented(self):
        from pathlib import Path

        from fraisier import doctor

        assert "pgbackrest_helper" in doctor.DOCTOR_CHECKS
        docs = (Path(__file__).parent.parent / "docs" / "doctor.md").read_text()
        assert "| `pgbackrest_helper` |" in docs
