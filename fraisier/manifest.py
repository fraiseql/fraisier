"""PathManifest: single source of truth for filesystem paths managed by fraisier."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from fraisier.config import FraisierConfig


@dataclass(frozen=True)
class ManagedPath:
    """Record of a managed filesystem path.

    Fields:
        path: Filesystem path
        owner: Username that owns this path
        group: Group that owns this path
        mode: Permission bits (e.g., 0o755)
        read_write_units: Tuple of systemd unit stems that need ReadWritePaths
        create_if_missing: Whether to create if missing during provisioning
    """

    path: Path
    owner: str
    group: str
    mode: int
    read_write_units: tuple[str, ...]
    create_if_missing: bool = True

    def __str__(self) -> str:
        """Readable representation for logging."""
        return (
            f"ManagedPath(path={self.path}, owner={self.owner}:{self.group}, "
            f"mode={oct(self.mode)}, units={self.read_write_units})"
        )


@dataclass(frozen=True)
class PathManifest:
    """Container for all managed paths (global + per-environment)."""

    global_paths: tuple[ManagedPath, ...]
    env_paths: tuple[ManagedPath, ...]

    def all_paths(self) -> Iterable[ManagedPath]:
        """Return flat iterable of all managed paths."""
        return tuple(self.global_paths) + tuple(self.env_paths)

    def paths_for_unit(self, unit_stem: str) -> Iterable[ManagedPath]:
        """Return paths that list this systemd unit in read_write_units."""
        for path in self.all_paths():
            if unit_stem in path.read_write_units:
                yield path

    def paths_owned_by(self, user: str) -> Iterable[ManagedPath]:
        """Return paths owned by the given user."""
        for path in self.all_paths():
            if path.owner == user:
                yield path


def build_manifest(config: FraisierConfig) -> PathManifest:
    """Construct PathManifest from FraisierConfig.

    Builds paths from:
    - Global fraisier paths: /opt/fraisier, /var/lib/fraisier, /run/fraisier
    - Config directory (parent of scaffold.config_path)
    - Per-environment paths: git_repo, app_path, app_path/.venv (if applicable)

    Args:
        config: FraisierConfig instance

    Returns:
        PathManifest with all managed paths
    """
    deploy_user = config.scaffold.deploy_user

    # Track all seen paths to avoid duplicates
    seen_global: set[str] = set()

    # Global paths
    global_paths: list[ManagedPath] = []

    # /opt/fraisier
    global_paths.append(
        ManagedPath(
            path=Path("/opt/fraisier"),
            owner=deploy_user,
            group=deploy_user,
            mode=0o755,
            read_write_units=(),
            create_if_missing=True,
        )
    )
    seen_global.add("/opt/fraisier")

    # /var/lib/fraisier
    global_paths.append(
        ManagedPath(
            path=Path("/var/lib/fraisier"),
            owner=deploy_user,
            group=deploy_user,
            mode=0o755,
            read_write_units=(),
            create_if_missing=True,
        )
    )
    seen_global.add("/var/lib/fraisier")

    # /run/fraisier (managed by systemd, don't create)
    global_paths.append(
        ManagedPath(
            path=Path("/run/fraisier"),
            owner=deploy_user,
            group=deploy_user,
            mode=0o755,
            read_write_units=(),
            create_if_missing=False,
        )
    )
    seen_global.add("/run/fraisier")

    # Config directory (only if not already in the list)
    config_dir = Path(config.scaffold.config_dir)
    config_dir_str = str(config_dir)
    if config_dir_str not in seen_global:
        global_paths.append(
            ManagedPath(
                path=config_dir,
                owner=deploy_user,
                group=deploy_user,
                mode=0o755,
                read_write_units=(
                    f"{config.project_name}-deploy.service",
                ),
                create_if_missing=True,
            )
        )
        seen_global.add(config_dir_str)

    # Per-environment paths
    env_paths: list[ManagedPath] = []
    seen_paths: set[str] = set()

    for fraise_config in config.fraises.values():
        for env_config in fraise_config.get("environments", {}).values():
            # Get git_repo path
            git_repo = env_config.get("git_repo")
            if git_repo:
                path_str = str(git_repo)
                if path_str not in seen_paths:
                    seen_paths.add(path_str)
                    unit_stem = f"{config.project_name}-deploy.service"
                    env_paths.append(
                        ManagedPath(
                            path=Path(git_repo),
                            owner=deploy_user,
                            group=deploy_user,
                            mode=0o755,
                            read_write_units=(unit_stem,),
                            create_if_missing=True,
                        )
                    )

            # Get app_path
            app_path = env_config.get("app_path")
            if app_path:
                path_str = str(app_path)
                if path_str not in seen_paths:
                    seen_paths.add(path_str)
                    unit_stem = f"{config.project_name}-deploy.service"
                    env_paths.append(
                        ManagedPath(
                            path=Path(app_path),
                            owner=deploy_user,
                            group=deploy_user,
                            mode=0o755,
                            read_write_units=(unit_stem,),
                            create_if_missing=True,
                        )
                    )

                # Check for install.user override
                install_config = env_config.get("install") or fraise_config.get(
                    "install"
                )
                if install_config and isinstance(install_config, dict):
                    install_user = install_config.get("user")
                    if install_user and install_user != deploy_user:
                        venv_path = Path(app_path) / ".venv"
                        venv_path_str = str(venv_path)
                        if venv_path_str not in seen_paths:
                            seen_paths.add(venv_path_str)
                            env_paths.append(
                                ManagedPath(
                                    path=venv_path,
                                    owner=install_user,
                                    group=install_user,
                                    mode=0o755,
                                    read_write_units=(),
                                    create_if_missing=True,
                                )
                            )

    return PathManifest(
        global_paths=tuple(global_paths),
        env_paths=tuple(env_paths),
    )
