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

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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


DOCTOR_CHECKS: dict[str, _CheckEntry] = {}


def register_check(name: str, *, network: bool = False) -> Callable[[CheckFn], CheckFn]:
    """Decorator: register a doctor check by name."""

    def deco(fn: CheckFn) -> CheckFn:
        DOCTOR_CHECKS[name] = _CheckEntry(fn=fn, network=network)
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
    try:
        unit = unit_path.read_text()
    except OSError:
        return CheckResult(name, "skip", f"{unit_path} not installed")

    if "ProtectSystem=strict" not in unit:
        return CheckResult(name, "pass", "webhook unit is not ProtectSystem=strict")

    allowed = {
        ln.split("=", 1)[1].strip()
        for ln in unit.splitlines()
        if ln.startswith("ReadWritePaths=")
    }
    missing = [
        d
        for d in dump_dirs
        if not any(d == a or d.startswith(a.rstrip("/") + "/") for a in allowed)
    ]
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
) -> list[CheckResult]:
    """Execute every registered check (or a filtered subset) and return results.

    Args:
        config: Loaded FraisierConfig or None if no fraises.yaml found.
        only: When non-empty, run only these check names.
        skip_network: When True, mark network-flagged checks as ``skip``
            instead of running them.

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
