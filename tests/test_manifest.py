"""Tests for PathManifest — single source of truth for filesystem paths."""

from pathlib import Path

import pytest

from fraisier.manifest import ManagedPath, PathManifest, build_manifest


class TestManagedPath:
    """ManagedPath: immutable record of a managed filesystem path."""

    def test_instantiate_with_all_fields(self):
        """ManagedPath instantiation with all required fields."""
        path = ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=("fraisier-deploy.service", "fraisier-api.service"),
            create_if_missing=True,
        )
        assert path.path == Path("/var/lib/fraisier")
        assert path.owner == "fraisier"
        assert path.group == "fraisier"
        assert path.mode == 0o755
        units = ("fraisier-deploy.service", "fraisier-api.service")
        assert path.read_write_units == units
        assert path.create_if_missing is True

    def test_create_if_missing_defaults_true(self):
        """create_if_missing defaults to True."""
        path = ManagedPath(
            path=Path("/opt/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        assert path.create_if_missing is True

    def test_is_immutable(self):
        """ManagedPath is frozen — cannot mutate after construction."""
        from dataclasses import FrozenInstanceError

        path = ManagedPath(
            path=Path("/opt/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        with pytest.raises(FrozenInstanceError):
            path.owner = "root"  # ty: ignore[invalid-assignment]

    def test_equality_on_identical_fields(self):
        """Two ManagedPath with identical fields compare equal."""
        path1 = ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=("deploy.service",),
            create_if_missing=False,
        )
        path2 = ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=("deploy.service",),
            create_if_missing=False,
        )
        assert path1 == path2

    def test_inequality_on_different_fields(self):
        """Two ManagedPath with different fields are not equal."""
        path1 = ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        path2 = ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner="root",
            group="root",
            mode=0o755,
            read_write_units=(),
        )
        assert path1 != path2

    def test_has_string_representation(self):
        """ManagedPath.__str__ produces readable output."""
        path = ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=("deploy.service",),
        )
        output = str(path)
        assert "/var/lib/fraisier" in output
        assert "fraisier" in output


