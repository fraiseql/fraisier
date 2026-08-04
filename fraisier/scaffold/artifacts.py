"""ArtifactManifest: what ``render()`` produced, and who installs each piece.

``fraisier scaffold`` already knows precisely what it wrote — ``render()``
returns the list of relative paths. That knowledge used to be discarded, and
three other components reconstructed it by hand: sixteen hardcoded names in
``install.sh.j2``, :meth:`ScaffoldRenderer.get_install_mapping`, and
``scheduled_install``'s directory scan. Every bug in the "rendered ≠ installed"
class (#323, #324, #325, #331) lives in that gap.

This module mirrors :mod:`fraisier.manifest`, which is already the single
source of truth for managed *paths*, and extends the idiom from paths to
artifacts.

**The manifest routes; it does not execute.** The install sequences are not
uniform, and the non-uniform ones are load-bearing and hard-won: the
install-helper re-bake must ``cp`` → ``daemon-reload`` → *stop the .service* →
``enable`` + ``restart`` the .socket in that order, because ``enable --now`` is
a no-op on a running unit and the stale argv would otherwise persist behind a
green re-bake (#279); the systemctl-helper must ``daemon-reload`` *before*
restarting or the stop phase wipes ``/run/fraisier``. Flattening those into a
generic executor would discard the reasoning that makes them correct. So each
artifact declares a :class:`Disposition`, generic handling covers ``PLAIN``,
and every special sequence keeps its own hand-written, commented form.

**The load-bearing part is coverage, not generic install.** Every rendered file
must be classified. An unclassified artifact is a hard error naming the file,
so a new rendered artifact cannot be added without someone stating — in
reviewable code — whether it gets installed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from fraisier.errors import ValidationError
from fraisier.naming import unit_installer_unit_names

if TYPE_CHECKING:
    from fraisier.scaffold.renderer import ScaffoldRenderer

SYSTEMD_DIR = "/etc/systemd/system"
NGINX_AVAILABLE = "/etc/nginx/sites-available"

ARTIFACT_MANIFEST_NAME = "artifact-manifest.json"
"""Written into the scaffold tree beside the artifacts it describes.

Nothing in fraisier parses it back. ``install.sh`` has the artifact list and
the hashes *baked in* at render time, so the target host needs no JSON parser;
``doctor`` and ``scaffold-diff`` build the manifest in-process from a render
rather than reading a file that may be older than the code reading it.

So this is the record for a human, for review, and for a diff between two
renders — the one place to look to answer "what does this tree contain and who
installs each piece". ``schema_version`` is carried for whoever writes the
first reader; there deliberately is not one yet.
"""

MANIFEST_SCHEMA_VERSION = 1


class Disposition(StrEnum):
    """How an artifact reaches the system, or why it does not."""

    PLAIN = "plain"
    """Generic ``cp`` to ``destination``, gated on ``environment``.

    The majority, and where custom-named units used to fall through.
    """

    WEBHOOK = "webhook"
    """#325's sequence: host-selected source, daemon-reload, restart, then
    restart the deploy sockets (the webhook owns ``RuntimeDirectory=fraisier``).
    """

    SYSTEMCTL_HELPER = "systemctl_helper"
    """daemon-reload *before* restart, or the stop phase wipes /run/fraisier."""

    SCAFFOLD_INSTALL_HELPER = "scaffold_install_helper"
    """Must not restart its own socket when install.sh is running *inside* it."""

    HELPER_REBAKE = "helper_rebake"
    """#279's allowlist re-bake, ``_run_strict`` throughout."""

    UNIT_INSTALLER = "unit_installer"
    """#240's per-environment unit-installer helper.

    Shares #279's re-bake *shape* — its ExecStart carries the ``--allow``
    allowlist as argv, so a running .service holds the stale one and
    ``enable --now`` is a no-op on it — but not #279's driver: these units come
    one per environment, not one per (fraise, environment). Kept a separate
    disposition so ``with_disposition('helper_rebake')`` cannot silently start
    matching units that block does not install.
    """

    NGINX_VHOST = "nginx_vhost"
    """Copy to sites-available plus a sites-enabled symlink."""

    SUDOERS = "sudoers"
    """``install -m 0440``, visudo-validated *before* installing."""

    APP_MANAGED = "app_managed"
    """Installed by ``fraisier scheduled-install`` from the app's own tree, not
    by scaffold-install. A genuinely different source, so it keeps its separate
    installer — but scaffold-install now knows it exists and says so (#323).
    """

    SCAFFOLD_LOCAL = "scaffold_local"
    """Consumed from the scaffold tree in place; never copied anywhere.

    ``install.sh`` itself, the shell scripts sudoers points at, ``confiture.yaml``,
    the CI workflow.
    """

    MANUAL = "manual"
    """Rendered for the operator to install by hand; install.sh prints how."""

    UNINSTALLED_GAP = "uninstalled_gap"
    """Rendered, needed on the host, and installed by nothing today.

    A deliberate, visible classification. Filing these as ``MANUAL`` would
    launder live bugs into "intentional"; naming them keeps the gap in the
    manifest and in ``doctor`` instead of leaving it implied by the absence of
    an install line. Each carries a ``note`` explaining the consequence.
    """


