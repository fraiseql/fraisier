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
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from packaging.version import InvalidVersion, Version

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


#: The interpreter floor (#435): ``requires-python`` and confiture's own.
PYTHON_FLOOR = (3, 14)


@register_check("python_version")
def _check_python_version(_config: FraisierConfig | None) -> CheckResult:
    minimum = PYTHON_FLOOR
    actual = sys.version_info[:3]
    detail = ".".join(str(p) for p in actual)
    if actual < minimum:
        return CheckResult(
            "python_version",
            "fail",
            f"Python {detail} < {'.'.join(str(p) for p in minimum)}",
            fix_hint=(
                f"move fraisier to Python {'.'.join(str(p) for p in minimum)}: "
                "`uv tool install --force --python 3.14 fraisier==<version>`"
            ),
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


#: Where installed units live. A module constant so tests can point it at a
#: temporary tree rather than needing a real systemd.
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")

#: systemd allows these before the executable in ``ExecStart=``: ``@`` (override
#: argv[0]), ``-`` (ignore failure), ``:`` (no variable expansion), ``+``/``!``/
#: ``!!`` (privilege modifiers). They are not part of the path.
_EXEC_PREFIXES = "@-:+!"


def _exec_start_binary(line: str) -> str | None:
    """The executable named by an ``ExecStart=`` line, or None for other lines."""
    stripped = line.strip()
    directive, sep, value = stripped.partition("=")
    if not sep or directive.strip() != "ExecStart":
        return None
    value = value.strip().lstrip(_EXEC_PREFIXES).strip()
    if not value:
        return None
    return value.split()[0]


def _installed_webhook_unit(project_name: str) -> Path:
    """Where scaffold-install puts the webhook unit. Seam for tests."""
    return Path("/etc/systemd/system") / f"fraisier-{project_name}-webhook.service"


def _enabled_dump_dirs(config: FraisierConfig | None) -> list[str]:
    """Every ``pre_migrate_dump.output_dir`` for a gate that is switched on.

    Only for fraises that migrate: elsewhere no deploy ever writes a dump (#429).
    """
    from fraisier.fraise_roles import fraise_migrates

    dirs: list[str] = []
    fraises = getattr(config, "fraises", None) if config is not None else None
    for fraise in (fraises or {}).values():
        if not isinstance(fraise, dict):
            continue
        for env_config in (fraise.get("environments") or {}).values():
            if not isinstance(env_config, dict):
                continue
            if not fraise_migrates(fraise, env_config):
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


@dataclass(frozen=True)
class _DriftGate:
    """One enabled ``post_migrate_check``, as the doctor needs to see it."""

    fraise: str
    app_path: str
    confiture_config: str
    checks: tuple[str, ...]
    on_critical: str
    escalate: tuple[str, ...]

    @property
    def project_dir(self) -> Path:
        return Path(self.app_path)

    @property
    def config_path(self) -> Path:
        candidate = Path(self.confiture_config)
        return candidate if candidate.is_absolute() else self.project_dir / candidate


def _enabled_drift_gates(config: FraisierConfig | None) -> list[_DriftGate]:
    """Every enabled ``post_migrate_check`` gate across the config.

    Only for fraises that migrate: the gate defaults to on, and a scheduled or
    backup fraise has no migration for it to follow (#429).
    """
    from fraisier.fraise_roles import fraise_migrates
    from fraisier.post_migrate_check import load_post_migrate_check

    gates: list[_DriftGate] = []
    fraises = getattr(config, "fraises", None) if config is not None else None
    for fraise_name, fraise in (fraises or {}).items():
        if not isinstance(fraise, dict):
            continue
        for env_config in (fraise.get("environments") or {}).values():
            if not isinstance(env_config, dict):
                continue
            if not fraise_migrates(fraise, env_config):
                continue
            db = env_config.get("database") or {}
            gate = load_post_migrate_check(db)
            if not gate.enabled:
                continue
            app_path = env_config.get("app_path")
            if not app_path:
                continue
            gates.append(
                _DriftGate(
                    fraise=str(fraise_name),
                    app_path=str(app_path),
                    confiture_config=str(db.get("confiture_config", "confiture.yaml")),
                    checks=gate.checks,
                    on_critical=gate.on_critical,
                    escalate=gate.escalate,
                )
            )
    return gates


@register_check("post_migrate_check_buildable")
def _check_post_migrate_check_buildable(config: FraisierConfig | None) -> CheckResult:
    """The drift gate must be able to build the schema it compares against (#395).

    ``confiture build`` cannot be pointed at a config *path*, only at an
    environment *name* it resolves to ``<app_path>/db/environments/<name>.yaml``.
    A project whose ``confiture_config`` lives anywhere else — a root
    ``confiture.yaml``, say — gives the gate nothing to build, so it refuses.

    That refusal lands mid-deploy, **after** the migrations have been applied,
    which is the worst moment to discover a configuration problem. Nothing else
    catches it: the config path itself is perfectly valid for `migrate up`,
    which takes it directly.
    """
    name = "post_migrate_check_buildable"
    gates = _enabled_drift_gates(config)
    if not gates:
        return CheckResult(name, "skip", "no post_migrate_check gate enabled")

    from fraisier.dbops.drift import _env_for_build

    unresolvable: list[str] = []
    for gate in gates:
        try:
            _env_for_build(gate.project_dir, gate.config_path)
        except ValueError:
            unresolvable.append(f"{gate.fraise} ({gate.confiture_config})")

    if unresolvable:
        return CheckResult(
            name,
            "warn",
            f"post_migrate_check is enabled but `confiture build --env` cannot "
            f"resolve the config for {', '.join(unresolvable)}; the gate will "
            f"refuse mid-deploy, after the migrations have been applied",
            fix_hint=(
                "point database.confiture_config at "
                "db/environments/<env>.yaml, or disable the gate"
            ),
        )
    return CheckResult(name, "pass", f"{len(gates)} drift gate(s) can build a schema")


#: One ``ALTER TABLE`` statement, up to its terminator.
_ALTER_TABLE_RE = re.compile(r"\bALTER\s+TABLE\b.*?(?:;|\Z)", re.IGNORECASE | re.DOTALL)

#: A ``DROP`` inside one that takes a **column** away.  The alternatives excluded
#: here are the other things ``ALTER TABLE`` can drop — they are not folded into
#: the expected schema either, but only a missing column is graded CRITICAL.
#: Both spellings count: ``DROP COLUMN legacy`` and the bare ``DROP legacy``.
_DROP_COLUMN_RE = re.compile(
    r"\bDROP\s+(?!CONSTRAINT\b|DEFAULT\b|NOT\s+NULL\b|IDENTITY\b|EXPRESSION\b)"
    r"(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?[\w\"]+",
    re.IGNORECASE,
)


def _ddl_dirs(gate: _DriftGate) -> list[Path]:
    """The directories the gate's confiture config builds its schema from."""
    import yaml

    try:
        raw = yaml.safe_load(gate.config_path.read_text()) or {}
    except OSError, yaml.YAMLError:
        return []
    if not isinstance(raw, dict):
        return []
    dirs: list[Path] = []
    for entry in raw.get("include_dirs") or []:
        candidate = Path(str(entry))
        dirs.append(
            candidate if candidate.is_absolute() else gate.project_dir / candidate
        )
    return dirs


def _drops_a_column(sql: str) -> bool:
    return any(
        _DROP_COLUMN_RE.search(statement) for statement in _ALTER_TABLE_RE.findall(sql)
    )


#: The confiture release whose linting inventory folds ``ALTER TABLE … DROP
#: COLUMN`` into the table it alters, so the expected schema the drift gate
#: grades against stops carrying the dropped column (#415,
#: fraiseql/confiture#301).
#:
#: Bisected against a live database with
#: ``.phases/2026-09-23-confiture-1-18-probe/driver7.py``, which applies #407's
#: tree verbatim so anything reported is a false positive by construction:
#: 1.10.1 reports two CRITICAL ``missing_column`` items and fails the gate;
#: 1.11.0, 1.12.0, 1.13.0 and 1.14.0 report none.  1.10.1 is the control —
#: without a version that still reproduces, "clean everywhere" would equally
#: describe a probe that measures nothing.
_ALTER_DROP_FOLDED_IN = Version("1.11.0")


def _confiture_cli_version() -> Version | None:
    """The version of the ``confiture`` the gate will run, or ``None``.

    Read from the binary on PATH rather than from installed package metadata:
    ``dbops/drift.py`` spells the executable ``confiture`` and lets PATH
    resolve it, the two can differ, and it is the binary's answer that decides
    what the gate does.
    """
    binary = shutil.which("confiture")
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired, OSError:
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"\d+\.\d+\.\d+\S*", proc.stdout or "")
    if match is None:
        return None
    try:
        return Version(match.group(0))
    except InvalidVersion:
        return None