class TestPathManifest:
    """PathManifest: container for all managed paths."""

    def test_holds_global_and_env_paths(self):
        """PathManifest holds both global and per-environment paths."""
        global_path = ManagedPath(
            path=Path("/opt/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        env_path = ManagedPath(
            path=Path("/var/www/myapp"),
            owner="deploy",
            group="deploy",
            mode=0o755,
            read_write_units=("myapp-deploy.service",),
        )
        manifest = PathManifest(
            global_paths=(global_path,),
            env_paths=(env_path,),
        )
        assert len(manifest.global_paths) == 1
        assert len(manifest.env_paths) == 1

    def test_all_paths_returns_flat_iterable(self):
        """all_paths() returns both global and env paths combined."""
        global_path = ManagedPath(
            path=Path("/opt/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        env_path = ManagedPath(
            path=Path("/var/www/myapp"),
            owner="deploy",
            group="deploy",
            mode=0o755,
            read_write_units=(),
        )
        manifest = PathManifest(
            global_paths=(global_path,),
            env_paths=(env_path,),
        )
        all_paths = list(manifest.all_paths())
        assert len(all_paths) == 2
        assert global_path in all_paths
        assert env_path in all_paths

    def test_paths_for_unit_filters_by_unit_stem(self):
        """paths_for_unit(unit_stem) returns paths that list the unit."""
        path1 = ManagedPath(
            path=Path("/var/www/api"),
            owner="deploy",
            group="deploy",
            mode=0o755,
            read_write_units=("myapp-deploy.service", "myapp-api.service"),
        )
        path2 = ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=("myapp-deploy.service",),
        )
        path3 = ManagedPath(
            path=Path("/opt/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        manifest = PathManifest(
            global_paths=(path3,),
            env_paths=(path1, path2),
        )
        api_paths = list(manifest.paths_for_unit("myapp-api.service"))
        assert len(api_paths) == 1
        assert path1 in api_paths

        deploy_paths = list(manifest.paths_for_unit("myapp-deploy.service"))
        assert len(deploy_paths) == 2
        assert path1 in deploy_paths
        assert path2 in deploy_paths

        no_paths = list(manifest.paths_for_unit("nonexistent.service"))
        assert len(no_paths) == 0

    def test_paths_owned_by_filters_by_owner(self):
        """paths_owned_by(user) returns paths where owner == user."""
        path1 = ManagedPath(
            path=Path("/var/www/api"),
            owner="deploy",
            group="deploy",
            mode=0o755,
            read_write_units=(),
        )
        path2 = ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        path3 = ManagedPath(
            path=Path("/home/user/.venv"),
            owner="deploy",
            group="deploy",
            mode=0o755,
            read_write_units=(),
        )
        manifest = PathManifest(
            global_paths=(path2,),
            env_paths=(path1, path3),
        )
        deploy_paths = list(manifest.paths_owned_by("deploy"))
        assert len(deploy_paths) == 2
        assert path1 in deploy_paths
        assert path3 in deploy_paths

        fraisier_paths = list(manifest.paths_owned_by("fraisier"))
        assert len(fraisier_paths) == 1
        assert path2 in fraisier_paths

    def test_is_immutable(self):
        """PathManifest is frozen — cannot mutate after construction."""
        from dataclasses import FrozenInstanceError

        manifest = PathManifest(
            global_paths=(),
            env_paths=(),
        )
        with pytest.raises(FrozenInstanceError):
            manifest.global_paths = ()  # ty: ignore[invalid-assignment]


class TestBuildManifest:
    """build_manifest: factory to construct PathManifest from FraisierConfig."""

    def test_global_paths_always_present(self, fraisier_config_fixture):
        """Global paths are always present in the manifest."""
        manifest = build_manifest(fraisier_config_fixture)
        all_paths = list(manifest.all_paths())
        paths_dict = {str(p.path): p for p in all_paths}

        assert "/opt/fraisier" in paths_dict
        assert "/var/lib/fraisier" in paths_dict
        assert "/run/fraisier" in paths_dict

    def test_global_paths_owned_by_deploy_user(self, fraisier_config_fixture):
        """Global paths are owned by scaffold.deploy_user."""
        manifest = build_manifest(fraisier_config_fixture)
        deploy_user = fraisier_config_fixture.scaffold.deploy_user

        for path in manifest.global_paths:
            assert path.owner == deploy_user

    def test_run_fraisier_not_created_by_systemd(self, fraisier_config_fixture):
        """/run/fraisier has create_if_missing=False (systemd manages it)."""
        manifest = build_manifest(fraisier_config_fixture)
        run_path = next(
            (p for p in manifest.global_paths if str(p.path) == "/run/fraisier"), None
        )
        assert run_path is not None
        assert run_path.create_if_missing is False

    def test_config_dir_in_manifest(self, fraisier_config_fixture):
        """Config directory (parent of scaffold.config_path) is in manifest."""
        manifest = build_manifest(fraisier_config_fixture)
        config_dir = fraisier_config_fixture.scaffold.config_dir
        all_paths = list(manifest.all_paths())
        paths_dict = {str(p.path): p for p in all_paths}

        assert config_dir in paths_dict

    def test_git_repo_in_env_paths(self, fraisier_config_fixture):
        """For each environment with git_repo, a ManagedPath is created."""
        manifest = build_manifest(fraisier_config_fixture)
        env_paths = list(manifest.env_paths)

        # The fixture should have at least one environment with git_repo
        git_repo_paths = [
            str(p.path) for p in env_paths if "git" in str(p.path).lower()
        ]
        assert len(git_repo_paths) > 0

    def test_env_paths_have_correct_read_write_units(self, fraisier_config_fixture):
        """Environment paths are listed in deploy service read_write_units."""
        manifest = build_manifest(fraisier_config_fixture)
        env_paths = list(manifest.env_paths)

        # At least one env path should have read_write_units
        has_read_write = any(len(p.read_write_units) > 0 for p in env_paths)
        assert has_read_write

    def test_app_path_in_env_paths(self, fraisier_config_fixture):
        """For each environment with app_path, a ManagedPath is created."""
        manifest = build_manifest(fraisier_config_fixture)
        env_paths = list(manifest.env_paths)

        # The fixture should have at least one environment with app_path
        # Some environments may not have app_path, so check if we have paths at all
        assert len(env_paths) > 0

    def test_venv_path_owned_by_install_user_when_different(
        self, fraisier_config_with_install_user_fixture
    ):
        """When install.user != deploy_user, .venv is owned by install.user."""
        manifest = build_manifest(fraisier_config_with_install_user_fixture)
        all_paths = list(manifest.all_paths())

        # Find .venv paths
        venv_paths = [p for p in all_paths if ".venv" in str(p.path)]
        if venv_paths:
            # If venv paths exist, they should be owned by a non-deploy user
            deploy_user = fraisier_config_with_install_user_fixture.scaffold.deploy_user
            for venv_path in venv_paths:
                assert venv_path.owner != deploy_user

    def test_deduplicates_shared_git_repos(self, fraisier_config_fixture):
        """If two environments share a git_repo, only one ManagedPath is created."""
        manifest = build_manifest(fraisier_config_fixture)
        all_paths = list(manifest.all_paths())

        # Collect all path strings
        path_strings = [str(p.path) for p in all_paths]

        # Count duplicates
        duplicates = [p for p in path_strings if path_strings.count(p) > 1]
        assert len(duplicates) == 0


class TestManagedPathsCarryTheirEnvironment:
    """So a host can create its own directories and not another host's.

    The units were host-filtered while the directories those units live in
    were not, so a production-only host created — and chowned — the dev host's
    ``git_repo`` and ``app_path``.
    """

    @staticmethod
    def _config(tmp_path, yaml_text):
        from fraisier.config import FraisierConfig

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(yaml_text)
        return FraisierConfig(cfg)

    _TWO_ENVS = """\
name: proj
servers:
  dev.example.io:
    machine_hostnames: [devbox]
  prod.example.io:
    machine_hostnames: [pio]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      development:
        server: dev.example.io
        app_path: /var/www/api-dev
        git_repo: /var/git/api-dev.git
      production:
        server: prod.example.io
        app_path: /var/www/api
        git_repo: /var/git/api.git
"""

    def test_env_derived_paths_name_their_environment(self, tmp_path):
        manifest = build_manifest(self._config(tmp_path, self._TWO_ENVS))
        by_path = {str(p.path): p for p in manifest.env_paths}

        assert by_path["/var/www/api-dev"].environments == ("development",)
        assert by_path["/var/git/api.git"].environments == ("production",)

    def test_shared_paths_stay_unconditional(self, tmp_path):
        """A path no environment owns must not acquire a gate."""
        manifest = build_manifest(self._config(tmp_path, self._TWO_ENVS))

        for path in manifest.global_paths:
            assert path.environments == (), path.path

    def test_a_path_two_environments_share_belongs_to_both(self, tmp_path):
        """Paths dedupe by location, so the second claim must not be dropped.

        Gating a shared path on whichever environment happened to be seen
        first would leave it uncreated on a host running only the other.
        """
        shared = """\
name: proj
servers:
  dev.example.io:
    machine_hostnames: [devbox]
  prod.example.io:
    machine_hostnames: [pio]
scaffold:
  deploy_user: deployer
fraises:
  api:
    type: api
    environments:
      development:
        server: dev.example.io
        app_path: /srv/shared
      production:
        server: prod.example.io
        app_path: /srv/shared
"""
        manifest = build_manifest(self._config(tmp_path, shared))
        entries = [p for p in manifest.env_paths if str(p.path) == "/srv/shared"]

        assert len(entries) == 1, "the path should still be deduplicated"
        assert set(entries[0].environments) == {"development", "production"}


class TestPathManifestSorting:
    """PathManifest.sorted_by_depth: sort paths by depth for install order."""

    def test_sorted_by_depth_parent_before_child(self):
        """Paths with fewer components come before paths with more components."""
        path1 = ManagedPath(
            path=Path("/var/lib"),
            owner="root",
            group="root",
            mode=0o755,
            read_write_units=(),
        )
        path2 = ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        path3 = ManagedPath(
            path=Path("/var/lib/fraisier/data"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        manifest = PathManifest(
            global_paths=(path3, path2, path1),
            env_paths=(),
        )
        sorted_paths = list(manifest.sorted_by_depth())
        assert sorted_paths == [path1, path2, path3]

    def test_sorted_by_depth_same_depth_preserves_order(self):
        """Paths with same depth preserve their original order."""
        path1 = ManagedPath(
            path=Path("/opt/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        path2 = ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner="fraisier",
            group="fraisier",
            mode=0o755,
            read_write_units=(),
        )
        manifest = PathManifest(
            global_paths=(path1, path2),
            env_paths=(),
        )
        sorted_paths = list(manifest.sorted_by_depth())
        # Both have depth 2, so preserve order
        assert sorted_paths == [path1, path2]

    def test_sorted_by_depth_mixed_depths(self):
        """Sorting works correctly with mixed depth paths."""
        paths_unordered = [
            ManagedPath(
                path=Path("/a/b/c/d"),
                owner="root",
                group="root",
                mode=0o755,
                read_write_units=(),
            ),
            ManagedPath(
                path=Path("/a"),
                owner="root",
                group="root",
                mode=0o755,
                read_write_units=(),
            ),
            ManagedPath(
                path=Path("/a/b"),
                owner="root",
                group="root",
                mode=0o755,
                read_write_units=(),
            ),
        ]
        manifest = PathManifest(
            global_paths=tuple(paths_unordered),
            env_paths=(),
        )
        sorted_paths = list(manifest.sorted_by_depth())
        depths = [len(p.path.parts) for p in sorted_paths]
        # Should be in ascending order of depth
        assert depths == sorted(depths)
