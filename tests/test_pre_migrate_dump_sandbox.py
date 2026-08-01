"""The dump gate's output_dir must reach the webhook unit's sandbox (#317).

`database.pre_migrate_dump` (v0.52.0) makes the deploy take a verified pg_dump
before applying migrations, and aborts if it cannot. The generated webhook unit
runs `ProtectSystem=strict` with a `ReadWritePaths=` list that never included
the dump directory — so on every strict install the dump failed with
`Read-only file system` and **every deploy with pending migrations failed
closed**.

Invisible to preflight: the path is writable from a login shell, and only a
write from inside the unit's sandbox reveals it. First signal was a failed
production deploy (printoptim.io, 2026-08-01).

Note the key is `output_dir`, not `dir` as the issue text says.
"""

from __future__ import annotations

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_YAML = """
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {output}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        git_repo: /var/git/api.git
        database:
          name: myapp_prod
          strategy: migrate
{dump}
"""

_DUMP_BLOCK = """          pre_migrate_dump:
            enabled: true
            output_dir: /var/backups/myproj/pre_migrate
"""


def _unit(tmp_path, dump: str) -> str:
    p = tmp_path / "fraises.yaml"
    p.write_text(_YAML.format(output=str(tmp_path / "out"), dump=dump))
    ScaffoldRenderer(FraisierConfig(p)).render()
    return (tmp_path / "out" / "fraisier-myproj-webhook.service").read_text()


def _rw_paths(text: str) -> list[str]:
    return [
        ln.split("=", 1)[1].strip()
        for ln in text.splitlines()
        if ln.startswith("ReadWritePaths=")
    ]


class TestDumpDirIsWritableFromTheSandbox:
    def test_output_dir_appears_in_readwritepaths(self, tmp_path):
        paths = _rw_paths(_unit(tmp_path, _DUMP_BLOCK))

        assert "/var/backups/myproj/pre_migrate" in paths, (
            f"dump dir not in the sandbox allowlist: {paths}"
        )

    def test_the_unit_is_still_strict(self, tmp_path):
        """The fix must widen the allowlist, not disable the sandbox."""
        text = _unit(tmp_path, _DUMP_BLOCK)

        assert "ProtectSystem=strict" in text

    def test_existing_paths_are_retained(self, tmp_path):
        """Widening must not drop the state dirs or the app/git trees."""
        paths = _rw_paths(_unit(tmp_path, _DUMP_BLOCK))

        for expected in ("/opt/fraisier", "/var/lib/fraisier", "/run/fraisier"):
            assert expected in paths

    def test_nothing_added_when_the_gate_is_absent(self, tmp_path):
        """No dump config, no extra sandbox hole."""
        paths = _rw_paths(_unit(tmp_path, ""))

        assert not any("backups" in p for p in paths)

    def test_nothing_added_when_the_gate_is_disabled(self, tmp_path):
        """enabled: false must not widen the sandbox either."""
        disabled = _DUMP_BLOCK.replace("enabled: true", "enabled: false")
        paths = _rw_paths(_unit(tmp_path, disabled))

        assert not any("backups" in p for p in paths)

    def test_no_duplicate_entries(self, tmp_path):
        paths = _rw_paths(_unit(tmp_path, _DUMP_BLOCK))

        assert len(paths) == len(set(paths)), f"duplicate ReadWritePaths: {paths}"


class TestDoctorCatchesItBeforeADeployDoes:
    """Re-scaffolding is what applies the fix; nothing told you that.

    An operator who upgrades but does not re-run `scaffold-install` keeps the
    old unit and keeps failing closed. This check reads the *installed* unit,
    so it catches exactly that gap.
    """

    def _run(self, cfg):
        from fraisier import doctor

        return doctor.DOCTOR_CHECKS["pre_migrate_dump_writable"].fn(cfg)

    def _cfg(self, tmp_path, enabled=True, output_dir="/var/backups/x"):
        p = tmp_path / "fraises.yaml"
        p.write_text(f"""
name: myproj
scaffold:
  deploy_user: fraisier
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        database:
          name: db
          strategy: migrate
          pre_migrate_dump:
            enabled: {str(enabled).lower()}
            output_dir: {output_dir}
""")
        return FraisierConfig(p)

    def test_registered(self):
        from fraisier import doctor

        assert "pre_migrate_dump_writable" in doctor.DOCTOR_CHECKS

    def test_skips_when_gate_not_enabled(self, tmp_path):
        assert self._run(self._cfg(tmp_path, enabled=False)).status == "skip"

    def test_skips_without_config(self):
        assert self._run(None).status == "skip"

    def test_warns_when_installed_unit_omits_the_dir(self, tmp_path, monkeypatch):
        unit = tmp_path / "webhook.service"
        unit.write_text(
            "[Service]\nProtectSystem=strict\nReadWritePaths=/opt/fraisier\n"
        )
        monkeypatch.setattr("fraisier.doctor._installed_webhook_unit", lambda _p: unit)

        result = self._run(self._cfg(tmp_path))

        assert result.status == "warn"
        assert "/var/backups/x" in result.detail
        assert result.fix_hint is not None
        assert "scaffold" in result.fix_hint

    def test_passes_when_installed_unit_lists_the_dir(self, tmp_path, monkeypatch):
        unit = tmp_path / "webhook.service"
        unit.write_text(
            "[Service]\nProtectSystem=strict\n"
            "ReadWritePaths=/opt/fraisier\nReadWritePaths=/var/backups/x\n"
        )
        monkeypatch.setattr("fraisier.doctor._installed_webhook_unit", lambda _p: unit)

        assert self._run(self._cfg(tmp_path)).status == "pass"

    def test_skips_when_the_unit_is_not_installed(self, tmp_path, monkeypatch):
        """A dev machine has no unit — that is not a finding."""
        monkeypatch.setattr(
            "fraisier.doctor._installed_webhook_unit",
            lambda _p: tmp_path / "nope.service",
        )

        assert self._run(self._cfg(tmp_path)).status == "skip"

    def test_no_warning_when_sandbox_is_not_strict(self, tmp_path, monkeypatch):
        """Without ProtectSystem=strict the allowlist does not gate writes."""
        unit = tmp_path / "webhook.service"
        unit.write_text("[Service]\nReadWritePaths=/opt/fraisier\n")
        monkeypatch.setattr("fraisier.doctor._installed_webhook_unit", lambda _p: unit)

        assert self._run(self._cfg(tmp_path)).status == "pass"