@register_check("post_migrate_check_alter_safe")
def _check_post_migrate_check_alter_safe(config: FraisierConfig | None) -> CheckResult:
    """A DDL tree that drops a column fails the drift gate on a *correct* database.

    confiture builds the gate's expected schema from its linting inventory, and
    that inventory folds only ``ADD COLUMN`` and ``ADD CONSTRAINT`` into a table.
    An ``ALTER TABLE … DROP COLUMN`` in the tree is invisible to it, so the
    column stays "expected" and a database applied verbatim from that same tree
    — correctly without it — is reported ``CRITICAL missing_column``.  With
    ``on_critical: fail`` that is a failed deploy on a correct migration, and
    the failure names a column rather than the cause (#407,
    fraiseql/confiture#301; reproduced on confiture 1.6.0 and 1.10.1).

    **confiture fixed this in 1.11.0**, so above that release the warning
    describes a failure that can no longer happen and the check passes
    (:data:`_ALTER_DROP_FOLDED_IN`, bisected — see #415).  It is gated rather
    than deleted because the floor is ``>=1.0.0``: a project resolving 1.10.1
    is inside the declared range, and there the warning is still true.

    Only ``live-drift`` reads that inventory, so a gate running ``signatures``
    alone is unaffected.
    """
    name = "post_migrate_check_alter_safe"
    gates = [g for g in _enabled_drift_gates(config) if "live-drift" in g.checks]
    if not gates:
        return CheckResult(
            name, "skip", "no post_migrate_check live-drift gate enabled"
        )

    scanned = 0
    offenders: list[str] = []
    for gate in gates:
        for ddl_dir in _ddl_dirs(gate):
            if not ddl_dir.is_dir():
                continue
            for sql_file in sorted(ddl_dir.rglob("*.sql")):
                try:
                    body = sql_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                scanned += 1
                if _drops_a_column(body):
                    offenders.append(f"{gate.fraise} ({sql_file})")

    if not scanned:
        return CheckResult(
            name, "skip", "no DDL files readable from here for the enabled gate(s)"
        )
    if offenders:
        # confiture fixed this upstream, so above that release the warning is
        # about a failure that can no longer happen.  The floor is `>=1.0.0`
        # and a project resolving 1.10.1 is inside the declared range, where
        # the warning is still true — hence a gate rather than a deletion.
        installed = _confiture_cli_version()
        if installed is not None and installed >= _ALTER_DROP_FOLDED_IN:
            return CheckResult(
                name,
                "pass",
                f"{scanned} DDL file(s) scanned; confiture {installed} folds "
                f"ALTER TABLE … DROP COLUMN into the expected schema, as every "
                f"release since {_ALTER_DROP_FOLDED_IN} does, so a dropped "
                f"column no longer reads as missing from a correct database",
            )
        return CheckResult(
            name,
            "warn",
            f"the drift gate will report false critical drift for "
            f"{', '.join(offenders)}: confiture's expected schema does not fold "
            f"ALTER TABLE … DROP COLUMN, so the dropped column reads as missing "
            f"from a correct database (fraiseql/confiture#301)",
            fix_hint=(
                f"upgrade confiture to {_ALTER_DROP_FOLDED_IN} or later, where "
                f"the drop is folded and this stops being a trap; or declare "
                f"the table's final shape in its CREATE TABLE and move the "
                f"drop to a migration, or set on_critical: warn"
            ),
        )
    return CheckResult(
        name, "pass", f"{scanned} DDL file(s) fold into the built schema"
    )


#: The confiture release that refuses to *compare* a schema whose identifiers
#: need quotes: ``DIFFER_403``, exit 5, on ``migrate validate
#: --check-live-drift`` — the command ``post_migrate_check`` runs
#: (fraiseql/confiture#505).
#:
#: Measured against a database applied verbatim from its own DDL, so every
#: finding is a false positive by construction: on 1.25.1 the gate answers
#: ``passed=True exit=0``; with #505 merged, ``passed=False exit=5``
#: (``.phases/2026-09-28-confiture-next-probe/probe_quoted.py``).
#:
#: .. note::
#:    #505 is on confiture's ``main`` under *Unreleased* and 1.25.1 is the
#:    latest tag, so this number is the *expected* home rather than a measured
#:    one — both probe venvs self-report 1.25.1 because confiture bumps its
#:    version in the release PR.  ``test_the_refusing_release_is_pinned``
#:    exists to make revisiting this a decision rather than an oversight.
_QUOTED_NAMES_REFUSED_IN = Version("1.26.0")

#: The keywords PostgreSQL cannot read as a bare identifier.  Generated, not
#: recalled -- ``SELECT word FROM pg_get_keywords() WHERE catcode IN ('R', 'T')``
#: on PostgreSQL 18.4, the grammar confiture parses with (pglast 8.4).
#:
#: The two categories that matter are not the ones their names suggest, so
#: both were measured as a **column** name rather than reasoned about:
#:
#: * ``R`` (reserved, 78) and ``T`` (type/function-name, 23) are syntax
#:   errors bare -- ``CREATE TABLE t (left text)`` does not parse.
#: * ``C`` (column-name, 63) parses bare and is deliberately **excluded**:
#:   ``CREATE TABLE t (between text)`` is accepted, and confiture does not
#:   refuse it either.  Including it would warn about names confiture takes.
#:
#: This matches confiture's own predicate, ``quote_identifier(name) != name``.
#: It inherits one over-report with it: a ``T`` word *is* bare-legal as a
#: **function** name (``CREATE FUNCTION similar()`` parses), and confiture
#: flags it anyway because its predicate does not look at the object's kind.
#: This check exists to predict the refusal rather than to be right about
#: PostgreSQL, so it follows confiture there on purpose.
_QUOTE_REQUIRING_KEYWORDS: frozenset[str] = frozenset(
    {
        "all",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "asymmetric",
        "authorization",
        "binary",
        "both",
        "case",
        "cast",
        "check",
        "collate",
        "collation",
        "column",
        "concurrently",
        "constraint",
        "create",
        "cross",
        "current_catalog",
        "current_date",
        "current_role",
        "current_schema",
        "current_time",
        "current_timestamp",
        "current_user",
        "default",
        "deferrable",
        "desc",
        "distinct",
        "do",
        "else",
        "end",
        "except",
        "false",
        "fetch",
        "for",
        "foreign",
        "freeze",
        "from",
        "full",
        "grant",
        "group",
        "having",
        "ilike",
        "in",
        "initially",
        "inner",
        "intersect",
        "into",
        "is",
        "isnull",
        "join",
        "lateral",
        "leading",
        "left",
        "like",
        "limit",
        "localtime",
        "localtimestamp",
        "natural",
        "not",
        "notnull",
        "null",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "outer",
        "overlaps",
        "placing",
        "primary",
        "references",
        "returning",
        "right",
        "select",
        "session_user",
        "similar",
        "some",
        "symmetric",
        "system_user",
        "table",
        "tablesample",
        "then",
        "to",
        "trailing",
        "true",
        "union",
        "unique",
        "user",
        "using",
        "variadic",
        "verbose",
        "when",
        "where",
        "window",
        "with",
    }
)

_BARE_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_$]*\Z")

#: A double-quoted identifier in SQL, with ``""`` standing for one quote.
_QUOTED_IDENTIFIER_RE = re.compile(r'"((?:[^"]|"")*)"')

#: Every span that ends where it begins, scanned in one left-to-right pass: a
#: quoted identifier, a ``$tag$ … $tag$`` body, a line comment, a block
#: comment, and a single-quoted literal.  All but the first are blanked before
#: the identifier scan, or ``-- renamed "createdAt"`` reads as an offender.
#:
#: A function or view body is blanked because it *refers* to names rather than
#: declaring them, and what it refers to may live in a schema this project does
#: not own — reporting that as a rename this project owes is the one outcome
#: that makes the whole check not worth believing.  Every declaration, the
#: routine's own name included, sits outside the body.
#:
#: **The quoted identifier has to be in this alternation**, even though it is
#: kept rather than blanked, because an identifier may contain a ``'`` and that
#: apostrophe must not open a string literal.  Blanking literals first —  which
#: is what this did — erased everything from a name like
#: ``"Licence d'impression"`` to the next apostrophe anywhere in the file.  On a
#: real schema that swallowed 89 lines into one multi-kilobyte "identifier" and,
#: far worse, **hid a genuine offender inside the erased span** — the one thing
#: this check exists to find.
#:
#: Ordering within the alternation is immaterial: no two of these can start at
#: the same character, so the leftmost match always belongs to whichever span
#: really opens first.  A ``"`` inside a literal is consumed by the literal, and
#: a ``'`` inside an identifier by the identifier, precisely because both are
#: matched here rather than in two passes.
_SQL_SPANS_RE = re.compile(
    r'"(?:[^"]|"")*"|\$(\w*)\$.*?\$\1\$|--[^\n]*|/\*.*?\*/|\'(?:[^\']|\'\')*\'',
    re.DOTALL,
)


def _identifier_scannable(sql: str) -> str:
    """*sql* with every non-identifier span blanked and identifiers left in place.

    Length is not preserved and does not need to be: nothing downstream reports
    an offset, only the names themselves.
    """
    return _SQL_SPANS_RE.sub(
        lambda m: m.group(0) if m.group(0).startswith('"') else " ", sql
    )


def _needs_quotes(name: str) -> bool:
    """Would PostgreSQL have to quote *name* to write it?

    One predicate with one table of cases, so the message and the finding
    cannot disagree about what an offender is.  A dot, a capital, a space or
    punctuation, a leading digit, a non-ASCII letter, or a reserved word.
    """
    if not _BARE_IDENTIFIER_RE.match(name):
        return True
    return name in _QUOTE_REQUIRING_KEYWORDS


def _conforming_rename(name: str) -> str:
    """The name confiture's own ``actionable`` would suggest: bare, lower-case."""
    folded = "".join(ch if ch.isalnum() or ch in "_$" else "_" for ch in name.lower())
    if not folded or not (folded[0].isalpha() or folded[0] == "_"):
        folded = f"_{folded}"
    return f"{folded}_" if folded in _QUOTE_REQUIRING_KEYWORDS else folded


def _offending_identifiers(sql: str) -> list[str]:
    """Every identifier in *sql* that PostgreSQL cannot write bare, in order.

    Deduplicated but order-preserving: a column repeated across twenty tables
    is one thing to rename, and the operator should see the twenty *distinct*
    names rather than the first name twenty times.
    """
    scannable = _identifier_scannable(sql)
    seen: dict[str, None] = {}
    for match in _QUOTED_IDENTIFIER_RE.finditer(scannable):
        name = match.group(1).replace('""', '"')
        if _needs_quotes(name):
            seen.setdefault(name, None)
    return list(seen)


