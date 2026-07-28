"""Relocated install-tool cache dirs must be manifest-managed (#288).

The install-helper unit relocates every write-heavy tool dir under ``app_path``
so they stay inside the single ``ReadWritePaths`` root that ``ProtectSystem=strict``
allows. ``ReadWritePaths`` lifts systemd's sandbox but does not change ownership,
so the install user cannot create those dirs under a ``deploy_user``-owned root
unless provisioning creates them first.
"""

from __future__ import annotations

from pathlib import Path

from fraisier.config import FraisierConfig
from fraisier.manifest import ManagedPath, PathManifest, build_manifest
from fraisier.scaffold.renderer import ScaffoldRenderer

APP_PATH = "/var/www/prod"

_YAML = """
name: tp
scaffold:
  deploy_user: deployer
  output_dir: {output}
fraises:
  my_api:
    type: api
{install_block}
    environments:
      production:
        app_path: {app_path}
"""

_INSTALL_BLOCK = """    install:
      user: {user}
      command: [/usr/local/bin/uv, sync, --frozen]
"""

# Every path the install-helper unit points a tool cache or state dir at.
# Keep in step with core/install-helper.service.j2 — the drift test below
# is what enforces that.
RELOCATED = (
    ".cache",
    ".cache/uv",
    ".local",
    ".local/share",
    ".local/state",
    ".cargo",
    ".npm",
)


def _config(tmp_path, install_user: str | None) -> FraisierConfig:
    block = "" if install_user is None else _INSTALL_BLOCK.format(user=install_user)
    p = tmp_path / "fraises.yaml"
    p.write_text(
        _YAML.format(
            output=str(tmp_path / "output"), app_path=APP_PATH, install_block=block
        )
    )
    return FraisierConfig(p)


def _by_path(manifest: PathManifest) -> dict[str, ManagedPath]:
    return {str(mp.path): mp for mp in manifest.all_paths()}


class TestRelocatedCacheDirsRegistered:
    """build_manifest emits install_user-owned entries for the relocated dirs."""

    def test_all_relocated_dirs_present_and_owned_by_install_user(self, tmp_path):
        """Each relocated dir is registered under app_path, owned by install.user."""
        manifest = build_manifest(_config(tmp_path, "appuser"))
        paths = _by_path(manifest)

        for rel in RELOCATED:
            full = f"{APP_PATH}/{rel}"
            assert full in paths, f"{full} is not manifest-managed"
            assert paths[full].owner == "appuser"
            assert paths[full].group == "appuser"
            assert paths[full].mode == 0o755

    def test_relocated_dirs_carry_no_read_write_units(self, tmp_path):
        """The install-helper unit hardcodes ReadWritePaths=app_path.

        Listing units here would inject redundant ReadWritePaths lines into the
        deploy-service and webhook units via paths_for_unit().
        """
        manifest = build_manifest(_config(tmp_path, "appuser"))
        paths = _by_path(manifest)

        for rel in RELOCATED:
            assert paths[f"{APP_PATH}/{rel}"].read_write_units == ()


class TestLocalParentIsRegistered:
    """`.local` needs its own entry, or it is created root-owned.

    install.sh's _ensure_dir does `mkdir -p` then chowns only the LEAF, so
    registering `.local/share` alone leaves `.local` owned by root.
    """

    def test_local_parent_has_its_own_entry(self, tmp_path):
        """`app_path/.local` is registered in its own right."""
        manifest = build_manifest(_config(tmp_path, "appuser"))

        assert f"{APP_PATH}/.local" in _by_path(manifest)

    def test_local_sorts_before_its_children(self, tmp_path):
        """Depth ordering creates `.local` before `.local/share`."""
        manifest = build_manifest(_config(tmp_path, "appuser"))
        order = [str(mp.path) for mp in manifest.sorted_by_depth()]

        parent = order.index(f"{APP_PATH}/.local")
        for child in (".local/share", ".local/state"):
            assert parent < order.index(f"{APP_PATH}/{child}")

    def test_cache_sorts_before_cache_uv(self, tmp_path):
        """Same guarantee for the `.cache` / `.cache/uv` pair."""
        manifest = build_manifest(_config(tmp_path, "appuser"))
        order = [str(mp.path) for mp in manifest.sorted_by_depth()]

        assert order.index(f"{APP_PATH}/.cache") < order.index(f"{APP_PATH}/.cache/uv")


