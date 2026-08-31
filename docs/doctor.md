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
| `backup_retention` | every declared `retain` corpus has both halves of its prune unit on disk | no | `sudo fraisier scaffold-install` — until then nothing prunes that corpus |
| `self_upgrade_failure` | the last webhook self-upgrade landed, rather than leaving the tool venv half-removed | no | clear foreign-owned `__pycache__`, then `uv tool install --force fraisier==<version>` — see [a self-upgrade that did not land](#a-self-upgrade-that-did-not-land) |
| `unit_entrypoints` | every installed unit's `ExecStart=` fraisier binary exists and is executable | no | reinstall the tool venv — a unit whose entrypoint dangles fails 203/EXEC at its next restart |
| `backup_corpus_free_space` | each `retain[].dir` exists and its volume meets the entry's `min_free_gb` | no | free space on the volume, or declare a threshold — see [`min_free_gb`](deployment-guide.md#min_free_gb--a-policy-is-not-a-disk-alarm) |
| `scaffold_artifact_coverage` | every artifact the manifest declares for this host is installed | no | `fraisier scaffold && sudo fraisier scaffold-install --yes` |
| `foreign_units` | no `fraisier` unit on this host belongs to a fraise or environment this host does not own | no | `sudo fraisier scaffold-install --prune` — see [one host authority](deployment-guide.md#scheduled-timers-you-switch-on) |
| `webhook_hosted_trees_writable` | the installed webhook unit's `ReadWritePaths=` covers every tree it deploys | no | `fraisier scaffold && sudo fraisier scaffold-install --yes` |
| `deferred_restarts` | no unit is installed-and-daemon-reloaded while still running its previous version | no | `sudo systemctl restart <unit>` once no deploy is in flight — see [restarts a deploy defers](#restarts-a-deploy-defers) |
| `sandbox_write_probe` | actually writes into the rendered unit's sandbox under `ProtectSystem=strict` | no | opt-in via `--probe-sandbox`; needs root, else `skip` |
| `deploy_result_channel` | every installed deploy service runs a fraisier new enough to return a result to `--wait` | no | `fraisier scaffold && sudo fraisier scaffold-install --yes` — see [a deploy socket that cannot answer](#a-deploy-socket-that-cannot-answer) |
| `refused_dispatch` | no deploy was requested and then dropped by a self-upgrade's 503 | no | `fraisier trigger-deploy <fraise> <env> --branch <branch>` — see [a deploy the self-upgrade swallowed](#a-deploy-the-self-upgrade-swallowed) |

The catalog above is **complete**, and a test asserts it: every name in
`DOCTOR_CHECKS` appears here and vice versa. It had silently fallen six checks
behind before that test existed, which is the same class of defect as a unit
nothing enables — documentation that describes a subset reads as describing the
whole.

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

## Restarts a deploy defers

`install.sh` will not restart a unit that hosts a deploy while a deploy is in
flight. The webhook runs deploys as in-process background tasks and a deploy
socket propagates a restart to the `deploy-daemon` instance running one, so
restarting either would SIGKILL the deploy that asked for the install — which is
how a `fraises.yaml` change used to fail its own deploy, deterministically.

It records what it skipped in `<deployment.lock_dir>/.deferred-restarts` and
prints it. The deploy pays that back when it finishes: a detached worker raises
the `.draining` flag, waits for the deployment locks to release, and sends the
restart over the systemctl-helper socket. **A ledger entry is cleared only when
its restart succeeded**, so `deferred_restarts` warns about anything left:

- the drain timed out and the restart was not attempted;
- the systemctl helper refused the unit — it allowlists services, not sockets;
- the deploy ran under the socket-activated `deploy-daemon`, whose per-connection
  service instance can take the detached worker down with its cgroup.

A unit in that state works, on its previous configuration. What it costs is a
stale `ReadWritePaths=` or `Environment=`, which surfaces later as a deploy
failing on a read-only filesystem rather than as anything pointing back here.
Restart the named units once no deploy is running.

## A self-upgrade that did not land

When a deployed `pyproject.toml` pins a newer fraisier, the webhook detaches a
worker that runs `uv tool install --force`. That command **removes before it
verifies**, so a failure partway through can leave the tool venv half-removed —
`bin/` gone, `lib/` intact — and every `~/.local/bin/fraisier*` symlink
dangling, including the one the webhook unit names in `ExecStart=`.

The running webhook survives this, because a live process outlives its deleted
binary. That is exactly what makes it dangerous: `systemctl is-active`, the
health check and the version endpoint all look normal, and nothing reveals the
damage until the next restart fails with status 203/EXEC — which, on a deploy
host, is often the restart you are relying on to fix something else.

`self_upgrade_failure` reads
`<deployment.lock_dir>/.self-upgrade-failure`, written by the worker when the
install returns non-zero and **cleared only when a later upgrade lands**. It
reports the version pair, the exit code and the recorded cause.

The most common cause is foreign-owned bytecode the deploy user cannot delete:
the helper daemons run as root out of the same venv, and while every unit sets
`PYTHONDONTWRITEBYTECODE=1`, an interpreter started as root *outside* a unit
still writes `fraisier/__pycache__/__init__.*.pyc` before fraisier's own
in-process guard can take effect. To recover:

```bash
ls -l ~/.local/bin/fraisier*                     # dangling?
sudo find ~/.local/share/uv/tools -name __pycache__ \
     ! -user "$(id -un)" -type d -exec rm -rf {} +
uv tool install --force fraisier==<version>
sudo systemctl restart fraisier-<project>-webhook.service
```

The worker's own log, which records the command it ran and the full stderr, is
under `/var/lib/fraisier/self-upgrade/`.

`unit_entrypoints` asks the same question from the other end, and does not care
how the host got there: it reads every `*.service` under `/etc/systemd/system`,
takes the binary from each `ExecStart=`, keeps the ones named `fraisier*`, and
checks that each still exists and is executable. A failed self-upgrade, a
half-finished manual install and a pruned venv all read the same to it. It takes
no configuration — the units on disk are the input — so it still answers on a
host whose `fraises.yaml` will not load.

It reports `fail` rather than `warn`, because unlike a deferred restart the unit
cannot start at all: the service running now is the last one that ever will,
until it is fixed. Existence is checked before `os.access`, since `os.access`
with `X_OK` is permissive for root and a root-run `doctor` would otherwise call
a missing file executable.

If no unit names a fraisier binary, the check reports `skip` and says so, rather
than `pass`. A scan that matched nothing must not read as a clean bill of
health.

## A deploy the self-upgrade swallowed

While that worker installs and drains, the webhook raises a `.draining` flag in
`deployment.lock_dir` and answers new dispatches with HTTP 503 plus a
`Retry-After`. The back-pressure is correct — a deploy must not start while the
binary that would run it is being replaced.

What was not correct is that the request then had nowhere to go. No file, no
row, nothing in `fraisier health` or `deployment-status`; the branch simply
stayed undeployed and looked like one nobody had pushed. A caller that does not
special-case 503 records a generic failure, indistinguishable from a deploy that
started and failed.

`refused_dispatch` reads `<deployment.lock_dir>/.refused-dispatches`, written at
the moment of the refusal with one entry per `(fraise, environment)` the push
would have deployed. It is **cleared only when a later deploy for that target
succeeds** — never on read, never at startup, never by the `Retry-After`
expiring. Clearing on a restart would mean the upgrade's own restart erased the
record of what it displaced.

The fix hint is the command that re-fires it, and it says whether the
`.draining` flag is still up, so you can tell "wait, that is a live upgrade"
from "that flag is a corpse and these are what it swallowed":

```bash
fraisier trigger-deploy my_api staging --branch main
```

A flag left by a killed worker no longer refuses forever. Past
`webhook.self_upgrade_flag_max_age_s` (default 3600, six times the drain
timeout) it is **ignored** and deploys resume — the flag file itself is left in
place as evidence. A wrongly permitted deploy is loud and recoverable; a
wrongly refused one is neither.

Replaying a dropped dispatch automatically is deliberately not done: it needs
dedup by ref, a staleness rule, and ordering across environments. This check
makes the loss visible; re-firing stays a decision.

## A deploy socket that cannot answer

`trigger-deploy --wait` exists so a caller can tell "done" from "started". Until
v0.64.0 it could not: `deploy-service` sets `StandardInput=socket` under an
`Accept=yes` socket, so fd 0 *is* the accepted client connection, but the
daemon wrote its machine-readable result with `print()` — to fd 1, which
`StandardOutput=journal` sends to the journal. The result never reached the
client. Every `--wait` run read an empty response and reported
`Deployment triggered successfully`, exit 0, whatever had actually happened.

v0.64.0 fixes both halves: the daemon writes the result to the connection, and
`--wait` treats a missing or unparseable result as a failure rather than
inventing a success.

That second half has a cost, and this check is how you pay it deliberately. A
host whose deploy unit still runs a **pre-v0.64.0** fraisier cannot return a
result at all, so every `--wait` deploy against it now exits 1. And that skew is
the normal upgrade order rather than an edge case: the CLI replaces itself via
self-upgrade, while the deploy unit's binary changes only when someone re-runs a
scaffold install. A host can sit new-client/old-unit for weeks.

`deploy_result_channel` reads the **installed** units — not the rendered ones —
finds every service whose `ExecStart=` runs `deploy-daemon`, and asks that
binary its version. The unit file itself is unchanged by the fix (the result
goes to fd 0, which `StandardInput=socket` already provided), so the binary is
the only thing that can answer the question.

It takes no config: the units on disk are the input, so it still reports on a
host whose `fraises.yaml` will not load. Deploy units on a host almost always
share one binary, so it is asked once and the answer reused.

Three verdicts, and the third is the point:

- `fail` — a unit runs a fraisier older than 0.64.0. Named per unit, and the
  unit name is the `(fraise, environment)` identity. Retrying will not help;
  re-render and reinstall:

  ```bash
  fraisier scaffold && sudo fraisier scaffold-install --yes
  ls -l ~/.local/bin/fraisier          # confirm the binary moved
  ```

- `warn` — the binary would not say what it is. Usually the deploy user's
  `~/.local/bin/fraisier` is simply not readable by whoever ran `doctor`. This
  is *unverifiable*, not broken, and follows `ArchiveCheck`'s three-valued
  precedent: "I could not ask" is not "this host is broken". Ask it directly
  with `sudo -u <deploy_user>`.

- `skip` — no deploy services are installed here, or the unit directory could
  not be read. As with `unit_entrypoints`, a scan that matched nothing reports
  `skip` rather than `pass`.

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