#: How many offenders the message names before it falls back to a count.  An
#: ORM tree can carry hundreds, and a doctor line nobody reads to the end
#: reports nothing.
_NAMES_SHOWN = 5


def _built_schema(gate: _DriftGate) -> str | None:
    """The schema *gate* builds, as the drift check would build it.

    ``None`` when it cannot be built from here — an unresolvable environment,
    a failing build, an unreadable output.  The doctor says nothing rather
    than guessing; :func:`_check_post_migrate_check_config_resolves` is the
    check that reports an unbuildable gate.
    """
    from fraisier.dbops import drift

    try:
        env_name = drift._env_for_build(gate.project_dir, gate.config_path)
    except ValueError, OSError:
        return None
    with tempfile.TemporaryDirectory(prefix="fraisier-doctor-names-") as tmp:
        output = Path(tmp) / "expected_schema.sql"
        try:
            build = drift.build_expected_schema(
                project_dir=gate.project_dir, env_name=env_name, output=output
            )
        except OSError:
            return None
        if build.returncode != 0 or not output.is_file():
            return None
        try:
            return output.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None


@register_check("post_migrate_check_names_conform")
def _check_post_migrate_check_names_conform(
    config: FraisierConfig | None,
) -> CheckResult:
    """A schema whose names need quotes cannot be compared at all (#505).

    confiture supports an identifier only as PostgreSQL writes it bare.  From
    :data:`_QUOTED_NAMES_REFUSED_IN` that stopped being advice: everywhere
    confiture *compares or generates* a schema it now refuses one — and the DDL
    side of ``migrate validate --check-live-drift`` is one of those places,
    which is the command ``post_migrate_check`` runs.  The refusal is
    ``DIFFER_403`` at exit 5, it has no opt-out flag, and a ``lint --baseline``
    that absorbs ``naming_003``/``naming_004`` does not absorb it.

    So an ORM-shaped schema — ``"createdAt"``, ``"userName"`` — does not drift:
    it becomes *ungradable*, and a deploy running ``on_critical: fail`` stops
    with the migrations already applied.  Nothing about that deploy was wrong,
    which is why it belongs at doctor time.

    The check reads the **built** schema rather than the DDL files, because the
    built schema is what confiture is handed and the two differ: a name can be
    introduced by an included directory the tree does not obviously own.  It is
    version-gated for the same reason :data:`_ALTER_DROP_FOLDED_IN` is — below
    the refusing release the advice describes a refusal the project will not
    meet.

    Only ``live-drift`` compares a built schema, so a gate running
    ``signatures`` alone is unaffected.
    """
    name = "post_migrate_check_names_conform"
    gates = [g for g in _enabled_drift_gates(config) if "live-drift" in g.checks]
    if not gates:
        return CheckResult(
            name, "skip", "no post_migrate_check live-drift gate enabled"
        )

    # Cheapest question first: below the refusing release there is nothing to
    # warn about, and asking costs a `--version` instead of a whole build.
    installed = _confiture_cli_version()
    if installed is not None and installed < _QUOTED_NAMES_REFUSED_IN:
        return CheckResult(
            name,
            "skip",
            f"confiture {installed} compares a schema whose names need quotes; "
            f"the refusal arrives in {_QUOTED_NAMES_REFUSED_IN}",
        )

    built = 0
    offenders: dict[str, None] = {}
    for gate in gates:
        schema = _built_schema(gate)
        if schema is None:
            continue
        built += 1
        for identifier in _offending_identifiers(schema):
            offenders.setdefault(identifier, None)

    if not built:
        return CheckResult(
            name, "skip", "no gate's expected schema could be built from here"
        )
    if not offenders:
        return CheckResult(
            name,
            "pass",
            f"every identifier in {built} built schema(s) is one PostgreSQL "
            f"writes bare",
        )

    found = list(offenders)
    shown = found[:_NAMES_SHOWN]
    listing = ", ".join(f'"{n}" → {_conforming_rename(n)}' for n in shown)
    more = len(found) - len(shown)
    tail = f", and {more} more" if more else ""
    dotted = [n for n in found if "." in n]
    misparse = (
        f'; "{dotted[0]}" carries a dot, so it is misread as '
        f"{dotted[0].split('.', 1)[0]}.{dotted[0].split('.', 1)[1]} — whatever "
        f"refers to it resolves somewhere else (naming_003)"
        if dotted
        else ""
    )
    return CheckResult(
        name,
        "warn",
        f"the drift gate cannot compare this schema: {len(found)} identifier(s) "
        f"need quotes, which confiture refuses from "
        f"{_QUOTED_NAMES_REFUSED_IN} on every path that compares a schema "
        f"(DIFFER_403, exit 5) — {listing}{tail}{misparse}",
        fix_hint=(
            "rename each to a name PostgreSQL writes bare, in the DDL and in "
            "a migration; `confiture lint` lists every one (naming_003, "
            "naming_004). There is no opt-out flag, and a lint baseline does "
            "not absorb this — until they are renamed, set post_migrate_check."
            "checks to [signatures], which compares no built schema"
        ),
    )


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


def _database_urls(config: FraisierConfig | None) -> dict[str, str]:
    """Each configured ``database.database_url``, keyed ``fraise/env``.

    An ``!envvar`` that is not set here is left out rather than raised: the
    doctor's job is to report, and ``fraises_yaml_resolves`` already says so.
    """
    from fraisier.dbops._url import resolve_db_url
    from fraisier.errors import ConfigurationError

    urls: dict[str, str] = {}
    fraises = getattr(config, "fraises", None) if config is not None else None
    for fraise_name, fraise in (fraises or {}).items():
        if not isinstance(fraise, dict):
            continue
        for env_name, env_config in (fraise.get("environments") or {}).items():
            if not isinstance(env_config, dict):
                continue
            raw = (env_config.get("database") or {}).get("database_url")
            try:
                url = resolve_db_url(raw)
            except ConfigurationError:
                continue
            if url:
                urls[f"{fraise_name}/{env_name}"] = url
    return urls


@register_check("pg_tviews_contract", network=True)
def _check_pg_tviews_contract(config: FraisierConfig | None) -> CheckResult:
    """pg_tviews must speak read contract 1 (0.1.0-beta.20 or later).

    confiture 1.29 refuses an older pg_tviews with ``CONFIG_014`` wherever it
    reads TVIEWs live, and the drift gate is on by default — so on an old host
    every deploy of a TVIEW project fails its gate, after the migrations ran.

    ``pg_extension.extversion`` cannot tell the versions apart: it reads
    ``0.1.0`` on every beta.  ``tviews.contract_version()`` is the one thing
    confiture itself checks, so the check calls it.  A database without the
    extension is none of this check's business, and one it cannot reach is a
    skip rather than a verdict.
    """
    import psycopg

    from fraisier.dbops.tviews import read_support

    name = "pg_tviews_contract"
    urls = _database_urls(config)
    if not urls:
        return CheckResult(name, "skip", "no database.database_url configured")

    old: list[str] = []
    seen = 0
    unreachable: list[str] = []
    for target, url in urls.items():
        try:
            with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
                support = read_support(conn)
        except psycopg.Error as exc:
            unreachable.append(f"{target}: {str(exc).splitlines()[0]}")
            continue
        if support.state == "absent":
            continue
        seen += 1
        if support.state == "outdated":
            old.append(target)

    if old:
        return CheckResult(
            name,
            "fail",
            f"pg_tviews on {', '.join(old)} predates read contract 1 "
            f"(0.1.0-beta.20): confiture 1.29 refuses it with CONFIG_014, so "
            f"every deploy of a TVIEW project fails its drift gate",
            fix_hint=(
                "upgrade pg_tviews to 0.1.0-beta.20 or later and run its "
                "scripts/migrate-from-0.1.0.sql on each database"
            ),
        )
    if not seen:
        if unreachable:
            return CheckResult(
                name, "skip", f"could not reach {'; '.join(unreachable)}"
            )
        return CheckResult(name, "skip", "pg_tviews is not installed")
    return CheckResult(name, "pass", f"pg_tviews read contract 1 on {seen} database(s)")


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


