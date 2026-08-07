"""Units installed on a host that belong to a fraise which does not run there.

Making host scoping fraise-aware (#336) stops a box from *acquiring* its
neighbour's units. It does not remove the ones already on disk from before
the fix, which are still installed, still enabled, and possibly still
serving traffic.

Those are reported by ``scaffold-diff`` and ``doctor`` and removed only by
``scaffold-install --prune-foreign`` (decision 5, owner's call). The
asymmetry with the stale pre-0.7.1 socket units ``install.sh`` deletes
outright is deliberate: those carry a name fraisier itself assigned under a
superseded scheme, while a neighbouring fraise's unit is another
application's service. Auto-stopping it would turn a routine
``scaffold-install`` into an outage on exactly the configs #336 describes.

**Scope.** Foreign means *fraise*-foreign. An artifact with no owning fraise
— the unit-installer helper, the postgresql conf — cannot be foreign in this
sense: it belongs to an environment, and a host declaring that environment is
entitled to it. A unit left behind by an environment a host no longer
declares is a different question, with no fraise to name in the report, and
is not covered here.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fraisier.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from fraisier.config import FraisierConfig


@dataclass(frozen=True)
class ForeignUnit:
    """One installed unit, and the fraise that owns it elsewhere."""

    source: str
    """Path relative to the scaffold tree, as the manifest names it."""

    installed_path: Path
    """Where it sits on this host."""

    owner_fraise: str
    environment: str

    @property
    def unit_name(self) -> str:
        """The name ``systemctl`` is given, as opposed to the path removed."""
        return self.installed_path.name

    @property
    def owner(self) -> str:
        """``fraise/environment``, for messages and for the refusal check."""
        return f"{self.owner_fraise}/{self.environment}"


def _run_with_sudo(cmd: list[str]) -> None:
    # Fixed argv assembled here, never a shell string.
    subprocess.run(cmd, check=True)


def find_foreign_units(
    config: FraisierConfig,
    *,
    server: str | None = None,
    root: Path | None = None,
) -> list[ForeignUnit]:
    """Return the fraise-owned units installed here that belong elsewhere.

    Args:
        config: the loaded ``fraises.yaml``.
        server: the logical server to judge against. Defaults to resolving
            this machine, and an unresolvable machine yields nothing —
            off-server, naming an arbitrary host's units would answer a
            question nobody asked, the same reasoning that makes
            ``scaffold-diff`` omit the webhook entry there.
        root: reroot the install destinations, a seam for tests. ``None``
            means the real filesystem.

    A unit is reported only when it is actually present: foreign means
    installed here, not merely installable somewhere else.
    """
    from fraisier.scaffold.artifacts import build_artifact_manifest
    from fraisier.scaffold.renderer import (
        ScaffoldRenderer,
        _scope_predicate,
        resolve_local_server,
    )

    resolved = server if server is not None else resolve_local_server(config)
    if resolved is None:
        return []

    allowed = _scope_predicate(config.get_scopes_for_server(resolved))

    # Rendered without `server=`, so the manifest covers every fraise in the
    # config rather than only this host's — the whole point is to find the
    # ones that are *not* this host's.
    renderer = ScaffoldRenderer(config)
    manifest = build_artifact_manifest(renderer, renderer.render(dry_run=True))

    foreign: list[ForeignUnit] = []
    for artifact in manifest.installed():
        if artifact.fraise is None or artifact.environment is None:
            continue
        if allowed(artifact.fraise, artifact.environment):
            continue
        installed_path = _reroot(Path(artifact.destination or ""), root)
        if not installed_path.exists():
            continue
        foreign.append(
            ForeignUnit(
                source=artifact.source,
                installed_path=installed_path,
                owner_fraise=artifact.fraise,
                environment=artifact.environment,
            )
        )
    return foreign


def _reroot(path: Path, root: Path | None) -> Path:
    if root is None:
        return path
    return root / path.relative_to("/")


def prune_foreign_units(
    config: FraisierConfig,
    units: Iterable[ForeignUnit],
    *,
    server: str | None = None,
    runner: Callable[[list[str]], object] = _run_with_sudo,
) -> list[ForeignUnit]:
    """Disable and remove *units*, refusing any this host actually owns.

    The ownership check is repeated here rather than trusted from the
    caller. This is the one code path in the bundle that stops a systemd
    unit, and a list assembled from a stale render or against the wrong
    server must not be able to talk it into stopping a service the host is
    running.

    Returns the units removed. Runs nothing when handed nothing.
    """
    from fraisier.scaffold.renderer import _scope_predicate, resolve_local_server

    units = list(units)
    if not units:
        return []

    resolved = server if server is not None else resolve_local_server(config)
    if resolved is None:
        msg = (
            "Cannot tell which logical server this machine is, so cannot tell "
            "which units are foreign to it. Pass --server explicitly."
        )
        raise ValidationError(msg)

    allowed = _scope_predicate(config.get_scopes_for_server(resolved))
    mine = [u for u in units if allowed(u.owner_fraise, u.environment)]
    if mine:
        listed = ", ".join(f"{u.unit_name} ({u.owner})" for u in mine)
        msg = (
            f"refusing to prune {len(mine)} unit(s) this host owns: {listed}. "
            f"Every unit passed to --prune-foreign must belong to a fraise "
            f"that does not run on {resolved!r}."
        )
        raise ValidationError(msg)

    for unit in units:
        runner(["sudo", "systemctl", "disable", "--now", unit.unit_name])
        runner(["sudo", "rm", "-f", str(unit.installed_path)])
    runner(["sudo", "systemctl", "daemon-reload"])
    return units