@dataclass(frozen=True)
class RenderedArtifact:
    """One file ``render()`` wrote, and what becomes of it."""

    source: str
    """Path relative to the scaffold output dir, as ``render()`` returns it."""

    disposition: Disposition
    destination: str | None = None
    """Absolute install path. None for artifacts that are never installed."""

    mode: int | None = None
    """Explicit permission bits, when the copy is not a plain ``cp``."""

    environment: str | None = None
    """Gate on ``_env_active``. None means unconditional."""

    note: str | None = None
    """Why, for dispositions where the absence of an install needs explaining."""

    sha256: str | None = None
    """Content hash, binding this entry to the bytes render() produced."""

    @property
    def is_installed(self) -> bool:
        return self.destination is not None


@dataclass(frozen=True)
class AppManagedUnit:
    """A unit fraisier expects on the host but scaffold-install does not own."""

    unit_name: str
    source_dir: str
    installer: str
    environment: str | None = None


@dataclass(frozen=True)
class UnitInstallerPair:
    """One environment's unit-installer helper, both units together."""

    socket: RenderedArtifact
    service: RenderedArtifact
    environment: str

    @property
    def socket_unit(self) -> str:
        """The name ``systemctl`` is given, as opposed to the path copied."""
        return self.socket.source.removeprefix("systemd/")

    @property
    def service_unit(self) -> str:
        return self.service.source.removeprefix("systemd/")


@dataclass(frozen=True)
class ArtifactManifest:
    """Every artifact of one render, plus what binds it to that render."""

    artifacts: tuple[RenderedArtifact, ...]
    app_managed: tuple[AppManagedUnit, ...]
    batch_hash: str

    def installed(self) -> tuple[RenderedArtifact, ...]:
        return tuple(a for a in self.artifacts if a.is_installed)

    def with_disposition(self, *want: Disposition) -> tuple[RenderedArtifact, ...]:
        return tuple(a for a in self.artifacts if a.disposition in want)

    def gaps(self) -> tuple[RenderedArtifact, ...]:
        return self.with_disposition(Disposition.UNINSTALLED_GAP)

    def unit_installer_pairs(self) -> tuple[UnitInstallerPair, ...]:
        """The unit-installer helpers, socket and service paired per environment.

        The re-bake sequence acts on both units together — stop the .service,
        restart the .socket — so it needs the pair, not two loose entries. The
        pairing is done here rather than by re-deriving one name from the other
        in the template, which is the drift this manifest exists to remove.

        Raises:
            ValidationError: An environment rendered one unit without the
                other. The sequence cannot be run on half a pair, and a
                silently skipped helper is exactly #323's shape.
        """
        by_env: dict[str, dict[str, RenderedArtifact]] = {}
        for artifact in self.with_disposition(Disposition.UNIT_INSTALLER):
            # environment is always set for this disposition; the walrus keeps
            # the type checker honest without inventing a fallback bucket.
            env = artifact.environment or ""
            suffix = "socket" if artifact.source.endswith(".socket") else "service"
            by_env.setdefault(env, {})[suffix] = artifact

        pairs: list[UnitInstallerPair] = []
        for env in sorted(by_env):
            units = by_env[env]
            if set(units) != {"socket", "service"}:
                raise ValidationError(
                    f"unit-installer helper for environment {env!r} is "
                    f"incomplete: rendered {sorted(units)}, needs both the "
                    ".socket and the .service"
                )
            pairs.append(
                UnitInstallerPair(
                    socket=units["socket"], service=units["service"], environment=env
                )
            )
        return tuple(pairs)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Rendered from the scaffold tree and consumed there. sudoers points at the
# shell scripts by absolute path inside the scaffold dir, install.sh is what
# the operator runs, deploy.yml is committed to the app repo by hand.
_SCAFFOLD_LOCAL_SOURCES = frozenset(
    {
        "install.sh",
        "backup.sh",
        "db_reset.sh",
        "db_deploy.sh",
        "confiture.yaml",
        "deploy.yml",
        "systemctl-wrapper.sh",
    }
)