@register_check("inert_timers")
def _check_inert_timers(config: FraisierConfig | None) -> CheckResult:
    """Which switchable timers this host runs, and which it only carries.

    Always a ``pass``: every family off is a configured state, not a defect,
    and warning about a deliberate choice on every run is how a report becomes
    wallpaper — the more so now that ``scaffold_artifact_coverage`` has gone
    quiet for the first time (#341 installed the last declared gap).

    What it adds is that the state is *legible*. `backup.timer` and
    `deploy-checker.timer` were copied to every host and started on none, and
    nothing distinguished "copied and running" from "copied and never
    started" — which is precisely why three units stayed broken for the
    project's whole history.
    """
    name = "inert_timers"
    if config is None:
        return CheckResult(name, "skip", "no config loaded")

    from fraisier.config.schema import TIMER_FAMILIES

    timers = config.scaffold.systemd.timers
    on = sorted(f for f in TIMER_FAMILIES if timers.get(f))
    off = sorted(f for f in TIMER_FAMILIES if not timers.get(f))

    detail = f"enabled: {', '.join(on) if on else 'none'}"
    if off:
        detail += f"; installed and not enabled: {', '.join(off)}"

    return CheckResult(
        name,
        "pass",
        detail,
        fix_hint=(
            f"set scaffold.systemd.timers.<name>: true and re-run "
            f"scaffold-install to start one ({', '.join(off)})"
        )
        if off
        else None,
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


def _stale_app_unit_candidates(config: FraisierConfig) -> dict[str, str]:
    """Unit name -> the ``Description=`` its app unit was rendered with.

    One entry per fraise and environment that does not serve, under each name
    an app unit could have been installed as: the generated
    ``{project}_{fraise}_{env}.service``, and ``systemd_service`` when set.
    """
    from fraisier.fraise_roles import fraise_serves
    from fraisier.naming import app_service_name

    project = config.project_name
    candidates: dict[str, str] = {}
    for fraise_name, fraise in (config.fraises or {}).items():
        if not isinstance(fraise, dict):
            continue
        for env_name, env_config in (fraise.get("environments") or {}).items():
            if not isinstance(env_config, dict) or fraise_serves(fraise, env_config):
                continue
            description = f"Description={fraise_name} ({env_name})"
            candidates[f"{project}_{fraise_name}_{env_name}.service"] = description
            candidates[app_service_name(project, fraise_name, env_name, env_config)] = (
                description
            )
    return candidates


@register_check("stale_app_units")
def _check_stale_app_units(config: FraisierConfig | None) -> CheckResult:
    """App units installed for a fraise that serves nothing (#432).

    Before #432 the scaffold rendered ``core/service.j2`` — uvicorn on port
    8000 — for every fraise, and ``fraisier setup`` copied and enabled each.
    Nothing renders those units now, so no manifest knows them and
    ``foreign_units`` cannot see them, while an enabled one still binds the
    API's port at boot.

    A unit counts only if it still carries the ``Description=`` line that
    template has written since v0.3.0: a scheduled fraise's own
    ``systemd_service`` shares the name, not the body. Removes nothing.
    """
    name = "stale_app_units"
    if config is None:
        return CheckResult(name, "skip", "no config loaded")
    if not SYSTEMD_UNIT_DIR.is_dir():
        return CheckResult(name, "skip", f"{SYSTEMD_UNIT_DIR} is not a directory")
    try:
        installed = {p.name: p for p in SYSTEMD_UNIT_DIR.glob("*.service")}
    except OSError as exc:
        return CheckResult(name, "skip", f"could not read {SYSTEMD_UNIT_DIR}: {exc}")

    stale: list[tuple[str, bool]] = []
    for unit, description in sorted(_stale_app_unit_candidates(config).items()):
        path = installed.get(unit)
        if path is None:
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError, UnicodeDecodeError:
            continue
        if description not in lines:
            continue
        enabled = any(SYSTEMD_UNIT_DIR.glob(f"*.wants/{unit}"))
        stale.append((unit, enabled))

    if not stale:
        return CheckResult(
            name, "pass", "no app unit installed for a non-serving fraise"
        )

    listed = ", ".join(
        f"{unit} ({'enabled' if enabled else 'disabled'})" for unit, enabled in stale
    )
    commands = [
        line
        for unit, _ in stale
        for line in (
            f"sudo systemctl disable --now {unit}",
            f"sudo rm {SYSTEMD_UNIT_DIR / unit}",
        )
    ]
    return CheckResult(
        name,
        "warn",
        f"{len(stale)} app unit(s) installed for a fraise that serves nothing, "
        f"left by a fraisier before #432 — an enabled one starts uvicorn on "
        f"port 8000 at boot: {listed}",
        fix_hint="\n".join([*commands, "sudo systemctl daemon-reload"]),
    )


@register_check("backup_corpus_free_space")
def _check_backup_corpus_free_space(config: FraisierConfig | None) -> CheckResult:
    """Room on the volumes holding the corpora this host receives (#344).

    ``check_disk_space`` guarded the host that *produces* a dump; a host that
    receives one by rsync had nothing, and #339's first cause was
    ``/backup/production`` on the destination filling up.

    Retention is not this check. It bounds the corpus in the steady state, and
    a correct policy can coexist with a full volume — something else on the
    disk grew, or ``keep_minimum`` is deliberately refusing to delete below its
    floor while the producer has stalled. Bounding is not alarming.

    ``min_free_gb`` is optional and absent means *no threshold*, which is every
    config written before it. Those report their free space and pass, so the
    absence is declared rather than mistaken for a guarantee (#341's rule).
    """
    name = "backup_corpus_free_space"
    if config is None:
        return CheckResult(name, "skip", "no config loaded")

    from fraisier.dbops.backup import free_space_gb

    try:
        entries = config.all_retain_entries()
    except ValidationError as exc:
        return CheckResult(name, "skip", f"invalid retention config: {exc}")

    if not entries:
        return CheckResult(name, "skip", "no backup retention declared")

    lines: list[str] = []
    breached: list[str] = []
    unmeasurable: list[str] = []
    missing: list[str] = []

    for entry in entries:
        if not Path(entry.dir).is_dir():
            missing.append(entry.dir)
            lines.append(f"{entry.name}: {entry.dir} is not a directory")
            continue
        try:
            free_gb = free_space_gb(entry.dir)
        except OSError as exc:
            # "I could not measure" is never "there is room" — the same rule
            # `db restore` applies to a lock it cannot evaluate and #342 applies
            # to an archive it cannot read.
            unmeasurable.append(f"{entry.dir}: {exc}")
            lines.append(f"{entry.name}: {entry.dir} — free space unreadable: {exc}")
            continue

        if entry.min_free_gb is None:
            lines.append(
                f"{entry.name}: {entry.dir} — {free_gb:.1f}GB free, no threshold set"
            )
            continue

        state = "OK" if free_gb >= entry.min_free_gb else "BELOW"
        lines.append(
            f"{entry.name}: {entry.dir} — {free_gb:.1f}GB free, "
            f"need {entry.min_free_gb}GB ({state})"
        )
        if free_gb < entry.min_free_gb:
            breached.append(f"{entry.dir} ({free_gb:.1f}GB < {entry.min_free_gb}GB)")

    listed = "; ".join(lines)

    if breached or missing:
        hints = []
        if breached:
            hints.append(
                "free space on " + ", ".join(breached) + " — a retention policy "
                "bounds the corpus but cannot recover a disk something else filled"
            )
        if missing:
            hints.append(
                "create " + ", ".join(missing) + " or correct the retain entry: a "
                "policy pointed at a path that is not there prunes nothing, every "
                "night, reporting success"
            )
        return CheckResult(name, "fail", listed, fix_hint="; ".join(hints))

    if unmeasurable:
        return CheckResult(name, "skip", listed)

    return CheckResult(name, "pass", listed)


@register_check("backup_retention")
def _check_backup_retention(config: FraisierConfig | None) -> CheckResult:
    """Corpora this host says it keeps, and whether anything prunes them.

    Both kinds: a received corpus (``backup.retain``, #339) and a
    ``pre_migrate_dump`` gate's own directory (#420), whose prune used to run
    only inside a deploy.

    #339's incident: a destination host received a nightly corpus, the
    unit meant to prune it was hand-written in the consuming repo, and it
    was never installed there. The disk filled. Nothing reported it,
    because nothing knew the policy existed.

    ``scaffold-diff`` now reports a missing retention unit for free — the
    units are fraisier's, so they are in the artifact manifest. This
    answers the operator's version of the question instead: which corpora
    are declared, and is each one's pair actually on disk.

    Uses a dry-run render, which writes nothing.
    """
    name = "backup_retention"
    if config is None:
        return CheckResult(name, "skip", "no config loaded")

    from fraisier.scaffold.renderer import ScaffoldRenderer
    from fraisier.scaffold.retention import (
        pre_migrate_prune_report,
        retention_report,
    )

    try:
        renderer = ScaffoldRenderer(config)
        report = [*retention_report(renderer), *pre_migrate_prune_report(renderer)]
    except ValidationError as exc:
        return CheckResult(name, "skip", f"invalid retention config: {exc}")
    except (OSError, ValueError) as exc:
        return CheckResult(name, "skip", f"could not read the scaffold: {exc}")

    if not report:
        return CheckResult(name, "skip", "no backup retention declared")

    missing = [entry for entry in report if not entry.installed]
    listed = "; ".join(entry.detail for entry in report)
    if not missing:
        return CheckResult(name, "pass", listed)

    return CheckResult(
        name,
        "warn",
        listed,
        fix_hint=(
            "run 'fraisier scaffold-install' on this host to install and "
            "enable the retention timers; until then nothing is pruning "
            f"{', '.join(entry.dir for entry in missing)}"
        ),
    )


@register_check("pgbackrest_helper")
def _check_pgbackrest_helper(config: FraisierConfig | None) -> CheckResult:
    """Each pgBackRest-restoring environment here has a helper to talk to (#424).

    A refresh stops the app service before it asks the helper for anything, so a
    helper that is not there costs a stopped service for nothing — and the helper
    exists on a host only after ``scaffold-install``, which an upgrade does not
    run.  This says so before the nightly timer finds out.

    *Installed* and *listening* are reported separately because the remedies
    differ: install it, or find out why an installed one is not running.

    Uses a dry-run render, which writes nothing.
    """
    name = "pgbackrest_helper"
    if config is None:
        return CheckResult(name, "skip", "no config loaded")

    from fraisier.scaffold.renderer import ScaffoldRenderer
    from fraisier.scaffold.retention import pgbackrest_helper_report

    try:
        report = pgbackrest_helper_report(ScaffoldRenderer(config))
    except ValidationError as exc:
        return CheckResult(name, "skip", f"invalid restore config: {exc}")
    except (OSError, ValueError) as exc:
        return CheckResult(name, "skip", f"could not read the scaffold: {exc}")

    if not report:
        return CheckResult(name, "skip", "no environment restores from pgBackRest")

    missing = [r.scope for r in report if not r.installed]
    silent = [r.scope for r in report if r.installed and not r.listening]
    if missing:
        return CheckResult(
            name,
            "warn",
            f"pgBackRest helper not installed for {', '.join(missing)}",
            fix_hint=(
                "run 'fraisier scaffold && sudo fraisier scaffold-install --yes' on "
                "this host; until then a pgBackRest refresh cannot stop or restore "
                "its cluster"
            ),
        )
    if silent:
        return CheckResult(
            name,
            "warn",
            f"pgBackRest helper installed but not listening for {', '.join(silent)}",
            fix_hint=(
                "systemctl status the helper's .socket unit "
                "(fraisier-<project>-<fraise>-<env>-pgbackrest-helper.socket) and "
                "journalctl -u its .service"
            ),
        )
    return CheckResult(
        name, "pass", f"{len(report)} pgBackRest helper(s) installed and listening"
    )


@register_check("deferred_restarts")
def _check_deferred_restarts(config: FraisierConfig | None) -> CheckResult:
    """Units installed by a deploy and still running their previous version.

    ``install.sh`` may not restart a unit that hosts a deploy — the webhook runs
    deploys in process, so restarting it mid-deploy SIGKILLs the deploy that
    asked for the install (#349). It records what it deferred instead, and the
    deploy pays that back once its lock releases.

    This is the backstop for a debt that was never paid: a drain that timed out,
    a unit the systemctl helper refuses (it allowlists services, not sockets), or
    a deploy run by the socket-activated ``deploy-daemon``, whose detached worker
    can be killed with its own per-connection service instance.

    ``warn``, not ``fail``: the host is running, just on an older unit. What it
    costs is a stale ``ReadWritePaths=`` or ``Environment=``, which is how a
    later deploy meets a read-only filesystem.
    """
    name = "deferred_restarts"
    if config is None:
        return CheckResult(name, "skip", "no config loaded")

    from fraisier.deferred_restart import read_deferred_restarts

    lock_dir = getattr(getattr(config, "deployment", None), "lock_dir", None)
    if not lock_dir:
        return CheckResult(name, "skip", "no deployment.lock_dir configured")

    pending = read_deferred_restarts(Path(lock_dir))
    if not pending:
        return CheckResult(name, "pass", f"nothing pending under {lock_dir}")

    return CheckResult(
        name,
        "warn",
        f"installed but not restarted: {', '.join(pending)}",
        fix_hint=(
            "these units are on disk and daemon-reloaded but are running their "
            "previous version; restart them once no deploy is in flight: "
            f"sudo systemctl restart {' '.join(pending)}"
        ),
    )


@register_check("unit_entrypoints")
def _check_unit_entrypoints(_config: FraisierConfig | None) -> CheckResult:
    """Every installed unit names a fraisier binary that is still executable (#351).

    A failed ``uv tool install --force`` can leave the tool venv half-removed —
    ``bin/`` gone, ``lib/`` intact — so every ``~/.local/bin/fraisier*`` symlink
    dangles, including the one the webhook unit names in ``ExecStart=``. The
    running process outlives its deleted binary, so ``is-active``, the health
    check and the version endpoint all look normal. The damage surfaces only at
    the next restart, as status 203/EXEC.

    This asks the question directly and does not care how the host got there: a
    failed self-upgrade, a half-finished manual install and a pruned venv all
    read the same. It takes no config — the units on disk are the input — so it
    still answers on a host whose ``fraises.yaml`` will not load.

    ``fail``, not ``warn``: unlike a deferred restart, this unit cannot start at
    all. The service running now is the last one that ever will, until it is
    fixed.
    """
    name = "unit_entrypoints"
    try:
        unit_files = sorted(SYSTEMD_UNIT_DIR.glob("*.service"))
    except OSError as exc:
        return CheckResult(name, "skip", f"could not read {SYSTEMD_UNIT_DIR}: {exc}")
    if not unit_files:
        return CheckResult(name, "skip", f"no units installed under {SYSTEMD_UNIT_DIR}")

    broken: list[tuple[str, str]] = []
    checked = 0
    for unit in unit_files:
        try:
            text = unit.read_text()
        except OSError, UnicodeDecodeError:
            # A unit we cannot read is not evidence of anything; the artifact
            # coverage check is what reports units that should not be here.
            continue
        for line in text.splitlines():
            binary = _exec_start_binary(line)
            if binary is None or not Path(binary).name.startswith("fraisier"):
                continue
            checked += 1
            # Existence first: os.access(X_OK) is permissive for root, so a
            # root-run doctor would otherwise call a missing file executable.
            target = Path(binary)
            if not target.exists() or not os.access(binary, os.X_OK):
                broken.append((unit.name, binary))

    if not checked:
        # Distinct from "pass" on purpose: a scan that matched nothing must not
        # read as a clean bill of health.
        return CheckResult(
            name,
            "skip",
            f"no fraisier entrypoints named by units in {SYSTEMD_UNIT_DIR}",
        )
    if not broken:
        return CheckResult(name, "pass", f"{checked} unit entrypoint(s) resolve")

    listed = "; ".join(f"{unit} -> {binary}" for unit, binary in broken)
    return CheckResult(
        name,
        "fail",
        f"{len(broken)} of {checked} unit entrypoint(s) do not resolve: {listed}",
        fix_hint=(
            "these units cannot start — the next restart fails 203/EXEC. A "
            "failed `uv tool install --force` leaves the tool venv half-removed "
            "(bin/ gone, lib/ intact). Reinstall:\n"
            "  sudo find ~/.local/share/uv/tools -name __pycache__ "
            "! -user $(id -un) -type d -exec rm -rf {} +\n"
            "  uv tool install --force fraisier==<version>\n"
            "then verify with `ls -l ~/.local/bin/fraisier*`."
        ),
    )


def _venv_python(binary: str) -> tuple[int, ...] | None:
    """The Python the venv holding *binary* was built on, or ``None``.

    Read from ``pyvenv.cfg`` (``version_info`` under uv, ``version`` under
    ``venv``), not by running the interpreter: the doctor may be unprivileged,
    and a ProtectHome'd or half-removed venv must read as "unknown", never as a
    pass.
    """
    resolved = Path(binary).resolve()
    if resolved.parent.name != "bin":
        return None
    try:
        text = (resolved.parent.parent / "pyvenv.cfg").read_text()
    except OSError, UnicodeDecodeError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("version_info", "version"):
            parts = value.strip().split(".")[:3]
            if len(parts) >= 2 and all(p.isdigit() for p in parts[:2]):
                return tuple(int(p) for p in parts if p.isdigit())
    return None


@register_check("unit_interpreter")
def _check_unit_interpreter(_config: FraisierConfig | None) -> CheckResult:
    """Every installed unit runs on the Python floor (#435).

    fraisier requires Python 3.14. A tool venv installed on 3.13 keeps serving
    the release it has, but a self-upgrade is pinned to the interpreter it runs
    on and refuses every release from the floor change on, by design. The host
    looks healthy and is stuck, so this reads the venv each unit's
    ``ExecStart=`` binary lives in rather than the interpreter the doctor was
    started with.

    ``warn``, not ``fail``, matching ``self_upgrade_failure``: the host is up
    and serving. What it has lost is the ability to receive a release. A venv
    whose version cannot be read is a ``skip``, never a pass.
    """
    name = "unit_interpreter"
    try:
        unit_files = sorted(SYSTEMD_UNIT_DIR.glob("*.service"))
    except OSError as exc:
        return CheckResult(name, "skip", f"could not read {SYSTEMD_UNIT_DIR}: {exc}")

    stale: list[str] = []
    read = 0
    for unit in unit_files:
        try:
            text = unit.read_text()
        except OSError, UnicodeDecodeError:
            continue
        for line in text.splitlines():
            binary = _exec_start_binary(line)
            if binary is None or not Path(binary).name.startswith("fraisier"):
                continue
            found = _venv_python(binary)
            if found is None:
                continue
            read += 1
            if found[:2] < PYTHON_FLOOR:
                stale.append(f"{unit.name} -> Python {'.'.join(map(str, found))}")

    if not read:
        return CheckResult(
            name, "skip", "no unit names a fraisier venv whose Python can be read"
        )
    if not stale:
        return CheckResult(name, "pass", f"{read} unit venv(s) on Python >= 3.14")
    return CheckResult(
        name,
        "warn",
        f"below the Python 3.14 floor: {'; '.join(stale)}",
        fix_hint=(
            "this host keeps serving, but self-upgrade is pinned to the running "
            "interpreter and refuses releases that need 3.14, so it cannot "
            "receive them. Move it by hand:\n"
            "  uv tool install --force --python 3.14 fraisier==<version>"
        ),
    )


_REMOTE_DEBUG_VAR = "PYTHON_DISABLE_REMOTE_DEBUG"


def _service_directives(path: Path) -> list[tuple[str, str]]:
    """``(key, value)`` pairs of a unit file's ``[Service]`` section, in order."""
    section = None
    directives: list[tuple[str, str]] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        key, sep, value = line.partition("=")
        if section == "[Service]" and sep:
            directives.append((key.strip(), value.strip()))
    return directives


def _env_file_defines(spec: str, var: str) -> bool:
    """Whether the ``EnvironmentFile=`` *spec* assigns *var*; unreadable is no."""
    try:
        text = Path(spec.removeprefix("-")).read_text()
    except OSError, UnicodeDecodeError:
        return False
    return any(
        line.strip().partition("=")[0].strip() == var for line in text.splitlines()
    )


def _remote_debug_lifted_by(unit: Path) -> Path | None:
    """Where the opt-out stops reaching *unit*'s process, or None if it reaches it.

    Follows systemd: the unit, then ``<unit>.d/*.conf`` in lexical order. An
    empty ``Environment=`` or ``EnvironmentFile=`` clears what came before it,
    ``UnsetEnvironment=`` applies last. Returns the unit itself when the
    variable is simply never set, else the file that took it away.
    """
    files = [unit, *sorted((unit.parent / f"{unit.name}.d").glob("*.conf"))]
    assigned = from_file = unset = False
    removed_by = unit
    for path in files:
        for key, value in _service_directives(path):
            if key == "Environment":
                tokens = shlex.split(value) if value else []
                if not tokens and assigned:
                    assigned, removed_by = False, path
                if any(t.partition("=")[0] == _REMOTE_DEBUG_VAR for t in tokens):
                    assigned = True
            elif key == "EnvironmentFile":
                if not value and from_file:
                    from_file, removed_by = False, path
                elif value and _env_file_defines(value, _REMOTE_DEBUG_VAR):
                    from_file = True
            elif key == "UnsetEnvironment":
                names = [t.partition("=")[0] for t in shlex.split(value)]
                if not names:
                    unset = False
                elif _REMOTE_DEBUG_VAR in names:
                    unset, removed_by = True, path
    if (assigned or from_file) and not unset:
        return None
    return removed_by


def _serving_app_units(config: FraisierConfig | None) -> set[str]:
    """Installed names of the app units fraisier renders for serving fraises."""
    if config is None:
        return set()
    from fraisier.fraise_roles import fraise_serves
    from fraisier.naming import app_service_name

    units: set[str] = set()
    for fraise_name, fraise in (getattr(config, "fraises", None) or {}).items():
        if not isinstance(fraise, dict):
            continue
        for env_name, env_config in (fraise.get("environments") or {}).items():
            if isinstance(env_config, dict) and fraise_serves(fraise, env_config):
                units.add(
                    app_service_name(
                        config.project_name, fraise_name, env_name, env_config
                    )
                )
    return units


@register_check("remote_debug_disabled")
def _check_remote_debug_disabled(config: FraisierConfig | None) -> CheckResult:
    """No fraisier unit on Python 3.14 accepts a PEP 768 remote attach (#436).

    3.14 lets anything allowed to ptrace a process inject Python into it while
    it runs. Every unit fraisier renders sets ``PYTHON_DISABLE_REMOTE_DEBUG``;
    this finds a unit installed before that, or one whose drop-in lifted it and
    was never reverted. It reads the **effective** environment, drop-ins
    included, because a drop-in is the documented way to lift it.

    CPython disables attach for any value, empty included, so "disabled" means
    "set". Judged: units running a fraisier binary, and the app units of
    serving fraises, each only once its venv's ``pyvenv.cfg`` says 3.14.
    """
    name = "remote_debug_disabled"
    if not SYSTEMD_UNIT_DIR.is_dir():
        return CheckResult(name, "skip", f"{SYSTEMD_UNIT_DIR} is not a directory")
    try:
        unit_files = sorted(SYSTEMD_UNIT_DIR.glob("*.service"))
    except OSError as exc:
        return CheckResult(name, "skip", f"could not read {SYSTEMD_UNIT_DIR}: {exc}")

    app_units = _serving_app_units(config)
    judged = 0
    open_units: list[str] = []
    for unit in unit_files:
        try:
            lines = unit.read_text().splitlines()
        except OSError, UnicodeDecodeError:
            continue
        binaries = [b for b in map(_exec_start_binary, lines) if b is not None]
        if unit.name not in app_units:
            binaries = [b for b in binaries if Path(b).name.startswith("fraisier")]
        versions = [v for v in map(_venv_python, binaries) if v is not None]
        if not versions or max(versions)[:2] < (3, 14):
            continue
        judged += 1
        try:
            lifted_by = _remote_debug_lifted_by(unit)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return CheckResult(name, "skip", f"could not read {unit.name}: {exc}")
        if lifted_by == unit:
            open_units.append(f"{unit.name} (not set)")
        elif lifted_by is not None:
            open_units.append(f"{unit.name} (lifted by {lifted_by})")

    if not judged:
        return CheckResult(
            name, "skip", "no fraisier unit runs a venv on Python 3.14 or newer"
        )
    if not open_units:
        return CheckResult(
            name, "pass", f"{judged} unit(s) refuse a PEP 768 remote attach"
        )
    return CheckResult(
        name,
        "warn",
        f"{len(open_units)} of {judged} unit(s) on Python 3.14 accept a PEP 768 "
        f"remote attach: {', '.join(open_units)}",
        fix_hint=(
            "a unit installed before #436: `fraisier scaffold && sudo fraisier "
            "scaffold-install --yes`. A drop-in that lifted it for debugging: "
            "`sudo systemctl revert <unit>`, then restart it"
        ),
    )


#: What ``root_unit_exec_trust`` reports while #433 has no fix shipped: the root
#: helpers still run from the deploy user's uv tool dir on every host. The
#: release that moves them to root-owned code flips this to ``"fail"``.
ROOT_EXEC_TRUST_STATUS: Status = "warn"

#: Where the file reader looks for drop-ins, lowest priority first: a drop-in in
#: a later root masks a same-named one in an earlier root. Only the fallback
#: reads these; ``systemctl show`` has already merged every drop-in it knows.
SYSTEMD_DROPIN_ROOTS: tuple[Path, ...] = (
    Path("/usr/lib/systemd/system"),
    Path("/usr/local/lib/systemd/system"),
    Path("/run/systemd/system"),
    SYSTEMD_UNIT_DIR,
)

#: Every directive that starts a process, in the order systemd runs them.
_EXEC_KEYS = (
    "ExecCondition",
    "ExecStartPre",
    "ExecStart",
    "ExecStartPost",
    "ExecReload",
    "ExecStop",
    "ExecStopPost",
)

#: ``*Ex`` carries the prefix flags; the plain property is asked for only to
#: tell a systemd too old to have ``*Ex`` (< 243) from a unit with no command.
_SHOW_PROPERTIES = (
    "LoadState",
    "NeedDaemonReload",
    "User",
    "DynamicUser",
    *_EXEC_KEYS,
    *(f"{key}Ex" for key in _EXEC_KEYS),
)

#: ``systemctl show`` flag names: ``privileged`` is ``+``, ``no-setuid`` is
#: ``!``. ``!!`` (``ambient``) is ignored wherever ambient capabilities exist,
#: and systemd 260 says so when it loads one, so it runs as ``User=``.
_PRIVILEGED_FLAGS = frozenset({"privileged", "no-setuid"})

#: The search path systemd uses for an ``Exec*=`` command that is not absolute.
_SYSTEMD_EXEC_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"

_EXEC_VALUE_RE = re.compile(r"([@\-:+!|]*)(.*)", re.DOTALL)

_SYMLINK_DEPTH = 40

_SCRIPT_RUNNING_HELPER = "fraisier-scaffold-install-helper"


@dataclass(frozen=True)
class _ExecCommand:
    key: str
    #: ``argv[0]`` is the executable, even under ``@`` (which renames argv[0]).
    argv: tuple[str, ...]
    #: ``+`` or ``!``: ``User=`` does not apply, the command runs as root.
    privileged: bool


@dataclass(frozen=True)
class _EffectiveService:
    user: str
    dynamic_user: bool
    commands: tuple[_ExecCommand, ...]

    def root_commands(self) -> list[_ExecCommand]:
        """The commands that run as root."""
        as_root = not self.dynamic_user and self.user in ("", "root", "0")
        return [c for c in self.commands if as_root or c.privileged]


def _systemd_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "yes", "y", "true", "t", "on")