class TestOnlyWhenInstallUserDiffers:
    """The relocation only exists when the install-helper unit exists."""

    def test_no_entries_without_install_user(self, tmp_path):
        """No install section → no relocated dirs."""
        paths = _by_path(build_manifest(_config(tmp_path, None)))

        for rel in RELOCATED:
            assert f"{APP_PATH}/{rel}" not in paths

    def test_no_entries_when_install_user_equals_deploy_user(self, tmp_path):
        """install.user == deploy_user → no separate helper, no relocation."""
        paths = _by_path(build_manifest(_config(tmp_path, "deployer")))

        for rel in RELOCATED:
            assert f"{APP_PATH}/{rel}" not in paths


class TestUnitAndManifestDoNotDrift:
    """Every cache path the unit exports must be manifest-managed.

    The relocated-dir list lives in two places (the unit template and
    manifest.py). This test is what keeps them in step.
    """

    def test_every_env_path_under_app_path_is_managed(self, tmp_path):
        """Parse Environment= out of the rendered unit and check each path."""
        config = _config(tmp_path, "appuser")
        renderer = ScaffoldRenderer(config)
        entry = next(
            e
            for e in renderer.context["install_helper_sockets"]
            if e["fraise_name"] == "my_api"
        )
        unit = renderer.env.get_template("core/install-helper.service.j2").render(
            **renderer.context, **entry
        )

        exported = set()
        for line in unit.splitlines():
            if not line.startswith("Environment="):
                continue
            _, _, value = line.partition("=")[2].partition("=")
            if value.startswith(f"{APP_PATH}/"):
                exported.add(value)

        assert exported, "no relocated cache paths found in the rendered unit"

        managed = set(_by_path(build_manifest(config)))
        missing = sorted(p for p in exported if p not in managed)
        assert not missing, f"unit exports unmanaged paths: {missing}"

    def test_relocated_constant_matches_the_unit(self, tmp_path):
        """This test module's RELOCATED list is not stale either."""
        expected = {f"{APP_PATH}/{rel}" for rel in RELOCATED}
        managed = set(_by_path(build_manifest(_config(tmp_path, "appuser"))))

        assert expected <= managed


class TestInstallShCreatesTheDirs:
    """The manifest entries must reach the provisioning script."""

    def _install_sh(self, tmp_path) -> str:
        renderer = ScaffoldRenderer(_config(tmp_path, "appuser"))
        return renderer.env.get_template("core/install.sh.j2").render(
            **renderer.context
        )

    def test_ensure_dir_line_per_relocated_dir(self, tmp_path):
        """Each relocated dir gets an _ensure_dir call with the install user."""
        content = self._install_sh(tmp_path)

        for rel in RELOCATED:
            expected = f'_ensure_dir "{APP_PATH}/{rel}" "appuser" "appuser" "755"'
            assert expected in content, f"missing _ensure_dir for {rel}"

    def test_parent_dirs_created_before_children(self, tmp_path):
        """`.local` is chowned before `.local/share` exists under it.

        _ensure_dir chowns only the leaf, so ordering is what keeps `.local`
        from being left root-owned.
        """
        content = self._install_sh(tmp_path)

        parent = content.index(f'_ensure_dir "{APP_PATH}/.local"')
        for child in (".local/share", ".local/state"):
            assert parent < content.index(f'_ensure_dir "{APP_PATH}/{child}"')


class TestPathsAreRealPathObjects:
    """Guard against string/Path confusion in the new construction sites."""

    def test_entries_are_path_instances(self, tmp_path):
        """Every relocated entry stores a Path, not a str."""
        paths = _by_path(build_manifest(_config(tmp_path, "appuser")))

        for rel in RELOCATED:
            assert isinstance(paths[f"{APP_PATH}/{rel}"].path, Path)
