"""Manifest-driven ownership preflight verification."""

import logging
import pwd
import shutil

from fraisier.manifest import PathManifest

logger = logging.getLogger("fraisier")


def _verify_manifest_ownership(manifest: PathManifest) -> None:
    """Delete manifest paths owned by the wrong user so they can be recreated.

    For each path in the manifest, if the path exists and is owned by a user
    different from the path's declared owner, delete it. This allows subsequent
    install steps to recreate the path with the correct ownership.

    Args:
        manifest: PathManifest containing all managed paths and their owners

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

        if actual_owner != mp.owner:
            logger.info(
                "%s owned by %s (expected %s) — removing for recreation",
                mp.path,
                actual_owner,
                mp.owner,
            )
            shutil.rmtree(mp.path)