def _parse_show_command(key: str, value: str) -> _ExecCommand | None:
    """One ``{ path=… ; argv[]=… ; flags=… ; … }`` record of a ``*Ex`` property."""
    body = value.strip().removeprefix("{").removesuffix("}")
    fields = {}
    for part in body.split(" ; "):
        field, _, content = part.strip().partition("=")
        fields[field] = content
    path = fields.get("path", "")
    if not path:
        return None
    args = fields.get("argv[]", "").split()[1:]
    flags = set(fields.get("flags", "").split())
    return _ExecCommand(key, (path, *args), bool(flags & _PRIVILEGED_FLAGS))


def _parse_systemctl_show(text: str) -> _EffectiveService | None:
    """The effective service in one unit's ``systemctl show`` output.

    ``None`` when the output cannot answer, so the files are read instead: a
    unit systemd has not loaded, one changed on disk since the last
    daemon-reload (the next start runs the file), or a systemd without ``*Ex``
    properties, where a ``+`` command is indistinguishable from a plain one.
    """
    props: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props.setdefault(key, []).append(value)

    def last(key: str) -> str:
        return (props.get(key) or [""])[-1]

    if last("LoadState") != "loaded" or last("NeedDaemonReload") == "yes":
        return None
    commands: list[_ExecCommand] = []
    for key in _EXEC_KEYS:
        if key in props and f"{key}Ex" not in props:
            return None
        for value in props.get(f"{key}Ex", []):
            command = _parse_show_command(key, value)
            if command is not None:
                commands.append(command)
    return _EffectiveService(
        user=last("User"),
        dynamic_user=_systemd_bool(last("DynamicUser")),
        commands=tuple(commands),
    )