# Units install.sh copies unconditionally — no environment owns them, so
# `_env_of_unit` cannot route them and they are listed by name.
#
# A timer belongs here with the service it activates, never on its own: a timer
# whose target is not installed is a firing that hits a missing unit, which is
# how backup.timer/backup.service drifted apart.
_PLAIN_UNITS = frozenset(
    {
        "systemd/deploy-checker.timer",
        "systemd/deploy-checker.service",
        "systemd/backup.timer",
        "systemd/backup.service",
    }
)

# Rendered, needed, installed by nothing. Kept as data so every instance is
# enumerated in one reviewable place rather than inferred from silence.
#
# What remains here is the self-consistent case: restore-staging's .service and
# .timer are BOTH uninstalled, so nothing fires into a missing unit. The gaps
# where an installed timer activated an uninstalled service, or where one
# component told operators to run an installer that never installed the thing,
# were closed rather than described.
_KNOWN_GAPS: dict[str, str] = {
    "systemd/restore-staging.service": (
        "restore-staging.timer is rendered alongside it and neither is installed "
        "by install.sh"
    ),
    "systemd/restore-staging.timer": (
        "rendered but installed by nothing; its .service is in the same state"
    ),
}

_BACKUP_ALERT_RE = re.compile(r"^systemd/fraisier-.+-backup-alert@\.service$")


INSTALL_SCRIPT_NAME = "install.sh"
"""The one artifact whose own bytes are never hashed — nothing verifies the
verifier, and it is rendered last precisely so every hash it bakes in describes
a file already on disk."""


