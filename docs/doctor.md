# `fraisier doctor`

Host-wide self-diagnosis. Answers *"is this fraisier install OK to use
at all?"* rather than the per-environment question ``fraisier diagnose
<fraise> <env>`` answers.

```bash
fraisier doctor
fraisier doctor --format json | jq '.summary'
fraisier doctor --check python_version --check fraisier_version
fraisier doctor --skip-network
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All checks pass (skip counts as pass) |
| 1 | Any check returned `fail` |
| 2 | Any check returned `warn` but no `fail` |

## Check catalog

Each check is independent — one failing check never aborts the others.
Checks marked `network: yes` are skipped when ``--skip-network`` is
passed.

| Check | What it verifies | Network? | Canonical fix |
|---|---|---|---|
| `python_version` | Python >= 3.11 | no | upgrade Python |
| `fraisier_version` | `importlib.metadata.version("fraisier")` resolves | no | `pip install --force-reinstall fraisier` |
| `confiture_version` | `confiture --version` resolvable | no | `pip install confiture` |
| `fraises_yaml_loadable` | `fraises.yaml` parses without error | no | `fraisier validate` for details; `fraisier init` for fresh setup |
| `fraises_yaml_resolves` | every reachable `!envvar` resolves to a set var | no | export the missing variables or move them to `~/.config/fraisier/secrets.env` |
| `secrets_env_readable` | `~/.config/fraisier/secrets.env` exists and is mode 0600 | no | `chmod 600 ~/.config/fraisier/secrets.env` |
| `helper_sudoers` | `/etc/sudoers.d/<project>` exists and is mode 0440 | no | `fraisier scaffold-install` |
| `pre_migrate_dump_writable` | the installed webhook unit allows writes to `pre_migrate_dump.output_dir` | no | `fraisier scaffold && sudo fraisier scaffold-install --yes` |
| `install_compile_bytecode` | a `uv sync` `install.command` passes `--compile-bytecode` | no | add `--compile-bytecode` to `install.command` — see [bytecode and startup time](deployment-guide.md#bytecode-and-startup-time) |
| `inert_timers` | which `scaffold.systemd.timers` families this host runs, and which it only carries | no | none needed — see [scheduled timers you switch on](deployment-guide.md#scheduled-timers-you-switch-on) |

`inert_timers` is **always `pass`**. Every family off is a configured state,
not a fault, and a check that warns about a deliberate choice on every run
becomes wallpaper. What it adds is that the state is legible: `backup.timer`
and `deploy-checker.timer` were copied to every host and started on none, and
nothing distinguished "copied and running" from "copied and never started" —
which is why three of those units stayed broken for years.

`install_compile_bytecode` is **advisory** — it returns `warn`, never `fail`,
because the cost is startup latency rather than correctness. It resolves
`install:` the way the scaffold renderer does (per-environment overrides the
per-fraise default), and returns `skip` when no `uv sync` command is configured
at all — a project installing with `npm ci` or `poetry install` has nothing for
this check to say.

## Safety notes

- No check has side effects: no `systemctl start`, no DB writes, no
  env-var mutation. Reads stat info and shell command output only.
- The `helper_sudoers` check **never invokes `visudo`** and **never
  parses sudoers syntax** (sudoers parser bugs have been CVE-class).
  When the file is readable, fraisier byte-diffs against the expected
  rendered fragment via `fraisier.scaffold.sudoers_diff.diff_sudoers`.
- No check prints secret values. `fraises_yaml_resolves` lists env-var
  *names* that are unset, never values.

## JSON output

```json
{
  "fraisier_version": "0.25.1",
  "checks": [
    {"name": "python_version", "status": "pass", "detail": "3.13.11", "fix_hint": null},
    {"name": "fraisier_version", "status": "pass", "detail": "0.25.1", "fix_hint": null},
    {"name": "fraises_yaml_loadable", "status": "pass", "detail": "loaded /path/to/fraises.yaml", "fix_hint": null}
  ],
  "summary": {"pass": 3, "warn": 0, "fail": 0, "skip": 0}
}
```

## When to use

- **CI gate before deploy**: `fraisier doctor --skip-network --format json | jq -e '.summary.fail == 0'`.
- **New-host bootstrap verification**: after `fraisier bootstrap` returns, `fraisier doctor` confirms the install is operable.
- **On-call triage**: when a fraise won't deploy, run `fraisier doctor` first to rule out install-level issues before reaching for `fraisier diagnose`.

## See also

- [`fraisier env-check`](cli-reference.md#env-check) — per-subcommand envvar preflight
- [`fraisier diagnose`](cli-reference.md#diagnose) — per-environment deployment health