def _split_show_output(text: str, names: list[str]) -> dict[str, str]:
    """``systemctl show a b`` prints one blank-line-separated block per unit."""
    blocks = [b.strip("\n") + "\n" for b in text.split("\n\n") if b.strip()]
    if len(blocks) != len(names):
        return {}
    return dict(zip(names, blocks, strict=True))


def _systemctl_show(names: list[str]) -> dict[str, str]:
    """Each unit's ``systemctl show`` block; empty when systemd cannot be asked."""
    if not names:
        return {}
    try:
        proc = subprocess.run(
            [
                "systemctl",
                "show",
                f"--property={','.join(_SHOW_PROPERTIES)}",
                "--",
                *names,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return {}
    if proc.returncode != 0:
        return {}
    return _split_show_output(proc.stdout, names)


def _dropin_dir_names(unit_name: str) -> list[str]:
    """Drop-in dirs that apply to *unit_name*, least specific first.

    ``service.d``, then each dash prefix (``a-.service.d`` for ``a-b.service``),
    then the template (``a@.service.d``), then the unit's own.
    """
    stem, _, suffix = unit_name.rpartition(".")
    prefix, at, instance = stem.partition("@")
    parts = prefix.split("-")
    names = [f"{suffix}.d"]
    names += [f"{'-'.join(parts[:i])}-.{suffix}.d" for i in range(1, len(parts))]
    if at and instance:
        names.append(f"{prefix}@.{suffix}.d")
    names.append(f"{unit_name}.d")
    return names


def _parse_exec_value(key: str, value: str, start_only: bool) -> _ExecCommand | None:
    match = _EXEC_VALUE_RE.match(value.strip())
    if match is None:
        return None
    prefix, rest = match.group(1), match.group(2)
    tokens = shlex.split(rest)
    if "@" in prefix:
        tokens = tokens[:1] + tokens[2:]
    if not tokens:
        return None
    privileged = start_only or "+" in prefix or ("!" in prefix and "!!" not in prefix)
    return _ExecCommand(key, tuple(tokens), privileged)


def _unit_file_service(unit: Path) -> _EffectiveService:
    """The effective service from the unit file and its drop-ins.

    The fallback for ``systemctl show``. Reads every drop-in root in
    ``SYSTEMD_DROPIN_ROOTS`` and every drop-in dir name that applies, keyed by
    file name so a higher root masks a lower one, then applied in lexical
    order. An empty ``Exec*=`` clears the list; ``User=`` is last-wins, and an
    empty one means root.
    """
    dropins: dict[str, Path] = {}
    for root in SYSTEMD_DROPIN_ROOTS:
        for dirname in _dropin_dir_names(unit.name):
            for conf in sorted((root / dirname).glob("*.conf")):
                dropins[conf.name] = conf
    user = ""
    dynamic_user = start_only = False
    values: dict[str, list[str]] = {key: [] for key in _EXEC_KEYS}
    for path in [unit, *(dropins[n] for n in sorted(dropins))]:
        for key, value in _service_directives(path):
            if key == "User":
                user = value
            elif key == "DynamicUser":
                dynamic_user = _systemd_bool(value)
            elif key == "PermissionsStartOnly":
                start_only = _systemd_bool(value)
            elif key in values:
                values[key] = [*values[key], value] if value else []
    commands = [
        _parse_exec_value(key, value, start_only and key != "ExecStart")
        for key in _EXEC_KEYS
        for value in values[key]
    ]
    return _EffectiveService(
        user=user,
        dynamic_user=dynamic_user,
        commands=tuple(c for c in commands if c is not None),
    )


def _path_stat(path: str) -> tuple[int, int, int]:
    """``(uid, gid, mode)`` of *path*, not following a final symlink.

    The seam the tests fake: the suite does not run as root.
    """
    st = os.lstat(path)
    return st.st_uid, st.st_gid, st.st_mode


def _account_name(uid: int) -> str:
    import pwd

    try:
        return f"{pwd.getpwuid(uid).pw_name}, uid {uid}"
    except KeyError:
        return f"uid {uid}"


def _group_name(gid: int) -> str:
    import grp

    try:
        return f"{grp.getgrgid(gid).gr_name}, gid {gid}"
    except KeyError:
        return f"gid {gid}"


def _entry_flaw(path: str) -> str | None:
    """Why someone other than root can replace or change *path*, or None.

    A symlink's own owner and mode do not matter: only whoever can write the
    directory holding it can re-point it. A sticky root-owned directory (like
    ``/tmp``) does not let anyone replace an entry they do not own, and a
    directory group-writable by group 0 is writable only by root's group.
    """
    uid, gid, mode = _path_stat(path)
    if stat.S_ISLNK(mode):
        return None
    if uid != 0:
        return f"{path} (owned by {_account_name(uid)})"
    if stat.S_ISDIR(mode) and mode & stat.S_ISVTX:
        return None
    if mode & stat.S_IWOTH:
        return f"{path} (world-writable)"
    if mode & stat.S_IWGRP and gid != 0:
        return f"{path} (group-writable, group {_group_name(gid)})"
    return None


def _chain_flaw(path: str, depth: int = 0) -> str | None:
    """The first flaw on *path* or any directory above it, following symlinks.

    A symlink is judged where it sits (its directory decides who can re-point
    it) and then through its target. A component that does not exist ends the
    walk: everything above it was judged, and that is who could create it.
    """
    if depth > _SYMLINK_DEPTH:
        return f"{path} (symlink loop)"
    target = Path(path)
    if not target.is_absolute():
        return None
    current = Path("/")
    components = target.parts[1:]
    for index in range(len(components) + 1):
        if index:
            current = current / components[index - 1]
        try:
            flaw = _entry_flaw(str(current))
            is_link = current.is_symlink()
        except OSError:
            return None
        if flaw is not None:
            return flaw
        if is_link:
            link = current.readlink()
            resolved = link if link.is_absolute() else current.parent / link
            rest = components[index:]
            return _chain_flaw(os.path.normpath(resolved.joinpath(*rest)), depth + 1)
    return None


def _tree_flaw(root: Path) -> str | None:
    """The first entry under *root* someone other than root can change."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for entry in [*dirnames, *sorted(filenames)]:
            try:
                flaw = _entry_flaw(str(Path(dirpath) / entry))
            except OSError:
                continue
            if flaw is not None:
                return flaw
    return None


def _shebang_interpreter(path: Path) -> str | None:
    """The interpreter a ``#!`` script names, through ``env`` if it uses it."""
    try:
        with path.open("rb") as fh:
            head = fh.read(256)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    tokens = head[2:].split(b"\n", 1)[0].decode(errors="replace").split()
    if not tokens:
        return None
    if Path(tokens[0]).name == "env":
        named = [t for t in tokens[1:] if not t.startswith("-")]
        return shutil.which(named[0]) if named else tokens[0]
    return tokens[0]


def _venv_root(binary: Path) -> Path | None:
    venv = binary.parent.parent
    if binary.parent.name == "bin" and (venv / "pyvenv.cfg").is_file():
        return venv
    return None


def _pyvenv_home(venv: Path) -> str | None:
    try:
        text = (venv / "pyvenv.cfg").read_text()
    except OSError, UnicodeDecodeError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "home":
            return value.strip()
    return None


def _command_flaw(command: _ExecCommand) -> str | None:
    """The first path root would execute or import that someone else can change.

    Judged, in order: the executable (through its links), its ``#!``
    interpreter, the scaffold-install-helper's script, then every file of the
    venv either belongs to, and the base Python its ``pyvenv.cfg`` names.
    """
    executable = command.argv[0]
    if not executable.startswith("/"):
        found = shutil.which(executable, path=_SYSTEMD_EXEC_PATH)
        if found is None:
            return None
        executable = found
    resolved = Path(executable).resolve()
    interpreter = _shebang_interpreter(resolved)
    chains = [executable]
    if interpreter is not None:
        chains.append(interpreter)
    if Path(executable).name == _SCRIPT_RUNNING_HELPER and len(command.argv) > 1:
        chains.append(command.argv[-1])
    for path in chains:
        flaw = _chain_flaw(path)
        if flaw is not None:
            return flaw
    candidates = [resolved, *([Path(interpreter)] if interpreter else [])]
    for venv in dict.fromkeys(v for v in map(_venv_root, candidates) if v):
        flaw = _tree_flaw(venv)
        home = _pyvenv_home(venv)
        if flaw is None and home is not None:
            flaw = _chain_flaw(home)
        if flaw is not None:
            return flaw
    return None


@register_check("root_unit_exec_trust")
def _check_root_unit_exec_trust(_config: FraisierConfig | None) -> CheckResult:
    """No command runs as root from code someone else can change (#433).

    A unit with no ``User=``, ``User=root`` or ``User=0`` runs every command as
    root, and a ``+`` or ``!`` command runs as root whatever ``User=`` says.
    For each, the executable, its ``#!`` interpreter, the venv either lives in
    (every file), the base Python that venv's ``pyvenv.cfg`` names, and every
    directory above them must be root-owned and writable only by root. The
    scaffold-install-helper's script argument is judged the same way, because
    the helper runs it.

    It reads the **effective** unit, from ``systemctl show``, which has merged
    every drop-in (one can clear ``User=`` or add a ``+`` line). Where systemd
    cannot answer, it reads the files: the unit and its drop-ins under
    ``SYSTEMD_DROPIN_ROOTS``.

    Every host running fraisier's root helpers today fails it, because they
    run from the deploy user's uv tool dir. It reports
    ``ROOT_EXEC_TRUST_STATUS``: ``warn`` until #433's fix ships.
    """
    name = "root_unit_exec_trust"
    if not SYSTEMD_UNIT_DIR.is_dir():
        return CheckResult(name, "skip", f"{SYSTEMD_UNIT_DIR} is not a directory")
    try:
        unit_files = sorted(SYSTEMD_UNIT_DIR.glob("*.service"))
    except OSError as exc:
        return CheckResult(name, "skip", f"could not read {SYSTEMD_UNIT_DIR}: {exc}")

    # A template (``a@.service``) is not a name systemctl show accepts.
    live = _systemctl_show([u.name for u in unit_files if "@." not in u.name])
    judged = 0
    flawed: list[str] = []
    for unit in unit_files:
        service = _parse_systemctl_show(live[unit.name]) if unit.name in live else None
        if service is None:
            try:
                service = _unit_file_service(unit)
            except OSError, UnicodeDecodeError, ValueError:
                continue
        for command in service.root_commands():
            judged += 1
            flaw = _command_flaw(command)
            if flaw is not None:
                flawed.append(f"{unit.name} {command.key}={command.argv[0]}: {flaw}")
                break

    if not judged:
        return CheckResult(name, "skip", "no installed unit runs a command as root")
    if not flawed:
        return CheckResult(
            name,
            "pass",
            f"{judged} command(s) run as root, all from root-owned paths",
        )
    return CheckResult(
        name,
        ROOT_EXEC_TRUST_STATUS,
        f"{len(flawed)} unit(s) run code as root that another user can change: "
        + "; ".join(flawed),
        fix_hint=(
            "fraisier's root helpers run from the deploy user's uv tool dir, so "
            "the deploy user can become root; the fix is tracked in #433, and "
            "until it ships treat the deploy user as root-equivalent. For any "
            "other unit listed: make the path and every directory above it "
            "root-owned and not group- or world-writable, or give the unit a "
            "non-root `User=`"
        ),
    )


@register_check("self_upgrade_failure")
def _check_self_upgrade_failure(config: FraisierConfig | None) -> CheckResult:
    """A self-upgrade that ran and did not land (#351).

    ``uv tool install --force`` removes before it verifies, so a failed upgrade
    can leave the tool venv half-removed — ``bin/`` gone, ``lib/`` intact, every
    ``~/.local/bin/fraisier*`` symlink dangling. The running webhook survives it
    (a live process outlives its deleted binary), which is precisely why this
    needs reporting: nothing looks wrong until the next restart fails 203/EXEC,
    and on a deploy host that restart is often what you are relying on to fix
    something else.

    ``warn``, not ``fail``: the host is up and serving. What it has lost is the
    ability to come back if it stops.
    """
    name = "self_upgrade_failure"
    if config is None:
        return CheckResult(name, "skip", "no config loaded")

    from fraisier.self_upgrade_record import read_self_upgrade_failure

    lock_dir = getattr(getattr(config, "deployment", None), "lock_dir", None)
    if not lock_dir:
        return CheckResult(name, "skip", "no deployment.lock_dir configured")

    failure = read_self_upgrade_failure(Path(lock_dir))
    if failure is None:
        return CheckResult(name, "pass", "the last self-upgrade landed")

    when = f" at {failure.recorded_at}" if failure.recorded_at else ""
    return CheckResult(
        name,
        "warn",
        (
            f"upgrade to {failure.required} from {failure.installed} failed"
            f" (rc={failure.rc}){when}"
        ),
        fix_hint=(
            "check the entrypoints are still executable — a failed "
            "`uv tool install --force` can leave the tool venv half-removed:\n"
            "  ls -l ~/.local/bin/fraisier*\n"
            "If they dangle, clear any foreign-owned bytecode blocking the "
            "removal and reinstall:\n"
            "  sudo find ~/.local/share/uv/tools -name __pycache__ "
            "! -user $(id -un) -type d -exec rm -rf {} +\n"
            f"  uv tool install --force fraisier=={failure.required}\n"
            f"Recorded cause: {failure.detail[:300]}"
        ),
    )


@register_check("refused_dispatch")
def _check_refused_dispatch(config: FraisierConfig | None) -> CheckResult:
    """A deploy that was requested and never ran (#365).

    While a self-upgrade drains, the webhook answers new dispatches with 503.
    The back-pressure is right; the request being *gone* afterwards is not.
    Nothing in ``health``, nothing in ``deployment-status``, no row — the
    branch just stayed undeployed and looked like one nobody had pushed.

    ``warn``, not ``fail``, matching ``self_upgrade_failure``: the host is up
    and serving. What it has lost is a request.
    """
    name = "refused_dispatch"
    if config is None:
        return CheckResult(name, "skip", "no config loaded")

    from fraisier.locking import draining_flag_age_s
    from fraisier.refused_dispatch_record import read_refused_dispatches

    lock_dir = getattr(getattr(config, "deployment", None), "lock_dir", None)
    if not lock_dir:
        return CheckResult(name, "skip", "no deployment.lock_dir configured")

    entries = read_refused_dispatches(Path(lock_dir))
    if not entries:
        return CheckResult(name, "pass", "no deploy was dropped by a self-upgrade")

    detail = "; ".join(
        f"{e.fraise}/{e.environment} at {e.branch}"
        f"{'@' + e.commit_sha[:7] if e.commit_sha else ''}"
        f"{' on ' + e.refused_at if e.refused_at else ''}"
        for e in entries
    )

    # Tells "still draining, wait" from "long over, re-fire now". Re-firing
    # by hand is what the reporter did, and it worked.
    age = draining_flag_age_s(Path(lock_dir))
    flag_note = (
        ""
        if age is None
        else (
            f"\nThe .draining flag is still up ({age:.0f}s). If that is a live "
            "upgrade, wait for it; if it is a corpse, the deploys below are "
            "what it swallowed.\n"
        )
    )
    # shlex.quote, because every field here came off a webhook payload and this
    # hint is written to be copy-pasted into a shell. Git permits `;`, `&` and
    # `$` in a ref name, so an unquoted branch is a second command waiting for
    # an operator to paste it.
    commands = "\n".join(
        "  fraisier trigger-deploy "
        + " ".join(
            shlex.quote(part)
            for part in (e.fraise, e.environment, "--branch", e.branch)
        )
        for e in entries
    )
    return CheckResult(
        name,
        "warn",
        f"a deploy was requested and never ran: {detail}",
        fix_hint=(
            f"{flag_note}re-fire the dropped deploy(s):\n{commands}\n"
            "Each entry clears itself when a deploy for that target succeeds."
        ),
    )


#: The release in which ``deploy_daemon`` began writing its result to the
#: accepted connection (#356). Before it, the result went to the journal via
#: ``print()`` and no ``--wait`` client could ever receive one.
RESULT_CHANNEL_SINCE = (0, 64, 0)

_VERSION_IN_OUTPUT = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _binary_semver(binary: str) -> tuple[int, int, int] | None:
    """Ask a fraisier binary its version. None when it will not say.

    None is *unverifiable*, not old: the deploy user's binary is frequently
    unreadable by whoever runs ``doctor``, and "I could not ask" must not read
    as "this host is broken".
    """
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired, OSError:
        return None
    if proc.returncode != 0:
        return None
    match = _VERSION_IN_OUTPUT.search(proc.stdout or "")
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _installed_deploy_entrypoints() -> dict[str, str] | None:
    """Map each installed deploy service to the fraisier binary it runs.

    Deploy services are identified by what they execute — ``deploy-daemon`` —
    rather than by unit name, so a hand-written or renamed unit is found too.
    None when the unit directory cannot be read at all.
    """
    try:
        unit_files = sorted(SYSTEMD_UNIT_DIR.glob("*.service"))
    except OSError:
        return None

    entrypoints: dict[str, str] = {}
    for unit in unit_files:
        try:
            text = unit.read_text()
        except OSError, UnicodeDecodeError:
            continue
        for line in text.splitlines():
            binary = _exec_start_binary(line)
            if binary is None or "deploy-daemon" not in line:
                continue
            entrypoints[unit.name] = binary
    return entrypoints


@register_check("deploy_result_channel")
def _check_deploy_result_channel(_config: FraisierConfig | None) -> CheckResult:
    """Every installed deploy service can return a result to ``--wait`` (#356).

    ``trigger-deploy --wait`` now exits 1 when no result arrives, because
    reporting an outcome that never came is how a nightly staging restore
    skipped a full day while systemd recorded a clean exit. The cost is that a
    host whose deploy unit still runs a pre-v0.64.0 fraisier fails every
    ``--wait`` deploy until it is reinstalled — and that skew is the *normal*
    upgrade order: the CLI replaces itself via self-upgrade, while the deploy
    unit's binary changes only when someone re-runs a scaffold install.

    This is what finds those hosts before their next nightly does. The unit
    file is unchanged by the fix — the result goes to fd 0, which
    ``StandardInput=socket`` already provided — so the discriminator is the
    version of the binary the *installed* unit names in ``ExecStart=``.

    Takes no config: the units on disk are the input, so it still answers on a
    host whose ``fraises.yaml`` will not load.
    """
    name = "deploy_result_channel"

    entrypoints = _installed_deploy_entrypoints()
    if entrypoints is None:
        return CheckResult(name, "skip", f"could not read {SYSTEMD_UNIT_DIR}")
    if not entrypoints:
        # Distinct from "pass" on purpose: a scan that matched nothing must not
        # read as a clean bill of health.
        return CheckResult(
            name, "skip", f"no deploy services installed under {SYSTEMD_UNIT_DIR}"
        )

    # Deploy units on a host almost always share one binary; ask each once.
    versions = {binary: _binary_semver(binary) for binary in set(entrypoints.values())}

    stale: list[str] = []
    unknown: list[str] = []
    for unit_name, binary in sorted(entrypoints.items()):
        semver = versions[binary]
        if semver is None:
            unknown.append(f"{unit_name} -> {binary}")
        elif semver < RESULT_CHANNEL_SINCE:
            stale.append(f"{unit_name} -> {'.'.join(str(p) for p in semver)}")

    since = ".".join(str(p) for p in RESULT_CHANNEL_SINCE)
    if stale:
        return CheckResult(
            name,
            "fail",
            f"{len(stale)} of {len(entrypoints)} deploy service(s) run a fraisier "
            f"older than {since} and cannot return a result to --wait: "
            f"{'; '.join(stale)}",
            fix_hint=(
                "every `trigger-deploy --wait` against these exits 1 until the "
                "unit runs a newer fraisier — retrying does not help. Re-render "
                "and reinstall:\n"
                "  fraisier scaffold && fraisier scaffold-install\n"
                "then confirm the deploy user's binary moved: "
                "`ls -l ~/.local/bin/fraisier`"
            ),
        )
    if unknown:
        return CheckResult(
            name,
            "warn",
            f"could not determine the fraisier version behind "
            f"{len(unknown)} of {len(entrypoints)} deploy service(s): "
            f"{'; '.join(unknown)}",
            fix_hint=(
                "this is unverifiable, not broken — the deploy user's binary is "
                "usually not readable by whoever runs doctor. Ask it directly: "
                "`sudo -u <deploy_user> ~<deploy_user>/.local/bin/fraisier "
                "--version`"
            ),
        )
    return CheckResult(
        name,
        "pass",
        f"{len(entrypoints)} deploy service(s) run fraisier {since} or newer",
    )
