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

    TIMER = "timer"
    """``PLAIN`` plus ``systemctl enable --now`` after the daemon-reload.

    For timers that must actually fire. A timer filed as ``PLAIN`` is
    rendered, installed, hashed, drift-checked and never run — the #339
    incident's own failure mode (the artifact exists, the work does not
    happen) reproduced inside the system built to prevent it. Until #341 that
    was the state of every timer ``install.sh`` installs.

    Two routes here, and the difference is deliberate:

    - **#339's retention pair, unconditionally.** A retention unit that does
      not fire *is* the incident, so it is never the operator's call.
    - **The three families in ``scaffold.systemd.timers``**, when the operator
      switches one on. They default to off, so an upgrade starts nothing:
      enabling ``backup.timer`` means starting a legacy ``pg_dump | gzip`` on
      a host that never asked, and that is a decision with its own blast
      radius rather than a side effect of a release.
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
    """The environment this artifact belongs to. None means unconditional."""

    fraise: str | None = None
    """The fraise that owns it, deciding which gate install.sh routes it to.

    A name routes to ``_scope_active <fraise> <env>``: the artifact belongs to
    one fraise, and a host running a *different* fraise under the same
    environment name must not install it (#336).

    ``None`` alongside an ``environment`` routes to ``_env_active <env>``, which
    after #336 means "a fraise on this host declares this environment". Some
    artifacts genuinely have no owning fraise — the unit-installer helper is one
    per (project, environment) by design (#240), and the postgresql logging conf
    is per environment. Naming an arbitrary fraise for those would invent an
    owner, which is how a second host authority gets born.

    ``None`` alongside no ``environment`` is unconditional, as before.
    """

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
    fraise: str | None = None


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

    def inert_timers(self) -> tuple[RenderedArtifact, ...]:
        """Timers this install copies and deliberately does not start (#341).

        The `.timer` of each switchable family whose knob is off — one entry
        per family, not two, since naming the `.service` alongside it would
        report the same decision twice.

        Reported by ``install.sh`` and by ``doctor`` rather than left implied
        by the absence of an enable line. That absence is what hid three
        broken units for the project's whole history: the operator saw a
        successful install, and nothing distinguished "copied, running" from
        "copied, never started".
        """
        return tuple(
            a
            for a in self.artifacts
            if a.disposition is Disposition.PLAIN and a.source in _TIMER_UNIT_FAMILY
            if a.source.endswith(".timer")
        )

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

# The timer families `scaffold.systemd.timers` switches, and the units each
# one owns. Copied unconditionally — no environment owns them, so
# `_owner_of_unit` cannot route them and they are listed by name.
#
# The pair is the unit of classification, deliberately. A timer must never be
# enabled without the service it activates: neither carries a `Unit=`, so
# systemd resolves the target by stem, and a timer whose target is missing is a
# firing into nothing — which is exactly how backup.timer and backup.service
# drifted apart. Classifying them together makes that structural rather than
# remembered.
_TIMER_FAMILY_UNITS: dict[str, tuple[str, str]] = {
    "backup": ("systemd/backup.timer", "systemd/backup.service"),
    "deploy_checker": (
        "systemd/deploy-checker.timer",
        "systemd/deploy-checker.service",
    ),
    "restore_staging": (
        "systemd/restore-staging.timer",
        "systemd/restore-staging.service",
    ),
}

_TIMER_UNIT_FAMILY: dict[str, str] = {
    source: family
    for family, sources in _TIMER_FAMILY_UNITS.items()
    for source in sources
}

# Rendered, needed, installed by nothing. Kept as data so every instance is
# enumerated in one reviewable place rather than inferred from silence.
#
# Empty since #341, which installed restore-staging — the last entry, and the
# only self-consistent one left after v0.57.0 closed the four where an
# installed timer activated an uninstalled service. The classification stays:
# it is the honest label for the next artifact that is rendered, needed and
# reached by no installer, and filing such a thing as MANUAL would launder a
# live bug into "intentional". An entry here must carry what it breaks.
_KNOWN_GAPS: dict[str, str] = {}


def _inert_note(family: str, project: str) -> str:
    """What enabling a copied-but-inert timer would start, and how to do it.

    `note` was introduced for UNINSTALLED_GAP — "why, for dispositions where
    the absence of an install needs explaining". "Installed, and deliberately
    not started" is the same question one step further along, and #341 emptied
    out the field's original user.
    """
    from fraisier.config.schema import TIMER_FAMILIES

    # Self-contained: this string also stands alone in artifact-manifest.json,
    # with no surrounding block to say what state it is describing.
    does = TIMER_FAMILIES[family].format(project=project)
    return f"not enabled; would run {does}. Enable: scaffold.systemd.timers.{family}"


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


def _owner_of_unit(renderer: ScaffoldRenderer, source: str) -> tuple[str, str] | None:
    """The ``(fraise, environment)`` an env-gated unit belongs to, or None.

    Derived from the same context install.sh gates with, so the manifest and
    the ``_scope_active`` guard cannot disagree about which pair owns a unit.
    The loop already walked past the fraise to find the environment; it simply
    stopped discarding it (#336).
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
                return fraise["name"], env_name
    return None


def _classify(renderer: ScaffoldRenderer, source: str) -> RenderedArtifact | None:
    """Route one rendered file, or None when nothing claims it."""
    # Resolved through the module rather than bound at import: the renderer
    # names the files it writes and this names the files it installs, and a
    # test proves the two move together by patching the authority. A
    # module-level `from … import` here would make that test pass while the
    # two sites disagreed, which is the drift it exists to catch.
    from fraisier.naming import retention_unit_names
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
        owner = _nginx_owner(renderer, vhost)
        return RenderedArtifact(
            source,
            Disposition.NGINX_VHOST,
            destination=f"{NGINX_AVAILABLE}/{vhost}",
            fraise=owner[0] if owner else None,
            environment=owner[1] if owner else None,
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
                fraise=entry["fraise_name"],
                environment=entry["env_name"],
            )

    # #240's unit-installer helper, one per environment with scheduled fraises.
    # Matched against the environments the renderer actually wrote units for,
    # and against names from the same helper it used, so a rename cannot leave
    # this branch matching nothing and silently reclassifying the units.
    #
    # No `fraise`: one helper serves every scheduled fraise in the environment,
    # so it is env-owned and gates on `_env_active` (#336 decision 4).
    for env_name in _collect_unit_installer_envs(renderer.context["local_fraises"]):
        if stem in unit_installer_unit_names(project, env_name):
            return RenderedArtifact(
                source,
                Disposition.UNIT_INSTALLER,
                destination=f"{SYSTEMD_DIR}/{stem}",
                environment=env_name,
            )

    # #339's retention pair, one per (environment, retain entry). Matched
    # against the entries the renderer actually wrote units for, and against
    # names from the same helper it used, so a rename cannot leave this branch
    # matching nothing and silently reclassifying the units.
    #
    # No `fraise`: a received corpus arrives by rsync from somewhere else and
    # has no owning fraise here, so it gates on `_env_active` (#336 decision
    # 4). TIMER rather than PLAIN because it has to fire.
    for entry in renderer.retention_entries():
        if stem in retention_unit_names(project, entry.environment, entry.name):
            return RenderedArtifact(
                source,
                Disposition.TIMER,
                destination=f"{SYSTEMD_DIR}/{stem}",
                environment=entry.environment,
            )

    # backup.service's OnFailure= target. Installed unconditionally, like the
    # backup units it serves: a missing OnFailure= target does not fail
    # loudly — systemd logs that it could not enqueue the job — so the backup
    # failure this unit exists to announce would go out silently.
    if _BACKUP_ALERT_RE.match(source):
        return RenderedArtifact(
            source, Disposition.PLAIN, destination=f"{SYSTEMD_DIR}/{stem}"
        )

    # The three families install.sh has always copied and, before #341,
    # enabled none of. The knob picks the disposition; install.sh's existing
    # `timer` block does the rest, so enabling a family is a classification
    # change rather than a new code path in the installer.
    family = _TIMER_UNIT_FAMILY.get(source)
    if family is not None:
        enabled = renderer.config.scaffold.systemd.timers.get(family, False)
        return RenderedArtifact(
            source,
            Disposition.TIMER if enabled else Disposition.PLAIN,
            destination=f"{SYSTEMD_DIR}/{stem}",
            note=None if enabled else _inert_note(family, project),
        )

    if source.startswith("systemd/"):
        owner = _owner_of_unit(renderer, source)
        if owner is not None:
            return RenderedArtifact(
                source,
                Disposition.PLAIN,
                destination=f"{SYSTEMD_DIR}/{stem}",
                fraise=owner[0],
                environment=owner[1],
            )

    if source.startswith("rc.d/"):
        return RenderedArtifact(
            source,
            Disposition.MANUAL,
            note="rc.d service manager; installed by the platform's own tooling",
        )

    return None


def _nginx_owner(renderer: ScaffoldRenderer, vhost: str) -> tuple[str, str] | None:
    """The ``(fraise, environment)`` a rendered vhost belongs to.

    Delegates to the renderer rather than re-deriving the stem: computing that
    name in a second place is what left vhosts without an explicit
    ``server_name`` rendered under one name and installed under another.
    """
    return renderer.nginx_vhost_scopes().get(vhost)


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
                    artifact.fraise or "",
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
                                fraise=fraise["name"],
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
                "fraise": a.fraise,
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
                "fraise": u.fraise,
                "environment": u.environment,
            }
            for u in manifest.app_managed
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
