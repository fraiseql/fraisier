"""``fraisier doctor`` — host-wide self-diagnosis (#221 bundle B phase 04).

Independent of any particular fraise. Answers "is this fraisier install
OK to use at all?" rather than the per-environment question
``fraisier diagnose <fraise> <env>`` answers.

Each check is a pure function ``(Config | None) -> CheckResult``
registered via ``@register_check``. Checks never abort each other — one
failing check returns ``fail`` and the registry moves on. The CLI
wrapper aggregates results and decides exit code.

Security
- No check has side effects beyond reading state (no ``systemctl start``,
  no DB writes, no env-var mutation).
- Never prints secret values, only env-var *names*.
- ``helper_sudoers`` reads stat info only; never invokes ``visudo``,
  never parses sudoers syntax. When the file is readable, it byte-diffs
  against the expected rendered fragment via
  ``fraisier.scaffold.sudoers_diff.diff_sudoers``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fraisier.errors import ValidationError

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig


Status = Literal["pass", "warn", "fail", "skip"]


@dataclass(frozen=True)
class CheckResult:
    """Result of running one doctor check."""

    name: str
    status: Status
    detail: str
    fix_hint: str | None = None


CheckFn = Callable[["FraisierConfig | None"], CheckResult]


@dataclass(frozen=True)
class _CheckEntry:
    fn: CheckFn
    network: bool
    privileged: bool = False


DOCTOR_CHECKS: dict[str, _CheckEntry] = {}


def register_check(
    name: str, *, network: bool = False, privileged: bool = False
) -> Callable[[CheckFn], CheckFn]:
    """Decorator: register a doctor check by name.

    ``privileged`` marks a check that needs root and mutates nothing but
    still costs real work (spawning a transient systemd unit). Those are
    opt-in: a default ``fraisier doctor`` reports them as ``skip``.
    """

    def deco(fn: CheckFn) -> CheckFn:
        DOCTOR_CHECKS[name] = _CheckEntry(fn=fn, network=network, privileged=privileged)
        return fn

    return deco


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@register_check("python_version")
def _check_python_version(_config: FraisierConfig | None) -> CheckResult:
    minimum = (3, 11)
    actual = sys.version_info[:3]
    detail = ".".join(str(p) for p in actual)
    if actual < minimum:
        return CheckResult(
            "python_version",
            "fail",
            f"Python {detail} < {'.'.join(str(p) for p in minimum)}",
            fix_hint="upgrade Python to 3.11 or newer",
        )
    return CheckResult("python_version", "pass", detail)


@register_check("fraisier_version")
def _check_fraisier_version(_config: FraisierConfig | None) -> CheckResult:
    try:
        from importlib.metadata import version

        v = version("fraisier")
    except Exception as exc:
        return CheckResult(
            "fraisier_version",
            "fail",
            f"importlib.metadata could not resolve fraisier: {exc}",
            fix_hint="reinstall fraisier (`pip install --force-reinstall fraisier`)",
        )
    return CheckResult("fraisier_version", "pass", v)


@register_check("confiture_version")
def _check_confiture_version(_config: FraisierConfig | None) -> CheckResult:
    binary = shutil.which("confiture")
    if binary is None:
        return CheckResult(
            "confiture_version",
            "fail",
            "confiture binary not found on PATH",
            fix_hint="install confiture (`pip install confiture` or vendor-specific)",
        )
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(
            "confiture_version",
            "fail",
            f"confiture --version failed: {exc}",
        )
    if proc.returncode != 0:
        return CheckResult(
            "confiture_version",
            "fail",
            f"confiture --version exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:120]}",
        )
    return CheckResult(
        "confiture_version", "pass", (proc.stdout or "").strip().splitlines()[0][:120]
    )


@register_check("fraises_yaml_loadable")
def _check_fraises_yaml_loadable(config: FraisierConfig | None) -> CheckResult:
    if config is None:
        return CheckResult(
            "fraises_yaml_loadable",
            "skip",
            "no fraises.yaml found",
            fix_hint="run `fraisier init` to scaffold a config",
        )
    return CheckResult(
        "fraises_yaml_loadable",
        "pass",
        f"loaded {getattr(config, 'config_path', '<unknown path>')}",
    )


@register_check("fraises_yaml_resolves")
def _check_fraises_yaml_resolves(config: FraisierConfig | None) -> CheckResult:
    if config is None:
        return CheckResult("fraises_yaml_resolves", "skip", "no fraises.yaml found")
    from fraisier.introspection import (
        SUBCOMMAND_CONFIG_SECTIONS,
        reachable_envvars,
    )

    raw = getattr(config, "_config", None)
    if not isinstance(raw, dict):
        return CheckResult(
            "fraises_yaml_resolves",
            "warn",
            "config loaded but raw dict not introspectable",
        )

    unset_names: set[str] = set()
    for cmd in SUBCOMMAND_CONFIG_SECTIONS:
        for ref in reachable_envvars(raw, cmd):
            if not ref.is_set:
                unset_names.add(ref.name)
    if not unset_names:
        return CheckResult("fraises_yaml_resolves", "pass", "all !envvar refs resolve")
    return CheckResult(
        "fraises_yaml_resolves",
        "warn",
        f"{len(unset_names)} envvar(s) unset: {', '.join(sorted(unset_names))}",
        fix_hint="export the missing variables or move them to secrets.env",
    )


@register_check("secrets_env_readable")
def _check_secrets_env_readable(_config: FraisierConfig | None) -> CheckResult:
    path = Path.home() / ".config" / "fraisier" / "secrets.env"
    if not path.exists():
        return CheckResult("secrets_env_readable", "skip", f"{path} does not exist")
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        return CheckResult(
            "secrets_env_readable",
            "fail",
            f"cannot stat {path}: {exc}",
        )
    if mode != 0o600:
        return CheckResult(
            "secrets_env_readable",
            "fail",
            f"{path} mode is {oct(mode)} (expected 0o600)",
            fix_hint=f"chmod 600 {path}",
        )
    return CheckResult("secrets_env_readable", "pass", f"{path} (mode 0600)")


@register_check("helper_sudoers")
def _check_helper_sudoers(config: FraisierConfig | None) -> CheckResult:
    # Read stat info only — never invoke visudo, never parse sudoers
    # syntax (CVE-class history). When the content is readable, byte-diff
    # against the expected fragment via scaffold.sudoers_diff.
    project = getattr(config, "project_name", None) if config is not None else None
    if project is None:
        return CheckResult("helper_sudoers", "skip", "no project_name in config")
    path = Path("/etc/sudoers.d") / project
    if not path.exists():
        return CheckResult(
            "helper_sudoers",
            "warn",
            f"{path} not present",
            fix_hint=f"run `fraisier scaffold-install` to install {path}",
        )
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        return CheckResult(
            "helper_sudoers",
            "fail",
            f"cannot stat {path}: {exc}",
        )
    if mode != 0o440:
        return CheckResult(
            "helper_sudoers",
            "fail",
            f"{path} mode is {oct(mode)} (expected 0o440)",
            fix_hint=f"chmod 440 {path}",
        )
    return CheckResult("helper_sudoers", "pass", f"{path} (mode 0440)")


def _is_uv_sync(command: list[str]) -> bool:
    """True when *command* invokes ``uv sync``, absolute path or not."""
    if len(command) < 2:
        return False
    return Path(command[0]).name == "uv" and command[1] == "sync"


@register_check("install_compile_bytecode")
def _check_install_compile_bytecode(config: FraisierConfig | None) -> CheckResult:
    """Warn when ``uv sync`` installs a venv nothing will ever byte-compile.

    ``uv sync`` does not compile by default, and since v0.50.1 every app unit
    sets ``PYTHONDONTWRITEBYTECODE=1`` (#292) — so without
    ``--compile-bytecode`` at install time the venv holds no ``.pyc`` and none
    is ever written, making every service start recompile all of
    site-packages. Measured at ~434 ms per start on a 49 MB site-packages app
    (#298).

    The two settings compose rather than conflict: ``PYTHONDONTWRITEBYTECODE``
    blocks *writes* only, so a cache laid down at install time is still read.
    The ``.pyc`` are owned by the install user, which is also what keeps them
    clear of the stale-cache sweep (#303) and of the ownership hazard #292 is
    about.

    Advisory only — ``warn``, never ``fail``. It costs startup time, not
    correctness.
    """
    name = "install_compile_bytecode"
    fraises = getattr(config, "fraises", None) if config is not None else None
    if not fraises:
        return CheckResult(name, "skip", "no fraises in config")

    missing: list[str] = []
    checked = 0
    for fraise_name, fraise in fraises.items():
        if not isinstance(fraise, dict):
            continue
        fraise_install = fraise.get("install") or {}
        environments = fraise.get("environments") or {}
        if not isinstance(environments, dict):
            continue
        for env_name, env_config in environments.items():
            # env-level `install:` overrides the fraise-level default, the same
            # resolution the scaffold renderer uses when it bakes the sudoers
            # rule and the install-helper allowlist.
            env_install = (
                env_config.get("install") if isinstance(env_config, dict) else None
            )
            install = env_install or fraise_install
            command = install.get("command") or [] if isinstance(install, dict) else []
            if not isinstance(command, list) or not _is_uv_sync(command):
                continue
            checked += 1
            if "--compile-bytecode" not in command:
                missing.append(f"{fraise_name}/{env_name}")

    if not checked:
        return CheckResult(name, "skip", "no `uv sync` install command configured")
    if missing:
        return CheckResult(
            name,
            "warn",
            f"`uv sync` without --compile-bytecode: {', '.join(sorted(missing))}"
            " — every service start recompiles site-packages",
            fix_hint=(
                "add --compile-bytecode to install.command "
                "(see https://github.com/fraiseql/fraisier/issues/298)"
            ),
        )
    return CheckResult(name, "pass", f"{checked} `uv sync` command(s) compile bytecode")


def _installed_webhook_unit(project_name: str) -> Path:
    """Where scaffold-install puts the webhook unit. Seam for tests."""
    return Path("/etc/systemd/system") / f"fraisier-{project_name}-webhook.service"


def _enabled_dump_dirs(config: FraisierConfig | None) -> list[str]:
    """Every ``pre_migrate_dump.output_dir`` for a gate that is switched on."""
    dirs: list[str] = []
    fraises = getattr(config, "fraises", None) if config is not None else None
    for fraise in (fraises or {}).values():
        if not isinstance(fraise, dict):
            continue
        for env_config in (fraise.get("environments") or {}).values():
            if not isinstance(env_config, dict):
                continue
            pmd = (env_config.get("database") or {}).get("pre_migrate_dump") or {}
            out = pmd.get("output_dir")
            if pmd.get("enabled") and out and out not in dirs:
                dirs.append(out)
    return dirs


def _resolve_local_server(config: FraisierConfig) -> str | None:
    """Which logical server this machine is. Seam for tests."""
    from fraisier.scaffold.renderer import resolve_local_server

    return resolve_local_server(config)


def _strict_readwritepaths(unit_path: Path) -> list[str] | None:
    """``ReadWritePaths=`` of an installed ``ProtectSystem=strict`` unit.

    Returns None when the unit is not installed (a dev machine has none, and
    that is not a finding) and an empty list when it is installed but not
    strict — in which case the allowlist does not gate writes at all.
    """
    try:
        unit = unit_path.read_text()
    except OSError:
        return None
    if "ProtectSystem=strict" not in unit:
        return []
    return [
        ln.split("=", 1)[1].strip()
        for ln in unit.splitlines()
        if ln.startswith("ReadWritePaths=")
    ]


def _not_covered(paths: list[str], allowed: list[str]) -> list[str]:
    """Those of *paths* that no entry in *allowed* contains.

    Prefix containment on whole components: ``ReadWritePaths=/var/www`` does
    grant ``/var/www/api``, and ``/var/wwwroot`` is not a match for
    ``/var/www``.
    """
    return [
        p
        for p in paths
        if not any(p == a or p.startswith(a.rstrip("/") + "/") for a in allowed)
    ]


def _hosted_trees(config: FraisierConfig, server: str) -> list[str]:
    """Every ``git_repo``/``app_path`` of a ``(fraise, environment)`` *server* hosts.

    Keyed by the pair, matching what the webhook unit is rendered from: a
    host carrying ``api/production`` does not thereby carry
    ``worker/production``, and demanding writes to the latter's trees would
    report a correctly scoped unit as broken (#336).
    """
    from fraisier.scaffold.renderer import _scope_predicate

    hosted = _scope_predicate(config.get_scopes_for_server(server))
    trees: list[str] = []
    for fraise_name, fraise in (getattr(config, "fraises", None) or {}).items():
        if not isinstance(fraise, dict):
            continue
        for env_name, env_config in (fraise.get("environments") or {}).items():
            if not hosted(fraise_name, env_name) or not isinstance(env_config, dict):
                continue
            for key in ("git_repo", "app_path"):
                value = env_config.get(key)
                if value and str(value) not in trees:
                    trees.append(str(value))
    return trees


@register_check("webhook_hosted_trees_writable")
def _check_webhook_hosted_trees_writable(config: FraisierConfig | None) -> CheckResult:
    """This host's webhook unit must allow writes to the trees it hosts (#325).

    Same shape and same reasoning as the #317 dump-dir check, widened from
    dump directories to the ``git_repo``/``app_path`` of every environment
    this machine hosts. Reads the **installed** unit rather than the rendered
    one, so it also catches the upgrade-without-re-scaffold case — the
    likeliest way to still be broken after the template fix — and a
    hand-written unit no template fix can reach.

    Only the *missing* direction is a finding here. An extra path is the #62
    least-privilege leak, which the render-time invariant owns; flagging it
    here would report every host that legitimately shares a tree.

    Warn rather than fail, matching #317: a hard failure would break hosts
    limping along on a hand-edited unit that works.
    """
    name = "webhook_hosted_trees_writable"
    project = getattr(config, "project_name", None) if config is not None else None
    if config is None or not project:
        return CheckResult(name, "skip", "no project_name in config")

    server = _resolve_local_server(config)
    if server is None:
        return CheckResult(
            name, "skip", "cannot tell which logical server this machine is"
        )

    trees = _hosted_trees(config, server)
    if not trees:
        return CheckResult(name, "skip", f"no git_repo/app_path hosted on {server}")

    unit_path = _installed_webhook_unit(project)
    allowed = _strict_readwritepaths(unit_path)
    if allowed is None:
        return CheckResult(name, "skip", f"{unit_path} not installed")
    if not allowed:
        return CheckResult(name, "pass", "webhook unit is not ProtectSystem=strict")

    missing = _not_covered(trees, allowed)
    if missing:
        return CheckResult(
            name,
            "warn",
            f"{unit_path} is ProtectSystem=strict but does not allow writes to "
            f"{', '.join(missing)} — this machine hosts those environments, so "
            f"their deploys fail read-only (git fetch exits 255). The installed "
            f"unit is most likely the one rendered for another host",
            fix_hint=(
                "run `fraisier scaffold && sudo fraisier scaffold-install --yes` "
                "on this machine to install the unit rendered for it"
            ),
        )
    return CheckResult(
        name, "pass", f"{len(trees)} hosted tree(s) writable from the sandbox"
    )


def _sandbox_probe_command(paths: list[str]) -> list[str]:
    """Build the transient-unit command that writes into *paths* under strict.

    ``systemd-run`` with the same two directives the webhook unit carries, so
    the probe fails exactly where a deploy would. Property names are joined to
    their values (``-pKey=value``) because the shell-less argv form takes one
    token per property.
    """
    script = "; ".join(
        f'p={path!r}; : > "$p/.fraisier-probe" || {{ echo "$p: not writable" >&2; '
        f'exit 1; }}; rm -f "$p/.fraisier-probe"'
        for path in paths
    )
    return [
        "systemd-run",
        "--pipe",
        "--wait",
        "--quiet",
        "--collect",
        "-pProtectSystem=strict",
        f"-pReadWritePaths={' '.join(paths)}",
        "/bin/sh",
        "-c",
        script,
    ]


def _run_sandbox_probe(paths: list[str]) -> tuple[int, str]:
    """Execute the probe. Seam for tests — the real call needs root + systemd."""
    result = subprocess.run(
        _sandbox_probe_command(paths),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result.returncode, (result.stderr or result.stdout or "").strip()


def _rendered_webhook_readwritepaths(config: FraisierConfig, server: str) -> list[str]:
    """``ReadWritePaths=`` of the unit *this render* produces for *server*."""
    import tempfile

    from fraisier.scaffold.renderer import ScaffoldRenderer, webhook_source_for_server

    with tempfile.TemporaryDirectory() as tmp:
        renderer = ScaffoldRenderer(config, server=server)
        renderer.output_dir = Path(tmp)
        renderer.render()
        unit = Path(tmp) / webhook_source_for_server(config, server)
        return [
            ln.split("=", 1)[1].strip()
            for ln in unit.read_text().splitlines()
            if ln.startswith("ReadWritePaths=")
        ]


@register_check("sandbox_write_probe", privileged=True)
def _check_sandbox_write_probe(config: FraisierConfig | None) -> CheckResult:
    """Actually write into the rendered unit's sandbox (#325), opt-in.

    Every other check reads a path list and reasons about it. This one runs a
    real write under a real ``ProtectSystem=strict`` transient unit built from
    the **rendered** allowlist, so an operator can find the gap before
    ``scaffold-install`` rather than on the next deploy.

    Opt-in via ``fraisier doctor --probe-sandbox`` and skipped without root:
    ``systemd-run`` needs privileges, and a check that fails for lack of them
    is noise rather than signal.
    """
    name = "sandbox_write_probe"
    if config is None:
        return CheckResult(name, "skip", "no fraises.yaml")
    if os.geteuid() != 0:
        return CheckResult(name, "skip", "needs root to spawn a transient unit")

    server = _resolve_local_server(config)
    if server is None:
        return CheckResult(
            name, "skip", "cannot tell which logical server this machine is"
        )

    try:
        paths = _rendered_webhook_readwritepaths(config, server)
    except Exception as exc:
        return CheckResult(name, "fail", f"could not render the unit: {exc}")
    if not paths:
        return CheckResult(name, "skip", "rendered unit lists no ReadWritePaths")

    if shutil.which("systemd-run") is None:
        return CheckResult(name, "skip", "systemd-run not available")

    code, output = _run_sandbox_probe(paths)
    if code != 0:
        return CheckResult(
            name,
            "fail",
            f"a write inside the rendered sandbox failed: {output or f'exit {code}'}",
            fix_hint=(
                "the path exists and is writable from a login shell but not from "
                "inside ProtectSystem=strict — check the ReadWritePaths= list and "
                "the mount it sits on"
            ),
        )
    return CheckResult(name, "pass", f"wrote into {len(paths)} sandboxed path(s)")


@register_check("pre_migrate_dump_writable")
def _check_pre_migrate_dump_writable(config: FraisierConfig | None) -> CheckResult:
    """The dump gate must be able to write from inside the unit's sandbox (#317).

    The webhook unit runs ``ProtectSystem=strict``; a dump directory missing
    from its ``ReadWritePaths=`` fails with ``Read-only file system`` and the
    gate — correctly — aborts the deploy. Every deploy with pending migrations
    then fails closed.

    Nothing else catches it: the path is writable from a login shell, so
    ownership and free-space checks all pass. Only a write attempted from
    inside the sandbox reveals it, and the first signal was a failed
    production deploy.

    Reads the **installed** unit rather than the rendered one, so it also
    catches the upgrade-without-re-scaffold case, which is the likeliest way to
    still be broken after the template fix.
    """
    name = "pre_migrate_dump_writable"
    dump_dirs = _enabled_dump_dirs(config)
    if not dump_dirs:
        return CheckResult(name, "skip", "no pre_migrate_dump gate enabled")

    project = getattr(config, "project_name", None)
    if not project:
        return CheckResult(name, "skip", "no project_name in config")

    unit_path = _installed_webhook_unit(project)
    allowed = _strict_readwritepaths(unit_path)
    if allowed is None:
        return CheckResult(name, "skip", f"{unit_path} not installed")
    if not allowed:
        return CheckResult(name, "pass", "webhook unit is not ProtectSystem=strict")

    missing = _not_covered(dump_dirs, allowed)
    if missing:
        return CheckResult(
            name,
            "warn",
            f"{unit_path} is ProtectSystem=strict but does not allow writes to "
            f"{', '.join(missing)} — the dump gate will fail closed and block "
            f"every deploy with pending migrations",
            fix_hint=(
                "run `fraisier scaffold && sudo fraisier scaffold-install --yes` "
                "to regenerate the unit with the dump directory allowed"
            ),
        )
    return CheckResult(
        name, "pass", f"{len(dump_dirs)} dump dir(s) writable from the sandbox"
    )


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------


def run_all(
    config: FraisierConfig | None,
    *,
    only: list[str] | None = None,
    skip_network: bool = False,
    probe_sandbox: bool = False,
) -> list[CheckResult]:
    """Execute every registered check (or a filtered subset) and return results.

    Args:
        config: Loaded FraisierConfig or None if no fraises.yaml found.
        only: When non-empty, run only these check names.
        skip_network: When True, mark network-flagged checks as ``skip``
            instead of running them.
        probe_sandbox: When True, also run privileged checks — today the
            active sandbox write probe, which spawns a transient systemd unit
            and therefore stays out of a default pass.

    Returns:
        Results in registration order. Each check is independent — one
        failure never aborts the rest.
    """
    results: list[CheckResult] = []
    for name, entry in DOCTOR_CHECKS.items():
        if only and name not in only:
            continue
        if skip_network and entry.network:
            results.append(CheckResult(name, "skip", "skipped (--skip-network)"))
            continue
        if entry.privileged and not probe_sandbox:
            results.append(CheckResult(name, "skip", "skipped (needs --probe-sandbox)"))
            continue
        try:
            results.append(entry.fn(config))
        except Exception as exc:
            results.append(
                CheckResult(
                    name,
                    "fail",
                    f"check raised: {type(exc).__name__}: {exc}",
                )
            )
    return results


def summarize(results: list[CheckResult]) -> dict[str, int]:
    summary = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for r in results:
        summary[r.status] += 1
    return summary


@register_check("scaffold_artifact_coverage")
def _check_scaffold_artifact_coverage(config: FraisierConfig | None) -> CheckResult:
    """Every artifact ``fraisier scaffold`` renders must have a disposition.

    The identical assertion runs inside ``render()``. It is repeated here on
    purpose: ``install.sh`` executes on a live host mid-deploy, under the
    self-upgrade dynamic, so if the *first* place an undispositioned artifact
    could surface were the installer, the first person to see it would be a
    production webhook. Running it at render time and here means it is caught
    in CI or on the operator's terminal; the deploy-time check is the backstop,
    not the discovery mechanism.

    Uses a dry-run render, which writes nothing.
    """
    name = "scaffold_artifact_coverage"
    if config is None:
        return CheckResult(name, "skip", "no config loaded")

    from fraisier.scaffold.artifacts import (
        UndispositionedArtifacts,
        build_artifact_manifest,
    )
    from fraisier.scaffold.renderer import ScaffoldRenderer, resolve_local_server

    try:
        renderer = ScaffoldRenderer(config, server=resolve_local_server(config))
        manifest = build_artifact_manifest(renderer, renderer.render(dry_run=True))
    except UndispositionedArtifacts as exc:
        return CheckResult(
            name,
            "fail",
            f"{len(exc.sources)} rendered artifact(s) have no disposition, so "
            f"nothing states whether they get installed: "
            f"{', '.join(exc.sources)}",
            fix_hint=(
                "give each one a disposition in "
                "fraisier/scaffold/artifacts.py::_classify"
            ),
        )
    except ValidationError as exc:
        return CheckResult(name, "skip", f"could not classify scaffold: {exc}")
    except (OSError, ValueError) as exc:
        return CheckResult(name, "skip", f"could not render scaffold: {exc}")

    gaps = manifest.gaps()
    if gaps:
        listed = ", ".join(a.source for a in gaps)
        return CheckResult(
            name,
            "warn",
            f"{len(manifest.artifacts)} artifact(s) classified; "
            f"{len(gaps)} rendered but installed by nothing: {listed}",
            fix_hint=(
                "these are tracked gaps, not silent ones — see the notes in "
                "the artifact manifest for what each one breaks"
            ),
        )
    return CheckResult(
        name,
        "pass",
        f"{len(manifest.artifacts)} artifact(s) classified, "
        f"{len(manifest.installed())} installed by scaffold-install",
    )


@register_check("foreign_units")
def _check_foreign_units(config: FraisierConfig | None) -> CheckResult:
    """Units installed here that belong to a fraise running somewhere else.

    Before host scoping became fraise-aware (#336), two fraises sharing an
    environment name across two servers made each host install the other's
    units. The fix stops new ones arriving; it cannot remove what is already
    on disk, still enabled and possibly still serving traffic. So this
    reports them — with their owner, so the operator can tell a leftover
    from a deliberate co-location — and removes nothing. Only
    ``scaffold-install --prune-foreign`` acts.

    Uses a dry-run render, which writes nothing.
    """
    name = "foreign_units"
    if config is None:
        return CheckResult(name, "skip", "no config loaded")

    from fraisier.scaffold import foreign as foreign_mod

    try:
        units = foreign_mod.find_foreign_units(config)
    except ValidationError as exc:
        return CheckResult(name, "skip", f"could not classify scaffold: {exc}")
    except (OSError, ValueError) as exc:
        return CheckResult(name, "skip", f"could not render scaffold: {exc}")

    if not units:
        return CheckResult(name, "pass", "no units owned by a non-local fraise")

    listed = ", ".join(f"{u.unit_name} ({u.owner})" for u in units)
    return CheckResult(
        name,
        "warn",
        f"{len(units)} unit(s) installed here are owned by a fraise that does "
        f"not run on this host: {listed}",
        fix_hint=(
            "they may be another application's running services — review, then "
            "'fraisier scaffold-install --prune-foreign' to disable and delete"
        ),
    )
