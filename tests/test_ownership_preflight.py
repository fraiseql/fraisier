"""Tests for manifest-driven ownership preflight verification."""

import logging
from pathlib import Path
from unittest.mock import Mock, patch

from fraisier.manifest import ManagedPath, PathManifest


def _make_managed_path(
    path: str, owner: str, group: str, *, reconcile: bool = True
) -> ManagedPath:
    """Helper to create a ManagedPath.

    ``reconcile`` defaults to True here — the opposite of the dataclass default —
    because most cases in this module predate the flag and assert the deletion
    behaviour that is now opt-in.
    """
    return ManagedPath(
        path=Path(path),
        owner=owner,
        group=group,
        mode=0o755,
        read_write_units=(),
        reconcile_ownership=reconcile,
    )


class TestVerifyManifestOwnership:
    """_verify_manifest_ownership deletes paths owned by wrong user."""

    def test_path_owned_by_wrong_user_deleted(self, caplog):
        """Path owned by wrong user is deleted and logged."""
        from fraisier.deployers.preflight_ownership import (
            _verify_manifest_ownership,
        )

        # Mock manifest with one path
        manifest = PathManifest(
            global_paths=(_make_managed_path("/var/www/app", "appuser", "appuser"),),
            env_paths=(),
        )

        with (
            patch.object(Path, "stat") as mock_stat,
            patch(
                "fraisier.deployers.preflight_ownership.shutil.rmtree"
            ) as mock_rmtree,
            patch(
                "fraisier.deployers.preflight_ownership.pwd.getpwuid"
            ) as mock_getpwuid,
            patch.object(Path, "exists", return_value=True),
        ):
            # Mock stat to return a stat_result with st_uid for "deployer" user
            deployer_uid = 1001
            mock_stat_result = Mock()
            mock_stat_result.st_uid = deployer_uid
            mock_stat.return_value = mock_stat_result
            mock_getpwuid.return_value.pw_name = "deployer"

            with caplog.at_level(logging.INFO):
                _verify_manifest_ownership(manifest)

            # Should delete the path
            mock_rmtree.assert_called_once()
            # Should log the deletion
            assert "removing for recreation" in caplog.text

    def test_path_owned_by_correct_user_not_deleted(self, caplog):
        """Path owned by correct user is not deleted."""
        from fraisier.deployers.preflight_ownership import (
            _verify_manifest_ownership,
        )

        manifest = PathManifest(
            global_paths=(_make_managed_path("/var/www/app", "appuser", "appuser"),),
            env_paths=(),
        )

        with (
            patch.object(Path, "stat") as mock_stat,
            patch(
                "fraisier.deployers.preflight_ownership.shutil.rmtree"
            ) as mock_rmtree,
            patch(
                "fraisier.deployers.preflight_ownership.pwd.getpwuid"
            ) as mock_getpwuid,
            patch.object(Path, "exists", return_value=True),
        ):
            # Mock stat to return appuser's UID
            appuser_uid = 1000
            mock_stat_result = Mock()
            mock_stat_result.st_uid = appuser_uid
            mock_stat.return_value = mock_stat_result
            mock_getpwuid.return_value.pw_name = "appuser"

            _verify_manifest_ownership(manifest)

            # Should NOT delete the path
            mock_rmtree.assert_not_called()

    def test_missing_path_not_checked(self):
        """Path that does not exist is skipped."""
        from fraisier.deployers.preflight_ownership import (
            _verify_manifest_ownership,
        )

        manifest = PathManifest(
            global_paths=(
                _make_managed_path("/var/www/missing", "appuser", "appuser"),
            ),
            env_paths=(),
        )

        with (
            patch.object(Path, "exists", return_value=False),
            patch(
                "fraisier.deployers.preflight_ownership.shutil.rmtree"
            ) as mock_rmtree,
        ):
            _verify_manifest_ownership(manifest)

            # Should NOT try to delete
            mock_rmtree.assert_not_called()

    def test_stat_failure_logged_not_raised(self, caplog):
        """OSError on stat() is logged, not raised."""
        from fraisier.deployers.preflight_ownership import (
            _verify_manifest_ownership,
        )

        manifest = PathManifest(
            global_paths=(_make_managed_path("/var/www/app", "appuser", "appuser"),),
            env_paths=(),
        )

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "stat", side_effect=OSError("Permission denied")),
            patch(
                "fraisier.deployers.preflight_ownership.shutil.rmtree"
            ) as mock_rmtree,
        ):
            with caplog.at_level(logging.WARNING):
                # Should not raise
                _verify_manifest_ownership(manifest)

            # Should NOT delete
            mock_rmtree.assert_not_called()
            # Should log warning
            assert "Could not stat" in caplog.text

    def test_unknown_uid_logged_and_deleted(self, caplog):
        """Unknown UID (KeyError from pwd.getpwuid) is logged, path deleted."""
        from fraisier.deployers.preflight_ownership import (
            _verify_manifest_ownership,
        )

        manifest = PathManifest(
            global_paths=(_make_managed_path("/var/www/app", "appuser", "appuser"),),
            env_paths=(),
        )

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "stat") as mock_stat,
            patch(
                "fraisier.deployers.preflight_ownership.pwd.getpwuid",
                side_effect=KeyError(9999),
            ),
            patch(
                "fraisier.deployers.preflight_ownership.shutil.rmtree"
            ) as mock_rmtree,
        ):
            mock_stat_result = Mock()
            mock_stat_result.st_uid = 9999
            mock_stat.return_value = mock_stat_result

            with caplog.at_level(logging.WARNING):
                _verify_manifest_ownership(manifest)

            # Should log warning
            assert "Could not stat" in caplog.text
            # Should NOT delete (safer: we skipped it due to error)
            mock_rmtree.assert_not_called()

    def test_multiple_paths_mixed_ownership(self, caplog):
        """Multiple paths: some wrong owner deleted, some correct owner skipped."""
        from fraisier.deployers.preflight_ownership import (
            _verify_manifest_ownership,
        )

        manifest = PathManifest(
            global_paths=(
                _make_managed_path("/var/www/app1", "appuser", "appuser"),
                _make_managed_path("/var/www/app2", "appuser", "appuser"),
            ),
            env_paths=(),
        )

        # Create a side effect that tracks which paths have been called
        calls = {}

        def stat_side_effect(self):
            path_str = str(self)
            if "/app1" in path_str:
                stat_result = Mock()
                stat_result.st_uid = 1001  # deployer
                calls["/app1"] = stat_result
                return stat_result
            else:
                stat_result = Mock()
                stat_result.st_uid = 1000  # appuser
                calls["/app2"] = stat_result
                return stat_result

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "stat", stat_side_effect),
            patch(
                "fraisier.deployers.preflight_ownership.pwd.getpwuid"
            ) as mock_getpwuid,
            patch(
                "fraisier.deployers.preflight_ownership.shutil.rmtree"
            ) as mock_rmtree,
        ):
            # Map UID to name
            def getpwuid_side_effect(uid):
                result = Mock()
                result.pw_name = "deployer" if uid == 1001 else "appuser"
                return result

            mock_getpwuid.side_effect = getpwuid_side_effect

            _verify_manifest_ownership(manifest)

            # Should delete one path (wrong owner)
            assert mock_rmtree.call_count == 1