def _sha256(path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class LazyArtifactManifest:
    """A manifest computed on first attribute access.

    ``ScaffoldRenderer.context`` is read by callers that render a template
    without going through :meth:`ScaffoldRenderer.render` — tests, and any
    future caller that wants one file. Seeding the context with an *empty*
    manifest would make ``install.sh`` render successfully and install nothing,
    which is the silent-skip failure this whole bundle exists to remove. So the
    default is real, just deferred: it resolves by asking the renderer what it
    would write.
    """

    def __init__(self, renderer: ScaffoldRenderer) -> None:
        self._renderer = renderer
        self._resolved: ArtifactManifest | None = None

    def _value(self) -> ArtifactManifest:
        if self._resolved is None:
            self._resolved = build_artifact_manifest(
                self._renderer, self._renderer.render(dry_run=True)
            )
        return self._resolved

    def __getattr__(self, name: str):
        return getattr(self._value(), name)


def _env_of_unit(renderer: ScaffoldRenderer, source: str) -> str | None:
    """The environment an env-gated unit belongs to, or None.

    Derived from the same context install.sh gates with, so the manifest and
    the ``_env_active`` guard cannot disagree about which env owns a unit.
    """
    from fraisier.naming import deploy_socket_name

    stem = source.removeprefix("systemd/")
    for fraise in renderer.context["local_fraises"]:
        for env_name, env_config in fraise.get("environments", {}).items():
            socket_unit = deploy_socket_name(env_config, env_name, fraise["name"])
            socket_stem = socket_unit.removesuffix(".socket")
            candidates = {
                socket_unit,
                f"{socket_stem}@.service",
                f"{env_config.get('service_base', '')}.service",
            }
            if stem in candidates:
                return env_name
    return None


def _classify(renderer: ScaffoldRenderer, source: str) -> RenderedArtifact | None:
    """Route one rendered file, or None when nothing claims it."""
    from fraisier.scaffold.renderer import _collect_unit_installer_envs

    project = renderer.context["project_name"]
    stem = source.removeprefix("systemd/")

    if source in _SCAFFOLD_LOCAL_SOURCES:
        return RenderedArtifact(source, Disposition.SCAFFOLD_LOCAL)

    if source in _KNOWN_GAPS:
        return RenderedArtifact(
            source, Disposition.UNINSTALLED_GAP, note=_KNOWN_GAPS[source]
        )

    if source == "sudoers":
        return RenderedArtifact(
            source,
            Disposition.SUDOERS,
            destination=f"/etc/sudoers.d/{project}",
            mode=0o440,
        )

    if source.startswith("postgresql/"):
        return RenderedArtifact(
            source,
            Disposition.MANUAL,
            note="server-specific; install.sh prints the cp for the operator",
        )

    if source == "nginx/gateway.conf":
        return RenderedArtifact(
            source,
            Disposition.NGINX_VHOST,
            destination=f"{NGINX_AVAILABLE}/{project}",
        )

    if source.startswith("nginx/"):
        vhost = source.removeprefix("nginx/").removesuffix(".conf")
        return RenderedArtifact(
            source,
            Disposition.NGINX_VHOST,
            destination=f"{NGINX_AVAILABLE}/{vhost}",
            environment=_nginx_env(renderer, vhost),
        )

    # The webhook unit's source carries the host, its destination never does.
    if re.fullmatch(rf"fraisier-{re.escape(project)}-webhook(-.+)?\.service", source):
        return RenderedArtifact(
            source,
            Disposition.WEBHOOK,
            destination=f"{SYSTEMD_DIR}/fraisier-{project}-webhook.service",
        )

    if stem in (
        f"fraisier-{project}-systemctl-helper.service",
        f"fraisier-{project}-systemctl-helper.socket",
    ):
        return RenderedArtifact(
            source, Disposition.SYSTEMCTL_HELPER, destination=f"{SYSTEMD_DIR}/{stem}"
        )

    if stem in (
        f"fraisier-{project}-scaffold-install-helper.service",
        f"fraisier-{project}-scaffold-install-helper.socket",
    ):
        return RenderedArtifact(
            source,
            Disposition.SCAFFOLD_INSTALL_HELPER,
            destination=f"{SYSTEMD_DIR}/{stem}",
        )

    for entry in renderer.context["install_helper_sockets"]:
        if stem in (entry["socket_unit"], entry["service_unit"]):
            return RenderedArtifact(
                source,
                Disposition.HELPER_REBAKE,
                destination=f"{SYSTEMD_DIR}/{stem}",
                environment=entry["env_name"],
            )

    # #240's unit-installer helper, one per environment with scheduled fraises.
    # Matched against the environments the renderer actually wrote units for,
    # and against names from the same helper it used, so a rename cannot leave
    # this branch matching nothing and silently reclassifying the units.
    for env_name in _collect_unit_installer_envs(renderer.context["local_fraises"]):
        if stem in unit_installer_unit_names(project, env_name):
            return RenderedArtifact(
                source,
                Disposition.UNIT_INSTALLER,
                destination=f"{SYSTEMD_DIR}/{stem}",
                environment=env_name,
            )

    # backup.service's OnFailure= target. Installed unconditionally, like the
    # backup units it serves: a missing OnFailure= target does not fail
    # loudly — systemd logs that it could not enqueue the job — so the backup
    # failure this unit exists to announce would go out silently.
    if _BACKUP_ALERT_RE.match(source):
        return RenderedArtifact(
            source, Disposition.PLAIN, destination=f"{SYSTEMD_DIR}/{stem}"
        )

    if source in _PLAIN_UNITS:
        return RenderedArtifact(
            source, Disposition.PLAIN, destination=f"{SYSTEMD_DIR}/{stem}"
        )

    if source.startswith("systemd/"):
        env = _env_of_unit(renderer, source)
        if env is not None:
            return RenderedArtifact(
                source,
                Disposition.PLAIN,
                destination=f"{SYSTEMD_DIR}/{stem}",
                environment=env,
            )

    if source.startswith("rc.d/"):
        return RenderedArtifact(
            source,
            Disposition.MANUAL,
            note="rc.d service manager; installed by the platform's own tooling",
        )

    return None


def _nginx_env(renderer: ScaffoldRenderer, vhost: str) -> str | None:
    """The environment a rendered vhost belongs to.

    Delegates to the renderer rather than re-deriving the stem: computing that
    name in a second place is what left vhosts without an explicit
    ``server_name`` rendered under one name and installed under another.
    """
    return renderer.nginx_vhost_envs().get(vhost)


class UndispositionedArtifacts(ValidationError):
    """Rendered files no rule claims — the coverage assertion failing.

    Carries the offending sources so callers can render their own diagnostic
    (``doctor`` wants one line, ``scaffold`` wants the full explanation)
    without scraping the message back apart.
    """

    def __init__(self, sources: list[str]) -> None:
        self.sources = sources
        super().__init__(_undispositioned_message(sources))


def _undispositioned_message(sources: list[str]) -> str:
    listed = "\n".join(f"  - {s}" for s in sources)
    return (
        f"{len(sources)} rendered artifact(s) have no disposition:\n{listed}\n\n"
        "Every file 'fraisier scaffold' writes must declare what becomes of it, "
        "so a new artifact cannot be rendered and then installed by nobody — "
        "which is the whole '#323 / #325' bug class.\n\n"
        "Add it to fraisier/scaffold/artifacts.py::_classify with the "
        "disposition that fits:\n"
        "  PLAIN            — a generic env-gated copy to /etc/systemd/system\n"
        "  SCAFFOLD_LOCAL   — consumed from the scaffold tree, never installed\n"
        "  MANUAL           — the operator installs it by hand\n"
        "  APP_MANAGED      — fraisier scheduled-install owns it\n"
        "  UNINSTALLED_GAP  — it should be installed and is not (say why)\n"
        "…or one of the sequence dispositions if its install is not a plain copy."
    )


def build_artifact_manifest(
    renderer: ScaffoldRenderer,
    rendered_files: list[str],
) -> ArtifactManifest:
    """Classify everything *renderer* just wrote.

    Args:
        renderer: The renderer that produced *rendered_files*, read for its
            context (local fraises, install-helper sockets, project name) and
            its ``output_dir`` for content hashing.
        rendered_files: Exactly what :meth:`ScaffoldRenderer.render` returned.

    Raises:
        ValidationError: Any rendered file no rule claims. This is the coverage
            assertion — the reason the manifest exists.
    """
    artifacts: list[RenderedArtifact] = []
    undispositioned: list[str] = []

    for source in rendered_files:
        artifact = _classify(renderer, source)
        if artifact is None:
            undispositioned.append(source)
            continue
        # install.sh is excluded: it is rendered after everything else so the
        # hashes it bakes in are current, which means its own bytes do not
        # exist yet when the manifest is built.
        digest = (
            None
            if source == INSTALL_SCRIPT_NAME
            else _sha256(renderer.output_dir / source)
        )
        artifacts.append(replace(artifact, sha256=digest))

    if undispositioned:
        raise UndispositionedArtifacts(sorted(undispositioned))

    artifacts.sort(key=lambda a: a.source)
    return ArtifactManifest(
        artifacts=tuple(artifacts),
        app_managed=tuple(_collect_app_managed(renderer)),
        batch_hash=_batch_hash(artifacts),
    )


def _batch_hash(artifacts: list[RenderedArtifact]) -> str:
    """Hash binding the manifest to the bytes of this render.

    Covers content *and* routing: a manifest describing the same files with a
    different destination is a different manifest, so an install.sh checking
    this cannot be handed a stale one that happens to list matching names.
    """
    digest = hashlib.sha256()
    for artifact in sorted(artifacts, key=lambda a: a.source):
        digest.update(
            "\0".join(
                [
                    artifact.source,
                    artifact.disposition.value,
                    artifact.destination or "",
                    str(artifact.mode or ""),
                    artifact.environment or "",
                    artifact.sha256 or "",
                ]
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _collect_app_managed(renderer: ScaffoldRenderer) -> list[AppManagedUnit]:
    """Units ``fraisier scheduled-install`` owns, from the app's own tree.

    Not rendered here, and deliberately not installed here — they are the
    consumer's hand-authored files, a genuinely different source. Recording
    them is what stops the two installers from covering disjoint sets
    *silently*, which was #323's actual complaint.
    """
    from fraisier.scheduled_install import APP_PATH_UNITS_SUBDIR

    units: list[AppManagedUnit] = []
    for fraise in renderer.context["local_fraises"]:
        if fraise.get("type") != "scheduled":
            continue
        for env_name, env_config in fraise.get("environments", {}).items():
            app_path = env_config.get("app_path")
            if not app_path:
                continue
            for job in (env_config.get("jobs") or {}).values():
                if not isinstance(job, dict):
                    continue
                for key in ("systemd_service", "systemd_timer"):
                    unit = job.get(key)
                    if unit:
                        units.append(
                            AppManagedUnit(
                                unit_name=unit,
                                source_dir=f"{app_path}/{APP_PATH_UNITS_SUBDIR}",
                                installer="fraisier scheduled-install",
                                environment=env_name,
                            )
                        )
    return units


def dump_manifest(manifest: ArtifactManifest) -> str:
    """Serialise for the on-disk manifest, sorted so renders diff cleanly."""
    import json

    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "batch_hash": manifest.batch_hash,
        "artifacts": [
            {
                "source": a.source,
                "disposition": a.disposition.value,
                "destination": a.destination,
                "mode": a.mode,
                "environment": a.environment,
                "sha256": a.sha256,
                "note": a.note,
            }
            for a in manifest.artifacts
        ],
        "app_managed": [
            {
                "unit_name": u.unit_name,
                "source_dir": u.source_dir,
                "installer": u.installer,
                "environment": u.environment,
            }
            for u in manifest.app_managed
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
