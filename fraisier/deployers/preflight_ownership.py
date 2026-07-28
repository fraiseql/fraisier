"""Manifest-driven ownership preflight verification."""

import logging
import pwd
import shutil

from fraisier.errors import DeploymentError
from fraisier.manifest import PathManifest

logger = logging.getLogger("fraisier")


def _verify_manifest_ownership(manifest: PathManifest) -> None:
    """Reconcile manifest paths owned by the wrong user.

    A wrong-owner path is only *deleted* when it carries
    ``reconcile_ownership=True`` — i.e. when the deploy itself regenerates it,
    which today is the venv alone. Every other managed path (``app_path``,
    ``git_repo``, ``config_dir``, the relocated tool caches) holds state that no
    later deploy step recreates, so a mismatch raises instead.

    That distinction matters because this runs inside ``_install_dependencies``,
    *after* the git checkout: deleting ``app_path`` here would destroy the tree
    that was just checked out and leave the install command running in a
    directory that no longer exists.

    Args:
        manifest: PathManifest containing all managed paths and their owners

    Raises:
        DeploymentError: A non-regenerable path is owned by the wrong user.

    Logs:
        - info: When a path is deleted due to wrong ownership
        - warning: When stat() fails or UID cannot be resolved
    """
    for mp in manifest.all_paths():
        if not mp.path.exists():
            continue

        try:
            uid = mp.path.stat().st_uid
            actual_owner = pwd.getpwuid(uid).pw_name
        except (OSError, KeyError) as exc:
            logger.warning(
                "Could not stat %s: %s — skipping ownership check", mp.path, exc
            )
            continue

        if actual_owner == mp.owner:
            continue

        if not mp.reconcile_ownership:
            raise DeploymentError(
                f"{mp.path} is owned by {actual_owner!r} but should be owned by "
                f"{mp.owner!r}. Refusing to delete it — nothing in the deploy "
                f"recreates this path, so removing it would destroy state. Fix "
                f"the ownership on the host "
                f"(chown -R {mp.owner}:{mp.group} {mp.path}) and redeploy."
            )

        logger.info(
            "%s owned by %s (expected %s) — removing for recreation",
            mp.path,
            actual_owner,
            mp.owner,
        )
        shutil.rmtree(mp.path)