class TestNonRegenerablePathsAreNotDeleted:
    """A wrong owner on a path no deploy step recreates must not be deleted.

    `_verify_manifest_ownership` runs inside `_install_dependencies`, i.e. AFTER
    the git checkout. Deleting `app_path` there destroys the tree that was just
    checked out and leaves the install command running in a directory that no
    longer exists — nothing in the deploy path recreates it. Only the venv is
    regenerable, so only the venv may be deleted.
    """

    def _run(self, manifest):
        from fraisier.deployers.preflight_ownership import _verify_manifest_ownership

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "stat") as mock_stat,
            patch(
                "fraisier.deployers.preflight_ownership.shutil.rmtree"
            ) as mock_rmtree,
            patch(
                "fraisier.deployers.preflight_ownership.pwd.getpwuid"
            ) as mock_getpwuid,
        ):
            stat_result = Mock()
            stat_result.st_uid = 1001
            mock_stat.return_value = stat_result
            mock_getpwuid.return_value.pw_name = "deployer"

            error = None
            try:
                _verify_manifest_ownership(manifest)
            except Exception as exc:
                error = exc
            return error, mock_rmtree

    def test_wrong_owner_raises_instead_of_deleting(self):
        """reconcile_ownership=False → DeploymentError, and nothing removed."""
        from fraisier.errors import DeploymentError

        manifest = PathManifest(
            global_paths=(
                _make_managed_path(
                    "/var/www/app", "appuser", "appuser", reconcile=False
                ),
            ),
            env_paths=(),
        )

        error, mock_rmtree = self._run(manifest)

        assert isinstance(error, DeploymentError), f"expected raise, got {error!r}"
        mock_rmtree.assert_not_called()

    def test_error_names_path_actual_and_expected_owner(self):
        """The operator must be able to fix it without reading the source."""
        manifest = PathManifest(
            global_paths=(
                _make_managed_path(
                    "/var/www/app", "appuser", "appuser", reconcile=False
                ),
            ),
            env_paths=(),
        )

        error, _ = self._run(manifest)

        message = str(error)
        assert "/var/www/app" in message
        assert "deployer" in message, "should name the actual owner"
        assert "appuser" in message, "should name the expected owner"

    def test_regenerable_path_still_deleted(self):
        """reconcile_ownership=True keeps the venv self-heal working."""
        manifest = PathManifest(
            global_paths=(
                _make_managed_path(
                    "/var/www/app/.venv", "appuser", "appuser", reconcile=True
                ),
            ),
            env_paths=(),
        )

        error, mock_rmtree = self._run(manifest)

        assert error is None
        mock_rmtree.assert_called_once()

    def test_correct_owner_never_raises(self):
        """A non-reconciled path with the right owner is simply left alone."""
        from fraisier.deployers.preflight_ownership import _verify_manifest_ownership

        manifest = PathManifest(
            global_paths=(
                _make_managed_path(
                    "/var/www/app", "appuser", "appuser", reconcile=False
                ),
            ),
            env_paths=(),
        )

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "stat") as mock_stat,
            patch(
                "fraisier.deployers.preflight_ownership.shutil.rmtree"
            ) as mock_rmtree,
            patch(
                "fraisier.deployers.preflight_ownership.pwd.getpwuid"
            ) as mock_getpwuid,
        ):
            stat_result = Mock()
            stat_result.st_uid = 1000
            mock_stat.return_value = stat_result
            mock_getpwuid.return_value.pw_name = "appuser"

            _verify_manifest_ownership(manifest)

            mock_rmtree.assert_not_called()


