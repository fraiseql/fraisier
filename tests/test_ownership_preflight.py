"""Tests for manifest-driven ownership preflight verification."""

import logging
from pathlib import Path
from unittest.mock import Mock, patch

from fraisier.manifest import ManagedPath, PathManifest


def _make_managed_path(path: str, owner: str, group: str) -> ManagedPath:
    """Helper to create a ManagedPath."""
    return ManagedPath(
        path=Path(path),
        owner=owner,
        group=group,
        mode=0o755,
        read_write_units=(),
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