class TestManifestDefaultsAreSafe:
    """The dataclass default must be the non-destructive one."""

    def test_reconcile_ownership_defaults_false(self):
        """A ManagedPath added later fails safe rather than becoming destructive."""
        path = ManagedPath(
            path=Path("/opt/fraisier"),
            owner="deployer",
            group="deployer",
            mode=0o755,
            read_write_units=(),
        )

        assert path.reconcile_ownership is False

    def test_only_the_venv_opts_in(self, tmp_path):
        """Exactly one construction site in build_manifest sets the flag."""
        from fraisier.config import FraisierConfig
        from fraisier.manifest import build_manifest

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: tp
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    install:
      user: appuser
      command: [/usr/local/bin/uv, sync]
    environments:
      production:
        app_path: /var/www/prod
"""
        )

        reconciled = [
            str(mp.path)
            for mp in build_manifest(FraisierConfig(p)).all_paths()
            if mp.reconcile_ownership
        ]

        assert reconciled == ["/var/www/prod/.venv"]


class TestDeployStopsBeforeTouchingTheTree:
    """The install step must abort before it can act on a bad-owner app_path.

    Regression guard for the ordering bug: `_verify_manifest_ownership` runs
    after `_git_pull`, so deleting app_path destroyed the freshly checked-out
    tree and then ran the install command in a missing directory.
    """

    def _deployer(self, tmp_path):
        from fraisier.config import FraisierConfig
        from fraisier.deployers.api import APIDeployer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: tp
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    install:
      user: appuser
      command: [/usr/local/bin/uv, sync]
    environments:
      production:
        app_path: /var/www/prod
"""
        )
        deployer = APIDeployer(
            {
                "app_path": "/var/www/prod",
                "deploy_user": "deployer",
                "install": {
                    "command": ["/usr/local/bin/uv", "sync"],
                    "user": "appuser",
                },
            }
        )
        deployer.config_object = FraisierConfig(p)
        return deployer

    def test_install_aborts_and_never_runs_the_command(self, tmp_path):
        """A wrong-owner app_path raises before the install command is invoked."""
        import pytest

        from fraisier.errors import DeploymentError

        deployer = self._deployer(tmp_path)
        mock_runner = Mock()
        deployer.runner = mock_runner

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "stat") as mock_stat,
            patch(
                "fraisier.deployers.preflight_ownership.shutil.rmtree"
            ) as mock_rmtree,
            patch(
                "fraisier.deployers.preflight_ownership.pwd.getpwuid"
            ) as mock_getpwuid,
        ):
            stat_result = Mock()
            stat_result.st_uid = 4242
            mock_stat.return_value = stat_result
            mock_getpwuid.return_value.pw_name = "someone-else"

            with pytest.raises(DeploymentError):
                deployer._install_dependencies()

            mock_rmtree.assert_not_called()
            mock_runner.run.assert_not_called()
