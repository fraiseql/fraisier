# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.65.0] - 2026-08-10

**Counting harder cannot see a restore that never ran.**

[#358](https://github.com/fraiseql/fraisier/issues/358) was filed against
v0.64.0's schema floor with a correct observation — a dump truncated inside its
data section restores a complete, empty schema, so no table-count floor of any
derivation can see it — and reached for row counts as the answer. Before
building that, we measured. The measurement changed the work.

The issue conflates two failures:

- **Partial data.** The schema arrived, the rows did not.
- **Non-actuation.** The pipeline never ran; staging keeps yesterday's database.
  Counts, floors and `pg_restore`'s exit are all *correct*, because nothing
  about the content is wrong. It is only old.

**Partial data turned out to already be caught, on both restore paths.** Against
a real PostgreSQL, a `-Fc` dump cut at 50%, 90% and 98% fails the restore every
time: `pg_restore` cannot read past the cut, writes a `pg_restore: error:` line
and exits non-zero, and confiture fails the section. That holds for `jobs > 1`
too, where `exit_on_error` is off — tolerating a non-zero *exit* is not the same
as tolerating an *error line*. The premise still holds exactly as #358 states it
(the table of contents parses, `verify_archive` returns VALID, the derived floor
is satisfied by an empty schema); it is the restore, not the checks around it,
that refuses. That is now pinned by a test rather than believed.

So the row-count manifest #358 asks for was **not built**. It exists only to
close the half that is already closed, it is the most expensive option on the
table — a producer, a consumer, snapshot skew against `pg_dump`'s own snapshot,
a tolerance band, and a three-valued case for every dump predating it — and it
would not have caught the failure that was actually reported.

**Non-actuation is the residual, and it is the #343 signature** ("success in 21s,
staging a day stale"). It is unreachable by counting: the cure is evidence the
heap was written *this run*.

### Added

- **A restore leaves a receipt proving it ran** (#358). The pipeline mints a
  token per run and writes it, with the backup's path and size, into a
  `fraisier.restore_receipt` table in the database it just restored — then reads
  it straight back and compares. A round trip through the database, not a
  variable the pipeline set and then trusted.

  The token is the whole mechanism. A restore that never ran leaves the
  *previous* run's receipt in place, so "a receipt exists" matches every time
  and proves nothing; `verify_actuation` therefore refuses to answer without a
  criterion — a run id, or a freshness window.

  Four-valued, following `ArchiveCheck`: `ACTUATED` is the only proof, `STALE`
  the only conviction, and `MISSING` (restored by hand, or by an older fraisier)
  and `UNVERIFIABLE` (no `psql`, no connection) say nothing in either direction.
  A receipt that could not be written **does not fail the restore** — it is
  bookkeeping written after every real check has already passed, and it is
  reported as *not verified*, never as verified.

  The table lives in a dedicated `fraisier` schema, not `public`: both floors
  guarding a restore count `relkind='r'` in one schema, so a bookkeeping table
  in `public` would quietly raise them, and a schema comparison would read it as
  drift. It needs no migration and no cleanup — the next restore's DROP+CREATE
  takes it with everything else.

- **`fraisier db receipt <fraise> <env>`** asks a database when a restore last
  rewrote it. This is the half that actually catches the reported failure: the
  pipeline's own read-back runs inside the process that did the writing, which
  proves the write landed but cannot prove a run happened — when the run does
  not happen, that code does not execute either. The durable row is what an
  independent caller interrogates the next morning.

  Three answers, three exit codes, so a monitoring timer can tell them apart:
  `0` rewritten inside the window, `1` stale, and **`3` not checked**. Not `0`,
  because a host that cannot check must not report what a host that checked and
  passed reports — that is `min_tables=0`'s silent hole moved into monitoring.
  Not `1`, because a missing `psql` should not page the way a stale staging
  database does.

- **`--check-heap`** reports relation-file mtimes, the check #358 names. Opt-in
  and advisory: it needs superuser or `pg_read_server_files`, which managed
  PostgreSQL commonly refuses, and autovacuum moves mtimes after a no-op — a
  false *pass*, never a false fail. It corroborates the receipt and never moves
  the exit code. When the two disagree both are printed; "the receipt says today
  and the heap says last week" is something an operator wants told, not
  something the tool should settle silently.

### Fixed

- `_pg_cmd` accepts stdin, so `psql`'s `:'var'` binding is usable at all. `-c`
  hands its string to the server unread, so the variables were never
  substituted; only input psql lexes itself gets them. Found by a test that put
  a quote in a backup path.

### Notes

- **The docstrings that were wrong are corrected.** `dbops/restore.py` and
  v0.64.0's CHANGELOG both said a truncated dump was an open hole. The floor
  cannot see one — that part was right — but the restore fails on it anyway.
- **The rollback template carries no receipt.** It is taken before `migrate up`
  and the receipt is written after, so a database rolled back onto it reads
  `MISSING`. That is correct: after a rollback it is not the state any completed
  run produced, and `MISSING` says *not proven* rather than *stale*.
- No confiture change and no version floor: the receipt is entirely fraisier's.

## [0.64.0] - 2026-08-10

**An outcome nobody could receive.**

`trigger-deploy --wait` printed `ok Deployment triggered successfully` and exited
0 in **7 seconds** for a nightly staging restore whose real runtime is ~33
minutes. The wrapping systemd unit recorded a clean exit. Staging silently stayed
a day stale, and it was found only by comparing relation-file mtimes — row counts
on a stale database are correct.

The reported defect was the `--wait` guard, and it is real. But reading the
source turned up something larger and stranger: **the result channel has never
existed.** `deploy-service.j2` sets `StandardInput=socket` under an `Accept=yes`
socket, so fd 0 *is* the accepted client connection — and it also sets
`StandardOutput=journal`, so the `print()` carrying the machine-readable result
went to the journal instead. Those three lines have been unchanged since socket
activation was implemented in April.

So the 7-second incident is not a race that produced an empty response. It is the
*only* response that transport could ever produce, observed on a run short enough
for someone to notice. Every `--wait` deploy in this project's history — success,
failure, crash, all of them — read an empty response and took the
fire-and-forget branch.

This is the sixth instance of v0.61.0–v0.63.0's heading — *work that could not
fail visibly*. The previous five destroyed or omitted the reporter. This one is
worse: the reporting channel was never connected. So the invariant, not the site:

> **A caller that asked for an outcome receives one, or an error — never a report
> of an outcome that could not have arrived.**

[#343](https://github.com/fraiseql/fraisier/issues/343)'s restore half is the
same invariant against a database, narrowed to what a table count can actually
prove:

> **A restore reports success against the schema the archive says it carries, not
> against a floor nobody configured.**

**Order matters here, and the phases exist to enforce it.** Making `--wait` refuse
to invent an outcome is only an improvement once an outcome can reach it. Shipped
alone against today's units, it would have turned *every* `--wait` deploy —
including the successful ones — into an exit 1. Nightly units that currently lie
would have started failing outright.

### Fixed

- **The daemon's result reaches the connection that asked for it**
  ([#356](https://github.com/fraiseql/fraisier/issues/356)). `deploy_daemon`
  writes its JSON result to the accepted socket on fd 0, and keeps writing it to
  stdout as well, so the journal holds the record it has always held and the
  documented `echo '{…}' | fraisier deploy-daemon` pipe form is untouched.
  `StandardOutput=journal` stays — it is deliberate (#72 Bug 2: `systemd` 255
  versus Debian 12's 252) and flipping it would have put Rich-styled prose on the
  wire ahead of the JSON, trading an empty response for an unparseable one.
- **All seven terminating paths report, not two.** Empty stdin, an exception
  while reading stdin, an unparseable request, a project mismatch and an
  exception out of `execute_deployment_request` each printed prose and exited 1
  having written nothing machine-readable at all. #356 attributed the empty
  response to #349 killing the unit; #349 was one trigger out of six, and the
  other five are ordinary control flow. Each now names its own reason.
- **A departed peer cannot change the outcome.** `deploy-checker` fires
  `trigger-deploy` with no `--wait` and closes in milliseconds, so half an hour
  later the daemon writes to a socket whose peer is long gone. Unhandled, that
  `BrokenPipeError` would raise *after* a successful deployment and take the
  unit's exit code with it — inverting the bug rather than fixing it. The write
  is wrapped, logged, and cannot touch the exit code.
- **`--wait` reports the outcome it received.** An empty response is an error
  naming both causes and the remedy for each; an unparseable response is an error
  too, where it used to print a yellow warning and then the success line — the
  same lie in a quieter voice, since a warning is not an exit code. Without
  `--wait`, nothing changes: the caller never asked.
- **`--follow` is bound by the same invariant.** It implies `--wait`, drained the
  result off the socket, and then exec'd into `journalctl` *before* looking at
  it — exiting with journalctl's status, which says nothing about the deploy. It
  now reports the outcome first and follows the logs only for a deployment that
  succeeded. It still exec's `journalctl` only once the recv loop has drained —
  that is, once the deploy is already over — so it follows nothing live; that
  second oddity is untouched here and tracked as
  [#357](https://github.com/fraiseql/fraisier/issues/357).
- **`min_tables_schema` is forwarded to confiture**
  ([#343](https://github.com/fraiseql/fraisier/issues/343)). It was defaulting to
  `public` while the count could describe any schema. The host that reported #356
  keeps its heaps in `tenant`, so this is the half that makes an archive-derived
  floor safe rather than a guaranteed false failure on a perfect restore.

### Added

- **A restore proves its schema arrived** (#343). `verify_archive` already ran
  `pg_restore --list` and then discarded the table of contents; it now counts the
  `TABLE DATA` entries **per schema** from output it already had in memory — no
  second invocation. The count becomes the floor confiture enforces *before*
  migrations, because the TOC describes the archive, so the database that must
  satisfy it is the one before `migrate up`. A shortfall fails the restore.

  This closes a hole `min_tables` only *declared*: it defaults to `0` and the
  strategy's floor is `if cfg.min_tables > 0`, so in the default configuration
  nothing counted anything and a near-empty restore exited 0 with a yellow note.

  **What it does not do, stated plainly because the CHANGELOG must not imply
  otherwise: it does not prove the data arrived.** A dump truncated inside its
  data section restores a complete, empty schema and passes any count —
  `dbops/restore.py` has said so in its own docstring the whole time. Relation
  mtimes, which is how #356 was found, remain the check for stale-but-intact
  data. That hole stays open as
  [#358](https://github.com/fraiseql/fraisier/issues/358).

  *Corrected in v0.65.0: the floor indeed cannot see a truncation, but the
  restore fails on one regardless — `pg_restore` errors and exits non-zero, on
  both the serial and parallel paths. The hole #358 really leaves open is a
  restore that never ran, which v0.65.0 closes with an actuation receipt.*
- **`doctor deploy_result_channel`** finds hosts that cannot answer (#356).
  Making `--wait` honest means every host whose deploy unit still runs a
  pre-v0.64.0 fraisier now fails `--wait` — and that skew is the *normal* upgrade
  order, not an edge case: the CLI replaces itself via self-upgrade while the
  deploy unit's binary changes only when someone re-runs a scaffold install. The
  check reads the **installed** units, finds every service whose `ExecStart=`
  runs `deploy-daemon`, and asks that binary its version. Three-valued, following
  `ArchiveCheck`: `fail` for a stale binary, `warn` for one it could not
  interrogate — the deploy user's binary is routinely unreadable by whoever runs
  `doctor`, and "I could not ask" is not "this host is broken" — and `skip` when
  there was nothing to look at.

### Upgrading

Reinstall the deploy units on every host, or `--wait` will exit 1 there:

```bash
fraisier scaffold && sudo fraisier scaffold-install --yes
fraisier doctor          # deploy_result_channel lists anything still stale
```

Hosts that never pass `--wait` are unaffected.

## [0.63.0] - 2026-08-09

**An install that killed the deploy that asked for it.**

When a deploy synced a changed `fraises.yaml`, it regenerated and installed the
scaffold *in process*, and `install.sh` then ran
`systemctl restart <project>-webhook.service` — SIGTERMing the very process
running the deploy. The webhook does not exit while a deploy is in flight, so
systemd's 90-second stop timeout expired, the process was SIGKILLed, and the
deploy died mid-flight. **Any change to `fraises.yaml` failed its own deploy,
deterministically**, and reported as "timeout waiting for deployment" — which
reads as slow rather than killed.

This is v0.61.0 and v0.62.0's theme once more — *work that could not fail
visibly* — with the reporter itself destroyed. So this release states the
invariant rather than patching the site:

> **Nothing fraisier installs may terminate the work that asked for it, and work
> that starts must end in a record.**

[#351](https://github.com/fraiseql/fraisier/issues/351) turned out to be the same
invariant one level up, so it ships here too: a failed self-upgrade left the tool
venv half-removed and every entrypoint dangling, reported only to a file nothing
reads. Where #349 was an install destroying the deploy that asked for it, #351
was fraisier destroying *itself* and staying quiet about it. The reach extends
accordingly:

> **A tool that replaces itself must leave a working tool, and must say so.**

### Fixed

- **`install.sh` no longer restarts a unit that hosts a deploy while a deploy is
  in flight** ([#349](https://github.com/fraiseql/fraisier/issues/349)). Every
  such restart now goes through one seam, and a test asserts nothing bypasses it
  — with a meta-test proving the guard can fail, because a tree-scanning guard
  that silently matches nothing is indistinguishable from a passing one.
- **The deploy sockets were the same bug, unreported.** There are *two*
  deploy-hosting units, not one: the webhook, and a deploy socket's
  `<stem>@N.service` instance running `fraisier deploy-daemon`. The generated
  deploy service carries `Requires=<its socket>`, and systemd propagates an
  explicit restart of a required unit to its dependents — the identical
  mechanism behind the scaffold-install-helper self-restart race guarded since
  v0.47.0. Routing them through the seam costs nothing: those restarts exist
  because a *first-time* install wipes `/run/fraisier/`, and a first-time
  install is by construction a state with no deploy in flight.
- **The declaration reaches `install.sh` on every path.** The existing
  `FRAISIER_VIA_SCAFFOLD_INSTALL_HELPER` marker only exists on the helper-socket
  path; the subprocess fallback runs `install.sh` as a child of the deploy, where
  the restart kills the whole cgroup just the same. The deploy now declares
  itself in the socket request payload *and* in the subprocess environment, and
  `fraisier scaffold-install` re-states it as `install.sh --deploy-in-flight` —
  `sudo` resets the environment, and that was the one hop that mattered. The
  helper honours only a literal JSON `true`, mapped to a fixed variable name: the
  request reaches a daemon running as root.
- **`install.sh` also probes the deployment locks itself**, so the decision does
  not rest on a claim it cannot check. The probe opens existing lock files
  read-only and never creates one — a root-owned lock file is one the deploy user
  cannot reopen for writing. An unevaluable probe resolves to "no deploy is
  running", loudly and deliberately: the authoritative signal is the caller's
  declaration, which is never unevaluable, and a backstop that cannot run must
  not block the manual re-bake that is the documented remedy for a stale unit.

### Added

- **A deferred restart is a debt, and the deploy pays it.** Skipping the restart
  alone would leave the webhook on its previous unit — old `ReadWritePaths=`, old
  `Environment=` — so a `fraises.yaml` change adding an environment would make
  the *next* deploy fail on a read-only filesystem. That is the "rendered ≠
  installed" class v0.57.0 and v0.58.0 were about. The units are recorded in
  `<deployment.lock_dir>/.deferred-restarts`, and at the end of the deploy a
  detached worker raises the `.draining` flag, waits for the locks to release and
  restarts over the systemctl-helper socket — the same sequence the self-upgrade
  path already used, now shared rather than duplicated.
- **A `deferred_restarts` doctor check.** An entry is cleared only when its
  restart succeeded, so anything left is visible: a timed-out drain, a unit the
  helper refuses (it allowlists services, not sockets), or a deploy run by
  `deploy-daemon`, whose detached worker can die with its per-connection cgroup.
  That bound is written down rather than implied.
- **A killed deploy leaves a record.** `DeploymentStatusFile` gains
  `owner_pid`, `owner_boot_id` and `owner_invocation_id`. The kernel releases the
  flock when a deploy is SIGKILLed, so the *lock* recovered from the kill while
  the record did not: it sat at `deploying` forever and `fraisier status` painted
  it blue with a growing elapsed time, indistinguishable from a deploy that was
  working. The webhook now reconciles orphaned records at startup — the restart
  that kills a deploy is itself what brings the reconciler up — into a new
  terminal state **`interrupted`**, which is in `FAILURE_STATES`.
- `interrupted` is deliberately not `failed`: the deploy's own failure path never
  ran, so nothing rolled back, `version.json` was not restored, and the tree may
  be half-deployed. `owner_invocation_id` is not used to decide liveness; it is
  recorded so an operator can recover the dead deploy's journal with
  `journalctl _SYSTEMD_INVOCATION_ID=<id>`.
- **A self-upgrade that does not land says so, and does not make it worse**
  ([#351](https://github.com/fraiseql/fraisier/issues/351)). `uv tool install
  --force` removes before it verifies, so an install that failed partway left the
  tool venv half-removed — `bin/` gone, `lib/` intact — and every
  `~/.local/bin/fraisier*` symlink dangling, including the one the webhook unit
  names in `ExecStart=`. The running process outlived its deleted binary, so
  `is-active`, the health check and the version endpoint all looked normal; the
  damage surfaced only at the next restart, as 203/EXEC.
  - The worker now **resolves its own unit's entrypoint after the install and
    refuses the restart when it does not resolve** — on a zero rc as well as a
    non-zero one, because success is not proof. Restarting at that moment would
    turn a latent problem into an outage: the process running is the only working
    fraisier left on the host.
  - A failed upgrade is recorded in
    `<deployment.lock_dir>/.self-upgrade-failure`, **cleared only when a later
    upgrade lands**, and reported by a new `self_upgrade_failure` doctor check.
  - A new **`unit_entrypoints`** doctor check asks the question from the other
    end: every installed unit's `ExecStart=` fraisier binary must exist and be
    executable. It takes no config — the units on disk are the input — and it
    does not care how the host got there, so it catches a machine that is already
    in this state today.
- **Both detached workers were mute, and that had a one-line cause.** Neither
  `webhook_self_upgrade` nor `deferred_restart` configured logging, so Python fell
  back to `logging.lastResort` — WARNING level, straight to stderr — and every
  `log.info` was dropped, including the line naming the command about to run. The
  deferred-restart worker was additionally spawned onto `DEVNULL` for both stdout
  and stderr, so even its warnings were unrecoverable: the ledger recorded *that*
  a debt went unpaid and nothing recorded *why*. Both now route through one named
  seam, with a guard test asserting every `python -m` worker uses it.

### Changed

- **Units are only reinstalled and restarted when their bytes changed.** Both of
  the reporter's failing changesets edited `ship:` definitions and touched
  nothing reaching the webhook unit, so most `fraises.yaml` edits now restart
  nothing at all — and a rollback that reinstalls the previous config cancels its
  own debt.
- `read_status` ignores keys it does not know. A self-upgrade puts two fraisier
  versions on one host by design, and `DeploymentStatusFile(**data)` turned a
  field added by the newer one into a `TypeError` that took `fraisier status`
  down with it.
- Output is louder in the same direction as v0.62.0: every deferral prints the
  unit and the signal that caused it, every unchanged unit says it was skipped,
  and a restart that *does* happen announces that it terminates whatever is
  running inside the unit — so a deploy killed by an older host still names its
  cause in the journal.
- **confiture is tracked through 0.44** (`fraiseql-confiture>=0.38.0,<0.45`,
  [#262](https://github.com/fraiseql/fraisier/issues/262)). The `<0.39` cap was
  holding consumers on 0.38.1 with nothing behind it: the migrate/build/preflight
  surface fraisier consumes is unchanged across 0.38→0.44, and 0.44's
  `CREATE OR REPLACE` change is gated on a `window_safe` flag fraisier never
  reads, so it cannot alter deploy behaviour here. The upper bound stays, because
  its job is to prevent silent drift onto a *future* schema change.

### Upgrade notes

Nothing here adds or removes units, and a host that never changes `fraises.yaml`
during a deploy behaves exactly as before. Hosts that do will see their
config-changing deploys succeed for the first time, followed by a webhook restart
once the deploy finishes rather than during it. Run `fraisier doctor` after
upgrading: a unit deferred and never restarted is now reported instead of
silently running an old configuration.

Upgrading will also pull confiture forward from 0.38.x to 0.44.x. Nothing in
fraisier's behaviour changes with it — the full suite passes unmodified against
0.44.0 — but the resolved version moves, so a host that pins its own confiture
should check that pin.

`fraisier doctor` gains two checks worth running once after upgrading.
`unit_entrypoints` reports `fail` if any installed unit names a fraisier binary
that no longer resolves — a host whose earlier self-upgrade failed is already in
that state, and will not restart until it is repaired. `self_upgrade_failure`
reports the upgrade that left it there, if one did.

## [0.62.0] - 2026-08-08

`fraisier ship` decided whether a `triggers:` check ran by diffing the **working
tree**. A changeset that is already committed leaves a clean tree, so the
changed-file list was empty and **every** triggered check was skipped — with no
output at all, because the skipped checks were filtered out before the only code
that prints.

The reporter had 12 triggered checks and a changeset touching 6 files under
`db/`. Four ran: exactly the four with no `triggers:` at all. Among the silent
ones was the gate blocking a schema change with no migration — the check
specifically protecting migrate-only production.

Committing before `ship` is not exotic. `git add --update` stages only tracked
files, so a changeset adding a new file must be committed by hand; and a check
that inspects committed history only passes once the work is committed. Together
there was no working-tree state in which such a check both ran and passed.

This is v0.61.0's theme in the tooling rather than on a host. That release was
about units that could not fail visibly; this is a check that could not be seen
*not* to have run.

### Fixed

- **Triggered checks see committed changes** (#346). The changed set is now the
  **union** of `merge-base(HEAD, base)..HEAD` and the working-tree diff — not a
  replacement, because checks deliberately run *before* `git add --update`, so
  uncommitted work must keep counting. Untracked files stay out: `ship` already
  fails outright on untracked files under `db/migrations/`, so the dangerous case
  is covered without letting a scratch file run unrelated checks.

- **"I could not tell" no longer means "nothing changed".** `_get_changed_files`
  returned `[]` both when git failed and when nothing had changed, and `[]` was
  read as *skip*. So a failed git invocation silently disabled every gate. The
  changed set is three-valued now, and an undetermined one runs **every**
  triggered check. Same rule as `db restore`'s lock ("a lock that cannot be
  evaluated is an error, not a skip") and v0.61.0's `ArchiveVerdict.UNVERIFIABLE`.

- **`--pr-base` reaches trigger evaluation.** `ShipPipeline` was built from
  `ShipConfig` alone, and the CLI's resolved base only ever went to the
  version-race check — and only when `--pr` was passed. So `ship --pr-base dev`
  evaluated triggers against a different base than the PR it was about to open.

- **The base is never the current branch.** `_assert_no_version_race` resolves
  `pr_base or current_branch` and is right to, but the same fallback is fatal
  here: on an already-pushed feature branch `merge-base(HEAD,
  origin/<current-branch>)` is HEAD, so the diff is empty and the bug is
  reproduced by the fallback meant to fix it. Resolution is
  `--pr-base`/`ship.pr_base` → `origin/HEAD` → undetermined, and a test pins that
  the current branch is not used.

- **The changed set is computed once per run**, not once per check. It was called
  from inside the per-check predicate, so N triggered checks meant N `git diff`
  subprocesses.

### Added

- **A skipped check says so** — the part that let the bug live. The issue's own
  words: *the failure is silent, which is the worst part; a skipped check is
  indistinguishable from a passing one in the output.* Twelve checks collapsing
  to four would have been noticed the first time it happened.

  ```
  note could not determine changed files (no ship.pr_base configured …) — running all 2 triggered check(s)
  skip schema-gate — no file matched db/** (vs origin/main, 3 file(s) changed)
  pass ruff (0.4s)
  ```

  The reason names the patterns, the base and the changed-file count, which is
  what distinguishes "correctly skipped" from "skipped because the base was
  wrong". `PipelineResult.skipped` carries them separately from `results`:
  deliberately **not** a `CheckResult` with `success=True`, since anything
  summing results would count a skip as a pass — the conflation this release is
  about. A forced run under an undetermined base is announced too, once per
  phase, because a check running for a reason nobody can see is how the next
  person concludes `triggers:` does not work.

- **The `ship:` block is documented** — `checks:`, phases, and `triggers:` were
  not documented anywhere. The new section states what the changed set is, how
  the base resolves, and that patterns use `fnmatch` semantics where `*` crosses
  `/`, so `db/*` matches `db/migrations/001.sql`. That over-matches rather than
  under-matches, so it is left as it is: tightening it would stop a check that
  fires today, which is a regression in the direction this release is fixing.

### Rollout

Nothing here touches a host, a systemd unit or a database. The whole surface is
`fraisier ship`'s local pre-commit behaviour, so upgrading changes nothing until
the next `ship`.

**A repo using `triggers:` will run more checks than it did**, which is the fix.
Two consequences worth expecting:

- **A check that has not run in a long time may fail.** The honest reading is
  that it was never passing — this release did not break it, it stopped hiding
  it. That is the case the reporter hit: the gate protecting migrate-only
  production had been skipped every run.
- **Runs are louder.** Every skipped check now prints a line. A run that skips
  ten checks gains ten lines; a run that used to look short and green now shows
  why it was short.

If the undetermined note appears on every run, set `ship.pr_base` or run
`git remote set-head origin -a`.

Closes #346

## [0.61.0] - 2026-08-08

Four issues, one shape: **work that could not fail visibly.**

`install.sh` had always copied three timer pairs to every host and enabled
none of them. That was never a decision anyone recorded — it was the absence
of one, and it is why **all three pairs were broken and nobody could tell**.
A unit that never starts cannot fail visibly.

The received-corpus path had the same property from the other direction. A
dump arrived on a host and nothing between its arrival and its restore ever
asked whether it was a dump: `keep_minimum` counted files without reading
them, the restore dropped the database three steps before it first opened the
archive, and no one watched the disk the corpus lived on. Each gap was
invisible because the thing that would have reported it was the thing missing.

v0.60.0 reached the edge of both. It added `Disposition.TIMER` for #339's
retention units and had to explain, in three places, why the classification
applied to those and not to the timers already on every host; and it recorded
three non-goals rather than leaving them implied. Writing either down forced
the questions they were deferring.

**Nothing starts on upgrade, and a good dump restores exactly as before.** The
three timer families are switched by `scaffold.systemd.timers`, every one
defaulting to off. What changes is that inertness is *declared* rather than
accidental — and that a restore now refuses an unreadable dump before it
destroys the database that dump was meant to replace.

### Fixed

- **Two units could never have executed** (#341). `deploy-checker.service`
  and `restore-staging.service` each set `ProtectHome=true` while their
  `ExecStart` was `/home/{deploy_user}/.local/bin/fraisier` — the uv-tool
  install path that `ProtectHome` makes unreachable. Every firing would have
  been an exec failure. `deploy-service.j2` has carried a comment explaining
  precisely this since #72; these two contradicted it, and a hardening test
  listing `ProtectHome=true` as *required* pinned the contradiction in place,
  so the unit that could never run had a green test calling it correctly
  hardened.

- **`backup.service` was not loadable at all**, which the issue did not know.
  Its `ExecStart` read `{{ scaffold.output_dir }}/backup.sh` and rendered
  `scripts/generated/backup.sh` — a relative path, which systemd refuses. It
  was the last template still reading `scaffold.output_dir`, the CWD-relative
  *local render and review* path; #283 moved every other server-side path to
  `scaffold.state_dir` and missed this one. With an absolute `output_dir` the
  same defect renders as a path that exists only on the machine that ran
  `fraisier scaffold`. No template may read `scaffold.output_dir` now, and a
  test says so.

- **`backup.service` could not start on a host without `/var/backups/{project}`**
  — every host, since nothing created it. `backup.sh` opens with
  `mkdir -p "${BACKUP_DIR}"`, which reads as the script provisioning its own
  directory and cannot be: systemd builds the mount namespace *before*
  `ExecStart` and refuses to start a unit whose `ReadWritePaths=` target is
  missing, so the script never ran to create it. The grant is now `-`-prefixed
  and the directory is a `PathManifest` entry, created and owned like every
  other managed path.

- **The deploy checker did not check.** Every `ExecStart` passed `--force`,
  which skips `is_deployment_needed()` — the deployed-vs-latest comparison the
  unit exists to perform — so each firing redeployed every fraise and
  environment unconditionally. Compounding it, `health.deploy_poll_interval_seconds`
  defaulted to **5**: a plausible number for polling a socket, and an
  implausible one for a timer that git-fetches every fraise on the host. It
  now defaults to 60, and an explicit value is still honoured as written.

- **One unreachable deploy socket stopped the whole host being polled.**
  `trigger-deploy` exits 1 on a missing or refused socket, and an un-prefixed
  failure in a `Type=oneshot` unit aborts every later `ExecStart`. Each
  per-environment line is now `-`-prefixed: polling one environment must not
  depend on another being reachable.

- **`fraisier validate` crashed on a bad `scaffold:` section instead of
  reporting it.** `_collect_all_validation_errors` force-traverses every
  Stage-2 section so one run names them all, and `scaffold` was never in its
  list — unnoticed because nothing under it raised. `scaffold.systemd.timers`
  is the first thing that does, and without this a typo'd timer name escaped
  the traversal and surfaced as an unhandled `ValidationError` from inside
  `_check_deploy_user`, replacing every other check's output with a stack
  trace. Found by smoke-testing the new config surface end-to-end rather than
  by a test.

- **`host_gate()` matched nothing for an unconditional artifact**, found by
  this release's own new golden case. It rendered `_env_active ""` when an
  artifact carried neither fraise nor environment, and the effect was that
  switching a timer family **on** *removed* its copies from the install plan.
  It survived because every loop called the macro directly except `plain` —
  the only one that ever passed such an artifact — which wrapped it in a local
  `{% if artifact.environment %}` instead. The macro's own docstring says it
  exists so those loops "cannot drift apart on which predicate an artifact
  deserves", and one had drifted away from it.

- **A restore destroyed the database before it read the dump** (#343). The
  `restore_migrate` strategy stopped the service, terminated every backend and
  dropped and recreated the database — and only *then* handed the file to
  `pg_restore`. The two earlier steps that look like validation are not:
  `find_latest_backup` sorts by mtime and the age check compares mtime to a
  cutoff, so neither opens the file. A dump `pg_restore --list` rejects in
  under a second therefore cost the staging database it was meant to replace,
  which is the #339 incident exactly. The archive is now read at a new step
  before the service is stopped, and an unreadable one aborts with
  `pg_restore`'s own stderr.

  Deliberately outside both preflight escapes: `--skip-preflight` and
  `preflight.enabled: false` do not skip it. An emergency restore may
  reasonably skip *migration* validation; it may not skip "is this a file
  `pg_restore` can read", because that is the check standing between a corrupt
  dump and a live database. Preflight would not have caught it anyway — it
  extracts the **schema**, and a dump truncated inside its data section has a
  complete header, TOC and schema, so preflight passed on precisely the corpus
  that would fail.

- **The table-count hand-off claimed a check that was switched off** (#343).
  `restore_backup` passed `min_tables=0` into confiture's `RestoreOptions`
  under a comment saying the strategy validated the count itself "after
  `migrate up` (step 10), so confiture skips its own min-tables check". Step 10
  is `if cfg.min_tables > 0` over a value that defaults to `0`, so in the
  default configuration **neither** side validated and a comment said one did.
  The floor is forwarded now, and an absent floor is stated on the console
  instead of covered for.

  This is the release's other instance of the same lesson: a hardening test
  listing `ProtectHome=true` as *required* pinned a unit that could not exec
  because of it, and a hand-off comment asserted a guarantee by naming a
  disabled check. Both read as evidence and were not.

- **`keep_minimum` protected the corrupt file first** (#342). It exempted by
  index into an mtime sort, and a dump still being written is the newest entry
  in the directory — so the floor's first act was to protect it, and with a
  stalled producer it held that slot while every readable dump aged out around
  it. "The newest three are safe" reads like a validity guarantee; it was a
  count. A dump that contests a floor slot is now read with
  `pg_restore --list`, and one that fails does not hold a slot.

  The limit is exact, and narrower than it is tempting to claim: **no readable
  dump is removed that the old floor would have kept**, and anything newly
  removed is both unreadable *and* already past the retention window. The
  unreadable dump does lose the exemption it used to hold — that is the point —
  and an unreadable dump still inside the window is still kept.

- **A receiving host had no disk guard at all** (#344). `check_disk_space`
  existed and ran on the host that *produces* a dump; a host that receives a
  corpus by rsync had nothing, and #339's first cause was
  `/backup/production` on the destination growing until the disk filled.
  Retention bounds a corpus in the steady state and that is not the same as
  watching a disk — the policy can be correct while something else on the
  volume grows, and `keep_minimum` deliberately refuses to delete below its
  floor.

### Added

- **`scaffold.systemd.timers` (#341)** — three booleans, all defaulting to
  `false`:

  ```yaml
  scaffold:
    systemd:
      timers:
        backup: false           # nightly pg_dump | gzip -> /var/backups/{project}
        deploy_checker: false   # poll each fraise/env, deploy when the branch moved
        restore_staging: false  # nightly staging restore from the production backup
  ```

  `false` classifies the pair `PLAIN` — copied and inert, as today. `true`
  classifies it `TIMER`, which install.sh's existing sequence installs and
  enables, so switching a family on is a classification change rather than a
  new code path in the installer. An unknown name or a non-boolean value is a
  config error naming it: ignoring an unknown key is how an operator comes to
  believe they enabled a nightly backup that never runs, and coercing a truthy
  string is the same failure pointing the other way, since YAML reads bare
  `yes` as a boolean but quoted `"yes"` as a string.

  Keys are stable identifiers, deliberately not the rendered unit filenames —
  a filename derived in a second place is the drift #323, #325 and #337 each
  closed.

- **`install.sh` reports what it did not start.** An "Installed and not
  enabled" block, beside the existing app-managed and known-gap reports,
  naming each copied timer and the line that enables it.

- **`fraisier doctor` gains `inert_timers`**, always `pass`. Every family off
  is a configured state, not a fault, and a check that warns about a
  deliberate choice on every run becomes wallpaper. What it adds is that the
  state is legible: nothing previously distinguished "copied and running"
  from "copied and never started".

- **A rendered-unit sweep** (`tests/test_rendered_unit_sanity.py`). No unit may
  pair `ProtectHome` with an `ExecStart` under `/home`; every `ExecStart` must
  be absolute; none may point into the local render directory. Asserted over
  **every** rendered `.service` rather than the three that were wrong, and over
  rendered output rather than template text — both broken `ExecStart` values
  were assembled from a Jinja variable, so no grep over the `.j2` files finds
  them.

- **Two golden matrix cases**, `timer_families_off` and `timer_families_on`.
  The matrix had no config declaring `restore_migrate`, so the restore-staging
  pair had never appeared in the golden plan at all, and no case enabled a
  timer. The `host_gate` bug above is what that omission was hiding.

- **`fraisier.dbops.archive.verify_archive` (#342, #343)** — one answer to "is
  this a readable pg_dump archive", read by the restore path, `backup prune`
  and `doctor`. The producing side's TOC check now routes through it too, so
  there is one implementation and a test rejects a second `pg_restore --list`
  call site.

  The answer is **three-valued**, and that is the load-bearing part. A host
  whose job is to hold dumps may have no PostgreSQL client tools, so
  `UNVERIFIABLE` is its own verdict and is never treated as "the dump is bad" —
  one caller deletes files on that reasoning and another refuses to restore.
  `ArchiveCheck.is_bad` is `INVALID`-only, and a tree-wide test rejects any
  `verdict != VALID` comparison in code so the safe reading stays the only one.
  Same rule `db restore` already applies to a lock it cannot evaluate.

  The file header is not the check: a dump truncated mid-transfer still carries
  the `PGDMP` magic, so reading five bytes proves nothing.

- **`backup.environments.<env>.retain[].min_free_gb` (#344)**, optional. On the
  entry rather than under `backup:` because a threshold is a property of a
  volume and each entry names its own directory; two corpora on different disks
  need different numbers. **Absent means no threshold** — which every config
  written before this is — so the default upgrade path gains information and no
  new failure.

- **`fraisier doctor` gains `backup_corpus_free_space` (#344).** Fails below a
  declared threshold naming free space and the floor, passes above it, and
  passes with no threshold *while saying none is set*. A `retain[].dir` that is
  not a directory fails — already the #339 shape. A volume whose free space
  cannot be read is `skip`, never `pass`.

- **`backup prune` reports two new conditions on stderr**, both exiting 0: a
  dump that was refused a floor slot, and a corpus volume below its threshold.
  A non-zero exit would convert either into a failed unit and stop the pruning
  that is the one thing that might still help — the same reasoning as the
  existing stalled-producer warning. The `--json` report carries an `invalid`
  list per entry, documented as an **overlay**: every path in it also appears in
  exactly one of `removed`/`kept`/`exempted_by_minimum`, so summing all four
  double-counts the corpus.

- **`dbops.backup.free_space_gb`**, the single free-space measurement;
  `check_disk_space` reads it. A test rejects a second `shutil.disk_usage`
  caller and caught one immediately — the new doctor check's first draft reached
  for it directly, which is how the producing and receiving sides came to
  disagree in the first place.

### Changed

- **`restore-staging` is installed** and is no longer an `UNINSTALLED_GAP`
  (#341). Its units render only where a fraise declares
  `database.strategy: restore_migrate` on a staging-named environment, so
  installation was already gated by intent; firing nightly is not, and stays
  behind the knob. `_KNOWN_GAPS` is now empty — the classification stays, since
  it is the honest label for the next artifact rendered, needed and reached by
  no installer, and its tests synthesise a gap rather than depending on one
  existing. A test that only passes while a bug exists disappears with the bug.

- **`doctor`'s `scaffold_artifact_coverage` goes `warn` → `pass`** on a
  `restore_migrate` tree, which has warned in every release until now.

- **The v0.60.0 pins changed shape rather than disappearing.**
  `test_existing_timers_are_still_not_enabled` asserted the
  retention-only asymmetry as permanent; it now asserts the rule that replaced
  it — a timer is enabled if and only if a config asked for it — across the
  golden matrix, plus the delta between two otherwise identical configs.
  #339's retention pair still fires with no knob: a retention unit that does
  not run reproduces the incident it was built to prevent.

- **`docs/doctor.md`'s catalog is complete, and a test keeps it so.** It listed
  10 of 16 registered checks, and #344 would have made it 10 of 17. Fixed here
  rather than deferred: documentation describing a subset reads as describing
  the whole, which is the same defect class as a unit nothing enables. The test
  compares the catalog against `DOCTOR_CHECKS` in both directions.

- **Retention tests declare their dumps' validity.** Eleven floor tests wrote
  stub bytes into files, so the real `pg_restore` calls them invalid — meaning
  they would have asserted all-invalid semantics on a machine with
  `pg_restore` and all-valid semantics on one without it. An
  environment-dependent retention test is the argv-dependence flake class PR
  #306 removed, so the fix is to stop depending on the environment.

### Not fixed, and why

- **The 21-second run in #343 is still unexplained.** Two defects on that path
  are fixed and neither required the reporter's journals. What the journals
  would settle is which of them the reporting run actually hit, so #343 stays
  open on that question.

  One hypothesis is disposed of: the issue suspects `--skip-if-locked` letting
  a "someone else is doing it" exit read as "it is done".
  `file_deployment_lock` acquires with `fcntl.LOCK_EX | fcntl.LOCK_NB` and
  raises on the first `BlockingIOError`, so that path exits in milliseconds and
  prints `Skipping restore:` while doing it. 21 seconds means real work
  happened. Also worth recording: `db restore` has **no `--wait` flag**; the
  reproduction to ask for is the `systemctl start --wait` invocation of the
  generated unit.

### Rollout

**Re-scaffold and re-install on every host**, both halves and in that order:
`fraisier scaffold && sudo fraisier scaffold-install --yes`. `install.sh` bakes
in the hashes of the artifacts it installs and refuses a tree from a different
render.

**By default nothing starts and nothing new is deleted.** Every pre-existing
case in the golden install plan gained exactly three lines — creating
`/var/backups/{project}` — and lost none. No `systemctl enable` line moved. A
restore of a readable dump runs the same steps in the same order with the same
exit code, and a prune of a readable corpus removes exactly what it removed
before.

Cases where behaviour does change:

- **A restore of an unreadable dump** now fails before the service is stopped
  and before the database is dropped, where it used to fail after. This is the
  change the release exists for.
- **A prune of a corpus containing an unreadable dump past its retention
  window** removes that dump, where the floor used to protect it, and protects a
  readable one in its place.
- **A host with no `pg_restore`** loses nothing: both new checks report that
  they could not run and proceed. Retention behaves exactly as before, because
  unverifiable dumps spend floor slots normally.
- **A host where an operator hand-enabled `backup.timer`.** The unit goes from
  refusing to load to running a nightly `pg_dump | gzip` at 03:00. That is the
  unit finally working, and it is also the first time that host writes to
  `/var/backups/{project}`. Its retention is `backup.sh`'s own 30-day window
  with a floor of 3 per database — **not** #339's `retain:` policy, which
  describes a corpus this host *receives*. A host can want both; they do not
  interact.
- **A host where an operator hand-enabled `deploy-checker.timer`.** It goes
  from failing to exec on every tick to a real poll — deploying only when the
  branch has moved, every 60s rather than every 5s unless
  `deploy_poll_interval_seconds` is set explicitly.
- **A host where an operator hand-installed the restore-staging pair.**
  `scaffold-install` now owns those two files and will overwrite them.

`/var/backups/{project}` is created on every host, including ones that enable
no timer — it is a global managed path with no owning environment, like
`/opt/fraisier` and `/var/lib/fraisier`. It is empty until a backup runs.

Nothing in this release requires a config change. `scaffold.systemd.timers` and
`min_free_gb` are both opt-in, and both default to the behaviour a host already
has.

## [0.60.0] - 2026-08-08

Retention for a backup corpus a host **receives**. A destination that is
rsync'd a nightly dump has no fraise producing it, so nothing on that host
knew the corpus existed and nothing pruned it; the unit meant to was
hand-written in the consuming repo, installed by nobody, and checked by
nothing until the disk filled.

Built on v0.59.0 rather than beside it: the config surface is keyed by
environment, and after #336 that predicate means "a fraise **on this host**
declares this environment". The same YAML that would have inherited a
cross-install bug one release ago is correctly scoped here with no migration
and no grandfathered key.

**Purely additive on live hosts.** A config with no `retain:` block renders
exactly what it rendered in v0.59.0 — the golden install plan gained 109 lines
and lost none.

### Added

- **`backup.environments.<env>.retain` (#339).** The first validated structure
  under the top-level `backup:` key, which every other consumer reads raw.
  Each entry declares a directory, a glob, a retention window, a floor and a
  schedule; `name` defaults to the directory basename and `user` to
  `scaffold.deploy_user`. Two entries in one environment may not resolve to
  the same name — that is a config error naming both, never a silent rename.
  An environment no fraise declares is rejected at config load rather than
  rendered into units that would be copied, gated and never fire.
- **`fraisier backup prune --env <env> [--name] [--dry-run] [--json]`.** The
  command an operator runs by hand on the destination, and the one the
  rendered timer invokes. Every "nothing to do" case exits non-zero: an
  unknown `--name`, an environment with no policy, a `dir` that is not a
  directory. A corpus kept alive only by its floor produces a WARNING on
  stderr and exits 0 — a stalled producer must be visible without also
  putting the timer in `failed`.
- **`Disposition.TIMER`** — `PLAIN` plus `systemctl enable --now`, after the
  units are on disk and systemd has re-read them. `install.sh` copies timers
  and enables none of them, so a retention timer filed as `PLAIN` would be
  rendered, installed, hashed, drift-checked and never run: the incident's own
  failure mode reproduced inside the system built to prevent it. Applied to
  the retention pair **only** — `backup.timer` and `deploy-checker.timer` stay
  copied-and-inert, pinned by a test and by the golden plan, because enabling
  `backup.timer` on upgrade would start a legacy `pg_dump | gzip` on every
  existing host as a side effect of a retention fix. Tracked in **#341**,
  which also records that two of those units would fail 203/EXEC today if they
  were enabled (`ProtectHome=true` hides the `~/.local` binary their
  `ExecStart` names).
- **`fraisier doctor` reports declared corpora and whether anything prunes
  them.** `scaffold-diff` reports a missing retention unit for free — the units
  are fraisier's now, so they are in the artifact manifest.
- **`naming.retention_unit_names(project, env, entry)`** — one authority for
  the pair's names, written before the second call site rather than after the
  third. That is #337's lesson, applied rather than only shipped.

### Changed

- **`cleanup_old_backups` returns `CleanupOutcome`, not `list[str]`.** The
  three tuples partition the corpus: `kept` would have survived with no floor,
  `exempted_by_minimum` survived only because of it. Keeping them apart is
  what makes `floor_was_load_bearing` — "the producer has stalled" — knowable
  at all; it cannot be reconstructed from a list of deletions. No compatibility
  shim; the one in-repo caller moved.
- **`cleanup_old_backups(..., dry_run=True)`** selects exactly as a real run
  does, containment guard included, and deletes nothing. A parameter rather
  than a candidate list rebuilt in the CLI: a preview derived from a second
  implementation of "what expires" previews something else.
- **The pre-migration dump gate keeps the newest dump** (`keep_minimum=1`). A
  deliberate behaviour change on live hosts, in the safe direction — it deletes
  strictly less. That dump is the migration's rollback point; expiring it
  leaves a migration with nothing to fall back to.
- **`backup.sh` keeps a minimum too.** Its prune was
  `find … -mtime +N -delete`: time-based, no floor, on the producing host.
  The floor is per database, not per directory — one shared count would keep
  three of whichever name sorts last and none of the others. `BACKUP_DIR` now
  honours `FRAISIER_BACKUP_DIR`, which is also what lets a test run the real
  script.
- **`fraisier backup` is a command group.** `fraisier backup <fraise> -e <env>`
  is unchanged: anything that is not a known subcommand routes to
  `backup run`. A fraise named after a subcommand is reachable as
  `fraisier backup run <fraise>`.

### Fixed

- **`scaffold-diff` aborted for any non-root caller.** Its orphan scan called
  `Path.exists()` unguarded, and `exists()` swallows only
  ENOENT/ENOTDIR/EBADF/ELOOP — EACCES is re-raised. `/etc/sudoers.d` is mode
  0750 root:root, so the whole diff died with `PermissionError` before
  reporting a single missing unit. `_compare_files` already guarded its own
  `exists()`; this loop did not. Found because #339 relies on that command
  reporting. Predates #339 and affected every artifact, not just retention.

### Not covered

Stated so they are not assumed closed. This release closes the retention hole;
it does not close the validity hole.

- **Nothing verifies a dump a host receives (#342).** `keep_minimum` counts, it
  does not validate — and a partially transferred dump is the *newest* by
  mtime, so the floor's first act is to protect it.
- **`db restore --wait` reported success in 21s for a failed restore (#343).**
  From #339's tail; journal excerpts offered by the reporter.
- **No disk-space guard on a receiving host (#344).** The incident's first
  cause. Retention bounds a corpus in the steady state; it does not alarm.

## [0.59.0] - 2026-08-08

One theme: a fact has one authority. A host's answer to "which artifacts are
mine?" was keyed on the environment *name*, discarding which fraise declared
it, so two fraises putting the same name on different servers made each host
install the other's units. The path the unit-installer socket listens on was
derived independently in three places. Both now have exactly one writer.

**One behaviour change on live hosts:** a host stops installing a neighbour's
units. The ones already installed are reported, never removed — see
`--prune-foreign` below.

### Fixed

- **Host scoping is by `(fraise, environment)`, not by environment name
  (#336).** `iter_environment_servers` walked past the declaring fraise and
  threw it away, so `machine_env_map` — the map `install.sh`'s gate reads —
  could only answer "does any fraise anywhere declare this name?". A config
  with `api` on box-a and `worker` on box-b, both under `production`, made
  each box install the other's app service, deploy socket and template
  service, and create and chown the other's `git_repo` and `app_path`.

  `install.sh` now carries two predicates. `_scope_active <fraise> <env>`
  gates what one fraise owns: app services, deploy sockets, install-helper
  pairs, nginx vhosts, and the managed directories those live in.
  `_env_active <env>` gates what no single fraise owns: the unit-installer
  helper is one per `(project, environment)` by design (#240), and the
  postgresql logging conf is per environment. Two rather than one because
  forcing the second kind through a fraise-keyed gate would mean inventing an
  owner for it — which is how a second host authority gets born.

  **A config declaring `server:` in the global `environments:` section is
  unaffected.** That declaration has no owning fraise and correctly binds
  every fraise using the name; only per-fraise `server:` declarations become
  fraise-scoped. Seven of the ten cases in the golden install-plan matrix are
  byte-identical across this change.

  Three other consumers were keyed the same way and moved with it: `doctor`'s
  hosted-trees check (which would otherwise have reported a correctly scoped
  webhook unit as broken for not granting a neighbour's trees), `setup`'s
  environment filter — which creates users, chowns trees and *enables* units —
  and `fraisier status --server`.

- **The unit-installer socket path is written once (#337).** The socket unit's
  `ListenStream=`, the path `scheduled-install` probes, and the path the
  webhook's auto-install probes were three independent copies of the same
  formula. All three read `naming.unit_installer_socket_path`, as does the
  `--socket-path` help text that stated the default a fourth time. Both
  consumers degrade quietly when the socket is absent, so drift here never
  crashed — it just stopped auto-installing. No rendered output changes.

### Added

- **`scaffold-diff` and `doctor` report foreign units.** A host mis-scoped
  before #336 still has its neighbour's units on disk, enabled, possibly
  serving traffic. Both surfaces now name each one with its owner —
  *"installed here, owned by fraise X which does not run on this host"* — and
  neither touches it.

- **`fraisier scaffold-install --prune-foreign`** disables and deletes them,
  for an operator who has read the report. Never a default: `install.sh` does
  remove stale pre-0.7.1 socket units, but those carry a name fraisier itself
  assigned under a superseded scheme, while a neighbouring fraise's unit is
  another application's service. Stopping it as a side effect of a routine
  `scaffold-install` would be an outage on exactly the configs #336 describes.
  The prune re-derives ownership rather than trusting its caller's list and
  refuses any unit whose owner resolves to this host.

- **`cleanup_old_backups` takes `keep_minimum` and `match`** (#339, request 1).
  The prune was purely time-based, so a stalled producer aged an entire corpus
  out at once. `keep_minimum` exempts the newest N by mtime *before* the age
  test, which is what makes "the newest 3 survive" true in the only case that
  matters — when all of them are already past the cutoff. `match` scopes the
  glob, so full and slim dumps sharing a directory can expire on different
  clocks.

### Changed

- **`cleanup_old_backups` returns `CleanupOutcome`, not `list[str]`.** The
  three tuples partition the corpus: `kept` is within retention and would have
  survived with no floor at all, `exempted_by_minimum` is past the cutoff and
  survived only because the floor held it back. `floor_was_load_bearing` reads
  "nothing survived on its own merits" — the stalled-producer signal, which is
  only knowable at prune time and cannot be reconstructed from a list of
  deletions. No compatibility shim; the one in-repo caller moved.

- **The `pre_migrate_dump` gate keeps the newest dump.** It now prunes with
  `keep_minimum=1`: that dump is the rollback point for the migration the gate
  is about to allow, and expiring it in the same breath leaves that migration
  with nothing to fall back to. Deletes strictly less than before.

## [0.58.0] - 2026-08-04

v0.57.0's coverage assertion found four artifacts that were rendered and
installed by nothing, and reported them rather than fixing them. This release
fixes all four. It also closes the directory half of #325's host gating — the
units were filtered by host, the directories they live in were not — and gives
ruff's version one place to be pinned instead of three.

Nothing here is a new capability. Each entry is a unit that has always been
rendered and has never reached a host.

### Fixed

- **`backup.service` was never installed, and `backup.timer` has always
  activated it.** The timer carries no `Unit=`, so systemd activates the unit
  with its own stem. `install.sh` copied the timer and not the service, so
  every firing hit a unit that was not there. The set it was missing from was
  named `_PLAIN_TIMERS` — named for what happened to be in it, which is how a
  timer came to be listed without the service it activates. It is now
  `_PLAIN_UNITS` and its comment states the pairing rule.

- **The alert unit `backup.service` fails into was never installed.**
  `backup.service` declares `OnFailure=fraisier-{project}-backup-alert@%n.service`
  and the renderer writes that template unit; no installer ever copied it. A
  missing `OnFailure=` target does not fail loudly — systemd logs that it could
  not enqueue the job — so the one failure the alert exists to announce was the
  one that went out silently. Installed unconditionally, matching how it is
  rendered: it is referenced by a unit that is itself unconditional. The pin
  reads `OnFailure=` out of the rendered `backup.service` and asserts the
  manifest installs *that* name, rather than checking a filename twice.

- **The deploy checker was rendered under a name its timer never activates.**
  `deploy-checker.timer` carries no `Unit=` either, so systemd looks for
  `deploy-checker.service`. The renderer wrote it as `poll-deploy.service`, at
  the scaffold-tree root rather than under `systemd/` — a name the timer never
  looks for, in a directory the installer never copies from. Two independent
  ways to miss.

  The unit is **renamed**, not retargeted with `Unit=`. For a host already
  running the timer the two are not equivalent: renaming leaves the installed
  timer correct and lands the fix by the presence of one file, while
  retargeting requires the timer to be replaced *and* daemon-reloaded *and*
  restarted, so a half-applied install leaves it pointing at a unit that is not
  there — the state being fixed, now harder to see because the tree looks
  current. Nothing is orphaned: no installer ever placed `poll-deploy.service`
  on a host.

- **`scaffold-install` now installs the unit-installer socket that
  `scheduled-install` requires.** `scheduled_install` refuses to apply unit
  diffs when the helper socket is absent and tells the operator to run
  `fraisier scaffold-install --yes` to bootstrap it. `scaffold-install` had no
  line for these units at all. Each side pointed at the other; neither
  installed anything. Webhook-driven auto-install of new units for
  `type: scheduled` fraises therefore returned early on every host, logging a
  warning that named a socket and a bootstrap command that had never installed
  it.

  The install sequence is #279's re-bake, for #279's reason rather than by
  analogy: the `.service` bakes its `--allow` allowlist into `ExecStart` as
  argv, so a running instance keeps the *old* allowlist and `enable --now` is a
  no-op on it. Copy, daemon-reload, **stop** the stale-argv `.service`, then
  enable and restart the `.socket` so the next connection re-execs with the new
  argv — `_run_strict` throughout, because a half-applied re-bake otherwise
  surfaces as a "not allowed" deep inside a later deploy. It is kept as its own
  disposition rather than folded into `helper_rebake`: same shape, different
  driver — one helper per environment versus one per (fraise, environment) —
  and sharing the disposition would let a manifest query start matching units
  that block does not install.

  `naming.unit_installer_unit_names()` joins `deploy_socket_name` as the sole
  authority for these unit names; the renderer that writes the files and the
  manifest that installs them were two independent derivations of one name.

- **`install.sh` no longer provisions other hosts' directories.** It iterated
  every managed path in `fraises.yaml` and `mkdir`/`chown`ed each one
  unconditionally, so a production-only host created the dev host's `git_repo`
  and `app_path`. The units were host-filtered; the directories those units
  live in were not — #325's shape, one layer down. Empty and wrongly present,
  they read as a half-provisioned environment on a box that should carry no
  trace of it, and they are chowned to the deploy user, so they also granted
  the write access the gating was meant to withhold.

  `ManagedPath` gains `environments` and the directory block gates on
  `_env_active` — the same authority the artifact installs already use. No
  third resolver: the environment names come from the config walk that produced
  the path. `environments` is a tuple rather than a single name because paths
  deduplicate by location, so an environment claiming a path an earlier one
  already claimed is folded in rather than dropped; gating on whichever was
  seen first would leave a shared directory uncreated on a host running only
  the other. Paths no environment owns — `/opt/fraisier`, `/var/lib/fraisier`,
  the config dir — carry an empty tuple and stay unconditional.

- **CI lints with the ruff the repo pins.** Both lint jobs ran
  `uv pip install ruff` with no constraint, so CI linted with whatever ruff had
  been released that morning while pre-commit used its own pinned rev and
  `uv.lock` a third version — 0.16.1, 0.16.1 and 0.16.0 at the time. The same
  disagreement the artifact manifest exists to prevent, one layer out. The
  `ruff-pre-commit` rev is now the sole authority and both workflows read it,
  erroring if it cannot be read rather than silently falling back;
  pre-commit.ci autoupdate keeps it current without a second edit. `publish.yml`
  is the one that mattered: an unpinned ruff on a release gate can fail a
  release on an upstream formatter change nobody chose.

### Known limitations, now tracked rather than latent

- **Host scoping is by environment *name*** (#336). `get_environments_for_server`
  returns environment names and discards which fraise declared them, so two
  fraises using one environment name on different servers each look active on
  both hosts, and each host installs the other's units and creates the other's
  directories. This predates this release and affects units and directories
  identically — gating directories neither introduced it nor could fix it,
  since the fix is to scope by (fraise, environment) and that changes which
  units live hosts install.
  `test_host_scoping_is_by_environment_name_not_by_fraise` states it and fails
  the day it stops being true.

- **The unit-installer socket *path* is still derived in three places** (#337) —
  `cli/scheduled_install.py`, `deployers/scheduled.py`, and the socket unit's
  `ListenStream=`. Same drift class as the unit *names* fixed here; the path
  has no authority yet.

- **`restore-staging.service` / `.timer` remain declared gaps, on purpose.**
  Both halves are uninstalled, so nothing fires into a missing unit — unlike
  the four closed here, this case is self-consistent. What it *should* do is a
  decision, not an oversight, and `_KNOWN_GAPS` names it so `install.sh` and
  `doctor` keep reporting it.

### Rollout

**Re-scaffold and re-install on every host** — that is what applies these
fixes. `fraisier scaffold && sudo fraisier scaffold-install --yes`. Both halves
and in that order: `install.sh` bakes in the hashes of the artifacts it
installs and refuses a tree from a different render.

**The two timer fixes take effect only where an operator enabled the timer.**
`install.sh` copies `backup.timer` and `deploy-checker.timer` and has never
enabled either. On a host where neither is enabled, nothing here starts
anything that was not already running — the units simply become correct for
whenever they are enabled. On a host where an operator *did* enable one, every
firing has been hitting a unit that was not there, and from this release hits
the service.

**Directories already created on the wrong host stay.** `install.sh` never
removes anything; the gate only stops new ones being created. They are empty
and owned by the deploy user, so they can be removed by hand after confirming
they are empty — check before removing, since the gate does not distinguish a
directory it wrongly created from one that acquired contents since.

**The unit-installer fix is what makes the documented bootstrap true.**
Operators told to run `fraisier scaffold-install --yes` to get webhook
auto-install working on a `type: scheduled` fraise can now do exactly that; the
socket has never been installed by that command before.

## [0.57.0] - 2026-08-03

Closes #323, #324, #326, #328, #331. One bug class, at the root: **two
components that must agree about the same fact were written separately and
drifted.** #325 (v0.56.0) was the first instance anyone chased to the bottom;
these are the rest, plus the structure that makes a sixth instance fail at
render time instead of on a production host.

### Added

- **The artifact manifest — `fraisier scaffold` now records what it wrote, and
  who installs each piece** (#323). `render()` always knew exactly what it
  produced and then discarded that knowledge, leaving three components to
  reconstruct it by hand: sixteen hardcoded names in `install.sh.j2`,
  `get_install_mapping()`, and `scheduled_install`'s directory scan. Every bug
  in the "rendered ≠ installed" class lived in that gap.

  The manifest mirrors `PathManifest` — already the single source of truth for
  managed *paths* — and extends the idiom to artifacts. Each artifact declares
  a **disposition**, and the manifest **routes; it does not execute**. The
  install sequences are not uniform and the non-uniform ones are load-bearing:
  the #279 re-bake must `cp` → `daemon-reload` → *stop the .service* →
  `enable` + `restart` the .socket in that order, because `enable --now` is a
  no-op on a running unit; the systemctl-helper must `daemon-reload` *before*
  restarting or the stop phase wipes `/run/fraisier`. A generic executor would
  discard the reasoning that makes those correct, so each keeps its
  hand-written, commented form. Only `plain` artifacts are installed
  generically — and that is the block where custom-named units used to fall
  through.

  **The load-bearing part is the coverage assertion**, not the generic install.
  Every rendered file must classify; an undispositioned artifact is a hard
  error naming the file and listing the dispositions to choose from. A new
  artifact cannot be rendered and then installed by nobody. It fires at render
  time and in `fraisier doctor` — `install.sh` runs on a live host mid-deploy
  under the self-upgrade dynamic, so the deploy-time check is the *backstop*,
  never the discovery mechanism.

- **`install.sh` refuses a scaffold tree it does not describe.** Each
  artifact's sha256 is baked into the generated installer. Without this the
  drift merely moves up a level, with the installer faithfully installing files
  nobody described — #323's triage found exactly that, v1.141.0-era rendered
  units in an app repo shadowing the current render. Verification is a
  preflight pass over the whole tree: checking inside each copy would install
  whatever sorted first and refuse midway, leaving the host half-converted
  between two renders, on the one tool you would use to recover.

- **`install.sh` states the boundary it used to leave silent** (#323). It now
  ends by naming the units `fraisier scheduled-install` owns — with their
  source tree and the command that installs them — and the artifacts that are
  rendered and installed by nothing, each with the consequence. The two
  installers still cover disjoint sets, from genuinely different source trees;
  what changes is that neither is silent about it. A wildcard install over the
  scaffold dir was rejected: that directory accumulates, so a wildcard promotes
  a leftover file into an installed unit — #325's failure mode generalised.

### Fixed

- **Four artifacts were rendered and installed by nothing**, found by the
  coverage assertion and now tracked rather than silent: `backup.timer` is
  installed and, having no `Unit=`, activates a `backup.service` that
  `install.sh` never copied; `deploy-checker.timer` likewise activates
  `deploy-checker.service` while the rendered file is named
  `poll-deploy.service` and written to the tree root; the backup-alert unit
  referenced by `OnFailure=` reached no host; and `scheduled_install` requires
  a unit-installer socket while telling operators to run
  `fraisier scaffold-install --yes`, which has never installed it. They are
  reported by `install.sh` and by `doctor` with what each one breaks.

- **An nginx vhost without an explicit `server_name` was never installed.**
  Three components computed the vhost filename independently and the
  installer's copy omitted the project prefix: the renderer wrote
  `{project}_{fraise}_{env}.conf` while `install.sh` looked for
  `{fraise}_{env}.conf`. Behind a `[ -f ]` guard that made it a silent skip —
  rendered, not installed, nothing said so. The formula now lives in one place
  and both sides read the same manifest entry. Found by this bundle's own
  coverage work, and pinned by a golden matrix case.

- **Every installed artifact is verified, not just the generically-copied
  ones.** The sudoers fragment, both helper socket pairs, the #279 re-bake
  units and the nginx vhosts each guarded their own copy with `[ -f ]`, which
  on its own is a silent skip — the shape of #325. The preflight now covers
  every artifact the manifest says is installed, including the webhook unit
  checked against the hash of the file selected for *this* host. Previously a
  webhook unit that was present but **stale** passed unnoticed: v0.56.0 caught
  a missing one, not a wrong one.

- **Version bumps refresh `uv.lock`** (#328). `ship` and `version bump` rewrote
  the version in `pyproject.toml` but never re-locked, so every release commit
  of a uv-managed project shipped a lockfile whose own `[[package]]` entry was
  one bump behind. Any later bare `uv run` then re-locked mid-command and
  dirtied the tree, which `fraisier sync` aborts on — hit twice on
  printoptim_backend on 2026-08-03 alone. `refresh_uv_lock()` runs `uv lock`
  when a sibling lockfile exists, and warns rather than fails when uv is
  absent, unexecutable, or slow: it runs *after* `bump_version` has committed
  pyproject.toml, so raising there would abort a ship on a half-applied bump —
  precisely the dirty tree this fixes. Bounded by a 120s timeout.

- **`fraisier deployment-status` no longer tracebacks on an unreadable
  `/run/fraisier`** (#326). `Path.exists()` propagates `EACCES`, and the
  directory is readable only by the deploy user — so the traceback hit exactly
  the operators most likely to run the command, following the hint
  `trigger-deploy` prints on timeout. Now a message naming the cause and the
  fix, exit 1. The same unguarded `exists()` in `_diagnose_deployment_status`
  is fixed with it.

- **`get_install_mapping()` no longer disagrees with the installer.** It mapped
  `systemd/poll-deploy.service` — a path the renderer never writes, under a
  name nothing installs — mapped restore-staging units nothing installs, and
  walked *every* fraise rather than this host's, so on a multi-host box
  `scaffold-diff` compared units the box does not install and reported them
  missing. Now derived from the manifest.

- **`fraisier setup` on an unrecognised machine no longer provisions every
  environment** (#331). `_resolve_allowed_environments` matched the machine's
  hostname against *logical server names* only, never against
  `servers:.machine_hostnames` — the map that exists precisely because a
  logical server is not a machine hostname. A config shaped like

  ```yaml
  servers:
    printoptim-io:
      machine_hostnames: [pio]
  environments:
    production: {server: printoptim-io}
  ```

  matched nothing on `pio`, and no match returned `None`, which the caller
  reads as *provision everything*. Resolution now routes through
  `resolve_local_server` — the sole host authority since v0.56.0 — which
  consults `machine_hostnames` first.

  **Breaking for multi-host configs:** an unresolvable host is now an error,
  not a warning-then-widen. `setup` creates users, chowns application trees and
  installs and **enables** systemd units and nginx vhosts; doing that for every
  environment from a box that cannot identify itself is a refusal to answer
  combined with maximum action, and a live candidate for how a production-only
  host acquires development units — the #325 failure shape one level up. There
  is no deprecation cycle because the warning already existed, already fired,
  and #331 exists *because nobody acted on it*.

  The error names this machine, lists the hosts the config knows with their
  registered machines and environments, and prints all three exits copy-paste
  ready: a `servers:` registration snippet, `--server <host>`, and a new
  `--all-environments` flag for "everything, deliberately".

  Unaffected: configs where **no** environment declares a `server:`. That is
  single-host, and provisions everything as before — a separate branch, not a
  fallback, because there "everything" and "this host's" are the same set.

  Also: `--server` naming a server no environment declares is now an error
  instead of silently widening to every environment.
||||||| parent of 0403ec0 (fix(scaffold): give deploy-daemon units the webhook's install-helper routing (#324))
- **Deploy-daemon units now carry the install-helper socket routing** (#324).
  The scaffolded webhook unit baked in
  `Environment=FRAISIER_INSTALL_SOCKET_<FRAISE>_<ENV>=…`; the deploy-daemon
  unit behind every `trigger-deploy` — and so behind every timer and CLI
  deploy — did not. With an `install.user` different from the deploy user,
  `deployers/mixins.py` looks for exactly that variable, does not find it,
  and falls back to `sudo -u`, which the unit's own `NoNewPrivileges=yes`
  denies outright:

  ```
  Install command failed (exit code 1): sudo -H -u printoptim_app …
    stderr: sudo: The "no new privileges" flag is set, which prevents sudo
            from running as root.
  ```

  The identical deploy through the webhook succeeded — same config, same
  render, two units running the same code, one wired. Reported from
  printoptim.dev.

  Fixed as a symmetry property rather than a patch: a test sweeps every
  rendered unit whose `ExecStart` performs a deploy **in process**, and
  asserts they all carry identical routing and are all `NoNewPrivileges`-
  hardened. A third deploy-capable unit cannot be added unwired. Units that
  only *ask* for a deploy (`poll-deploy` runs `trigger-deploy`, which writes
  to a socket and hands off) are correctly excluded — the install runs in the
  daemon on the other end.

  Applies on re-scaffold + re-install.

### Rollout

**Re-scaffold and re-install on every host** — that is what applies these
fixes. `fraisier scaffold && sudo fraisier scaffold-install --yes`.

Both halves are required and in that order. `install.sh` now bakes in the
hashes of the artifacts it installs and refuses a tree from a different
render, so an installer and a scaffold dir regenerated apart will stop rather
than install a mismatched pair. That refusal is the feature; the fix for it is
always to re-run `fraisier scaffold` so both are regenerated together.

Two things will look new on the first run:

- `fraisier setup` on a multi-host box that matches no declared host now
  **errors** instead of provisioning every environment (#331). The message
  names the machine, the hosts the config knows, and the three ways forward.
  Single-host configs — where no environment declares a `server:` — are
  unaffected.
- `install.sh` ends with a coverage report: units owned by
  `fraisier scheduled-install`, and artifacts that are rendered and installed
  by nothing. Both sections are informational. The second lists real gaps that
  predate this release; they are now tracked rather than silent.

## [0.56.0] - 2026-08-03

Closes #325. **The webhook unit installed on a machine was not the one
rendered for it.** Reported from production (printoptim.io, 2026-08-03): a
prod-only host running the unit built for the dev host, so
`ProtectSystem=strict` denied every write to the production bare repo and
`git fetch` aborted with exit 255.

### What broke

The per-server filter was never wrong. `_build_context(config, server)`
produced exactly the right path set. What was broken is that nothing
guaranteed the file carrying that set was the file that got installed.

The deploy path regenerates the tree with `fraisier scaffold --output-dir
<state_dir>` and **no `--server`**, which takes the auto-per-server branch and
writes only slugged `fraisier-{project}-webhook-{slug}.service` files. All
three installers — the generated `install.sh`, `ServerSetup._plan_webhook_service`
and `ScaffoldRenderer.get_install_mapping` — asked for the *unslugged*
`fraisier-{project}-webhook.service`, a name that render never produced.
`install.sh` guarded its copy with `if [ -f ]`, so the step was **silently
skipped**, or silently copied whatever unslugged file survived in the state
dir from an earlier, differently-filtered render — content frozen from
whichever server context last wrote it.

Two more routes reached the same failure, both fixed here:

- `_collect_unique_servers` read only the global `environments:` section while
  the installer's host gating read the per-fraise configs too. A config
  declaring `server:` only under `fraises.*` looked server-less to the
  renderer — one unit carrying **every** host's trees, the #62
  least-privilege leak by a second route.
- An environment declaring no `server:` in a multi-server config matched no
  logical server, so its `git_repo`/`app_path` were rendered into **no** unit
  at all.

### Why nothing caught it

Every operator-side check passes: the paths exist, are owned correctly and are
writable from a login shell. Only a write attempted from *inside* the unit's
sandbox reveals it, and nothing ever attempted one. `fraisier doctor`'s #317
check compared `ReadWritePaths=` against dump directories only.

### The new contract

> When any environment declares a `server:`, the scaffold tree contains
> **only** slugged `fraisier-{project}-webhook-{slug}.service` files, and the
> installer resolves the slug from the machine hostname. When no environment
> declares a `server:`, the tree contains the single unslugged file. There is
> no fallback from the first mode to the second.

The **destination** unit name is unchanged — only the source filename inside
the scaffold tree carries the host. No unit rename, no `systemctl
disable`/`enable` dance, no `[Install]` change. `install.sh` and the units
ship from the same render by the same version, so there is no version skew.

### Fixed

- **The webhook unit is addressed by host and selected at install time**
  (`scaffold/renderer.py`, `templates/core/install.sh.j2`, #325). `install.sh`
  bakes `_FRAISIER_MACHINE_WEBHOOK` alongside the `machine_env_map` it already
  had, and copies the unit matching `hostname -s`. The `if [ -f ]` guard is
  gone along with the comment asserting that "the bootstrap renderer is always
  called with `--server`" — the false premise the whole bug rested on.
- **One resolver for "which environments are local"** (`config/loader.py`).
  `FraisierConfig.iter_environment_servers()` is the single walk over both
  declaration sites; `declared_servers()`, `get_environments_for_server()` and
  therefore `get_machine_environment_map()` all derive from it.
- **`ServerSetup` no longer drops its server** (`setup.py`). It built
  `ScaffoldRenderer(config)` with no filter while `_resolve_allowed_environments`
  auto-detected the host for the plan, so the plan and the tree could disagree.
- **`get_install_mapping` and `_plan_webhook_service` resolve the same source
  as `install.sh`** via one shared `local_webhook_source`, so `scaffold-diff`
  stops reporting a phantom missing file.
- **Stale webhook units are swept from the tree** on an unfiltered render —
  both a slugged unit for a server dropped from the config and the legacy
  unslugged file. A `--server` render deliberately sweeps nothing: it holds
  one host's share of the truth.
- **A failed regeneration reports stderr** (`deployers/base.py`). It reported
  stdout only, which after this change would show the operator the successful
  part of a refused render and no reason.

### A render now refuses rather than narrowing

Three conditions abort the render instead of emitting a smaller unit. A
non-zero scaffold exit already becomes a `DeploymentError`, so a deploy that
cannot render a correct unit stops *before* the install step.

- `--server` naming a server no environment declares. This previously rendered
  a unit with the fraisier state directories and no application paths —
  installable, and then broken on every deploy.
- An environment that declares no `server:` while others do. Rejected rather
  than treated as hosted everywhere: "everywhere" re-creates the #62 leak by
  default and makes the permissive reading of a half-migrated config the
  safe-looking one. The error names the environment and the servers available.
- A rendered `ProtectSystem=strict` unit missing a `git_repo`/`app_path` of an
  environment its host runs. Checked against the rendered text, so it holds
  however the filtering was reached.

### Added

- **Deploy-start sandbox probe** (`deployers/api.py`). The deploy already runs
  inside the sandbox, so it creates and unlinks a file in each of this
  environment's `git_repo` and `app_path` before the first git operation — a
  real write, not `os.access`, which answers a different question. The
  reported failure surfaced as `git fetch` exit 255; it now surfaces as a
  diagnosis naming the path, the unit and the remedy.
- **`doctor` check `webhook_hosted_trees_writable`** — the #317 check's shape
  widened from dump directories to every hosted environment's trees. Reads the
  **installed** unit, so it catches the upgrade-without-re-scaffold case and
  hand-written units. A `warn`, matching #317: a hard failure would break hosts
  limping along on a hand-edited unit that works.
- **`fraisier doctor --probe-sandbox`** — opt-in active probe that spawns a
  transient `ProtectSystem=strict` unit over the *rendered* `ReadWritePaths=`
  and writes into each path, for checking a host before installing. Needs
  root; skipped cleanly without it.

### Conditional design decision

`_regenerate_scaffold` still renders **unfiltered**, deliberately: the tree is
host-independent and selection happens at install, so one tree stays valid for
every machine, which is what the state dir is for.

That is safe **only** under two invariants, and not otherwise. **(M)** mode is
a function of the config alone — `--server` narrows which slugs a render
emits, never the mode — so an unfiltered regen of a multi-server config emits
every slug and no host-agnostic unit. **(N)** the installer never falls back
to an unslugged leftover. Together, the all-paths unit that would re-create
the #62 leak is never written *and* never installed. **If (M) or (N) is ever
relaxed, `_regenerate_scaffold` must start passing `--server` in the same
commit.** `TestModeIsAFunctionOfTheConfigAlone` is the tripwire.

### Two deliberate test-contract changes

Both in `TestWebhookServerFiltering`, both carrying the rationale in their
docstrings so a later reader does not take the diff for a regression:

- `test_webhook_includes_only_local_server_paths` now reads the **slugged**
  file. It used to assert that a `--server` render writes the host-agnostic
  name with host-filtered content — that pairing is the bug. The filter it
  pins is unchanged and still correct; only the filename moved.
- `test_webhook_server_with_no_matching_environments` is replaced by
  `..._is_an_error`. The old assertion — that an unknown `--server` renders a
  pathless unit silently, with exit 0 — *was* the behaviour it pinned.

### Notes for operators

- **Re-scaffold and re-install on every machine**: `fraisier scaffold && sudo
  fraisier scaffold-install --yes`. Until you do, the installed unit is
  whatever is there now.
- **Add `server:` to every environment** if any environment has one. A render
  refuses otherwise, naming what to add.
- **A `prod-paths.conf` drop-in added as a workaround can be removed** after
  upgrading and re-scaffolding. Leaving it is harmless — a drop-in's
  `ReadWritePaths=` adds to the unit's list rather than replacing it.
- **`fraisier setup` on a machine registered under no logical server** skips
  the webhook install with a warning instead of copying a file that no render
  wrote. Pass `--server` there.
- Nothing changes for a genuinely single-server config: the tree still holds
  one unslugged unit.

## [0.55.0] - 2026-08-01

Closes #321. **Automatic rollback has never run in fraisier's own deployment
model.** Reported by a beta tester from journal evidence — every successful
webhook deploy logged `(None -> sha)` — and reproduced here.

### Fixed

- **`get_worktree_sha` now reads the SHA that is actually deployed** (`git/operations.py`, #321). It ran `git -C <worktree> rev-parse HEAD`, but the bare-repo + worktree layout leaves the worktree with **no `.git` directory** — which is exactly why `fetch_and_checkout` passes `--git-dir/--work-tree`. The call failed with `fatal: not a git repository`, was caught, and returned `None` on **every** deploy rather than only the first, as its docstring claimed.

### ⚠️ What that meant

`_previous_sha` is assigned from that function and nothing else. With it permanently `None`:

- **`_restore_previous_state` returned immediately** — no git revert, no service restart. A failed deploy left the worktree and venv **ahead of the database**.
- The database rollback never received a target.
- `rollback()` and the deploy-timeout rollback path were no-ops.
- `get_current_version()` had the same flaw, so deploy results always reported `old_version: null`.

Status reporting was *honest* throughout — with nothing restored, v0.51.0's classifier correctly reported `FAILED` rather than `ROLLED_BACK` — which is why this never surfaced as a wrong-status bug. The safety net accurately reported doing nothing.

### Notes for operators

- **Rollback now actually happens on a failed deploy.** If you have been relying on manual recovery, expect the deploy to revert the worktree and restart the service on the previous commit by itself.
- **`old_version` starts being populated** in deploy results, notifications and the status file, where it was previously always null.
- Nothing to re-scaffold: this is package code, not a generated unit.

### Known limitations

- **A genuinely first deploy still has no previous SHA**, so there is still nothing to roll back to — correctly reported as `FAILED`.
- **Fixed for the bare-repo + worktree model and the plain-clone fallback**; a layout that is neither still returns `None`.
- **This does not change what rollback *does*** — only that it now runs. The database half remains governed by the strategy, and a git-only revert still reports `ROLLED_BACK` per v0.51.0.

## [0.54.0] - 2026-08-01

Closes #317 and #318. #317 is the important one: **the v0.52.0 pre-migration
dump gate could never succeed on a standard install**, so every deploy with
pending migrations failed closed.

Absorbs v0.53.1, which was merged but never published.

### Fixed

- **The dump gate's `output_dir` is now writable from the webhook unit's sandbox** (`core/fraisier-webhook.service.j2`, #317). The generated unit runs `ProtectSystem=strict` with a `ReadWritePaths=` list covering only the fraisier state dirs and the app/git trees. `database.pre_migrate_dump.output_dir` was never propagated into it, so `pg_dump` failed with `Read-only file system` and the gate — correctly — aborted the deploy. On any strict install, which is every scaffold-generated unit, the feature was a guaranteed deploy blocker on first use. Hit in production on printoptim.io, 2026-08-01.
- **`fraisier bootstrap` now uploads `scaffold.template_dir`** (`bootstrap.py`, #318). `_upload_config` uploaded exactly one file, so a freshly bootstrapped host had a `fraises.yaml` whose relative `template_dir` pointed at a directory nothing had created — the same single-file omission #312 fixed on the deploy path, at provisioning time instead. It now uploads the tree alongside the config, replacing it wholesale so a template deleted upstream cannot survive and keep shadowing a built-in.

### Added

- **`doctor` check `pre_migrate_dump_writable`** (#317). Reads the **installed** webhook unit — not the freshly rendered one — and warns when it is `ProtectSystem=strict` but does not allow writes to a configured dump directory. That catches the upgrade-without-re-scaffold case, which is the likeliest way to still be broken after this fix. Advisory: `warn`, never `fail`.

### Notes for operators

- **Re-run `fraisier scaffold && sudo fraisier scaffold-install --yes`.** The fix is in a generated unit; upgrading the package alone changes nothing. `fraisier doctor` will tell you if you have not.
- **Nothing was silently wrong.** The gate did its job — migrations were not applied, the service was not restarted, the database was untouched. The failure mode was a blocked deploy, not a corrupted one.
- **The config key is `output_dir`**, not `dir`. The issue text says `dir`; the code has always read `output_dir` (`strategies/_core.py`).
- **`fraisier bootstrap` now uploads `scaffold.template_dir`** (#318) — see the note below, carried from the unpublished v0.53.1.
- **`--dry-run` names the template upload** in its plan, and an absolute `template_dir` is still never uploaded.

### Known limitations

- **Only the webhook unit's sandbox was widened.** If you run the deploy through a different unit, or carry a hand-written one, it needs the same `ReadWritePaths=` entry — the doctor check reads the generated unit's path, so a custom unit is not inspected.
- **The doctor check does not attempt a real write.** It compares the configured directory against the unit's allowlist textually; it will not catch a directory that is allowlisted but unwritable for some other reason (ownership, full filesystem, read-only mount).
- **The template-dir upload is best-effort** (#318): a failure is logged loudly but does not abort provisioning, so a host can still end up on built-in templates.
- **`fraisier setup` is unaffected** by #318 — it renders and installs locally and never populates `/opt/fraisier`.

## [0.53.0] - 2026-07-31

Closes #310, #311 and #312 — all three found by running fraisier in anger on
printoptim.dev, and all three of the same shape: **something silently did not
happen, and nothing said so.**

Two of them are the halves of one incident. On 2026-07-30 at 00:00 UTC the
staging-restore timer fired **on top of an in-flight deploy**, stopped the API
service and terminated every connection to the staging database. The deploy's
`pg_restore` died with `FATAL: terminating connection due to administrator
command`, the deploy reported FAILED, and staging was left half-restored. Two
independent defects had to line up for that: the timer fired at an hour nobody
had scheduled, and nothing stopped a restore running concurrently with a deploy.

### Fixed

- **`db restore` now holds the deployment lock** (`cli/db.py`, #310). `fraisier.locking` has always provided per-fraise mutual exclusion and the webhook wraps every deployment in `deployment_lock(fraise)` — but the `db restore` CLI never acquired it, so a timer-, cron- or hand-driven restore ran completely unsynchronised with deploys of the same fraise. It now takes the same lock, across the whole live restore.
- **The staging-restore timer no longer fires twice a day** (`core/restore-staging.timer.j2`, #311). The template carried both `OnCalendar=daily` and `OnCalendar=*-*-* 02:00:00` under a comment promising 2 AM. `OnCalendar=` **accumulates** — per `systemd.timer(5)` the trigger list resets only when the *empty* string is assigned — so the unit also fired at 00:00. Confirmed live: `systemctl list-timers` showed LAST 02:00 / NEXT 00:00 on the same unit, which a single-trigger timer cannot do.
- **`scaffold.template_dir` now reaches the server** (`deployers/base.py`, `scaffold/renderer.py`, `config_watcher.py`, #312). A project could override a built-in template, watch it render correctly with `fraisier scaffold`, and get the built-in on the deployed host with nothing said. Three causes stacked: a relative `template_dir` resolves against the *config* directory (`/opt/fraisier`) rather than the app checkout; config sync copied only `fraises.yaml`, so the template tree never left the repo; and `ConfigWatcher` hashed only `fraises.yaml`, so a template-only commit never triggered regeneration at all. Verified live, where the customised file was **`sudoers.j2`** — an operator believing a privilege rule was deployed when it was not.

### Added

- **`fraisier db restore --skip-if-locked`** — exit 0 with a clear line instead of failing when a deploy holds the lock. The generated `restore-staging.service` passes it: a concurrent staging deploy is itself restoring from production, so a collision means the work is already being done. Without this the new lock would convert a harmless overlap into a nightly failed unit.

### Changed

- **A missing `template_dir` is now a warning, not silence** (#312). `jinja2.ChoiceLoader` falls through to the built-ins when the configured directory is absent — no exception, no log line. Fraisier now warns and names the **resolved** path, since the relative resolution is the trap. Deliberately not fatal: deploys currently running on built-ins must not start failing on upgrade.
- **Change detection covers the template tree** (#312). Paths are hashed alongside contents and sorted, so the digest is order-independent and an edit, an addition or a **rename** all count as a change.

### Notes for operators

- **Re-run `fraisier scaffold && sudo fraisier scaffold-install --yes`, then deploy once.** #310 and #311 live in generated units; #312's template sync happens during deploy-time config sync. Until both, hosts keep the old behaviour.
- If you applied the `flock` drop-in from #310 as an interim mitigation, it can be removed — it is harmless if left.
- **`--dry-run` does not take the lock.** It mutates nothing, so a running deploy will not veto a plan preview.
- **A lock that cannot be evaluated is an error, not a skip.** If the lock directory is missing or unwritable, `db restore` fails with a message naming it — even under `--skip-if-locked`. "I cannot tell whether a deploy is running" must never be treated as "no deploy is running". `/run/fraisier` is tmpfs, created by the webhook unit's `RuntimeDirectory=`; `restore-staging.service` already depended on it for its systemctl socket, so this is not a new requirement.
- **The template sync replaces the directory wholesale.** A template deleted in your repo will not survive on the server — the point, since a stale override would otherwise shadow the built-in indefinitely. An **absolute** `template_dir` is never synced; that names a location you manage yourself. If you have been hand-placing templates under `/opt/fraisier/`, move your source of truth into the repo.

### Known limitations

- **Only `db restore` was given the lock.** The other `db` subcommands (`exec`, `reset`, `migrate`, `build`) still run unsynchronised. `restore` is the one that stops the service and terminates connections, so it is the one that destroyed a deploy — but the same class of collision is reachable through `db reset`, and closing that is a separate change with its own blast radius.
- **The lock is per-fraise and per-host.** Cross-host coordination still requires the `database` lock backend, unchanged here.
- **Nothing recreates `/run/fraisier` for a timer firing while the webhook is stopped.** The failure is now legible rather than a traceback, but it is still a failure.
- **Custom templates come from whichever fraise deployed last.** `template_dir` is project-level while `app_path` is per-fraise-per-environment, so in a multi-repo project the last deploy wins. That is exactly how `fraises.yaml` syncing already behaves; this inherits the wart rather than introducing it. Re-anchoring resolution at the app checkout was considered and rejected for the same reason.
- **The template sync is best-effort.** A failure is logged loudly but does not abort an otherwise-successful deploy, so a host can still end up on built-ins; the new render-time warning is what surfaces that.
- **Nothing validates that a custom template is still compatible.** Overriding a built-in that later gains a context variable fails at render time under `StrictUndefined` — on the server, mid-deploy.

## [0.52.0] - 2026-07-31

Adds an enforced pre-migration backup gate for production deploys, plus a
`doctor` check for a start-time regression that is easy to miss.

### Added

- **Verified pre-migration dump gate for the `migrate` strategy** (`database.pre_migrate_dump`). When enabled and migrations are pending, `MigrateStrategy` takes a `pg_dump` of the target database and verifies it (`pg_restore --list` TOC read + size-sanity against the previous dump, reusing the backup runner's truncation guards) **before** applying anything. A dump that cannot be produced or fails verification aborts the deploy with the schema untouched — *no fresh verified dump ⇒ no migration* — bounding production RPO to the moment immediately before the deploy instead of the last scheduled backup. Config: `enabled`, `output_dir` (required), `compression` (default `zstd:9`), `jobs`, `min_free_gb`, `retention_hours` (pruning runs only after a successful dump). No-op deploys (nothing pending) skip the dump. Note: the pre-existing `backup_before_deploy` key only ever affected the dry-run display; this gate is the enforced replacement.
- **`doctor` warns when a `uv sync` install command omits `--compile-bytecode`** (#298, #307). Since v0.50.1 the app unit runs with `PYTHONDONTWRITEBYTECODE=1`, so an install that lays down no bytecode cache pays it back at every start — measured at ~434 ms on a 49 MB site-packages app. The two settings compose (the env var disables bytecode *writing*, not reading), so an install-time cache is still used at runtime, and those install-user-owned caches are excluded from the v0.51.0 `__pycache__` sweep. `doctor` now points this out instead of leaving it to be discovered as a slow start.

### Internal

- Type-check floor driven to zero and CI made to hold it there (#309); pre-commit aligned on ruff 0.16.0 across every hook that gates the repo (#308); install-helper crash-logging test no longer depends on how the suite is invoked (#306).

## [0.51.0] - 2026-07-29

Four fixes that had been carried as separate branches, released together.
Closes #284, #294, #296, #303. **Read the compatibility note first** — #296
changes the status reported by nearly every failed deployment.

### ⚠️ Compatibility — deployment status

A failed deploy that reverted the working tree and restarted the service now reports **`rolled_back`** where it previously reported **`failed`** (#296). Most deploy failures on a box with history fall into this case: the automatic restore git-reverts on *every* failure that has a previous SHA, migrations or not.

- **Alerting that tests `state == "failed"` will stop seeing most failed deploys.** Test membership in `status.FAILURE_STATES` (`{failed, rolled_back, rollback_failed}`) instead — that set exists for exactly this reason and has since v0.49.0.
- **Deployment-history stats shift too.** A `rolled_back` result is mapped onto `mark_deployment_rolled_back`, so `fraisier ops` shows these under "Rolled back" rather than "Failed". The deploy is still recorded as unsuccessful.
- Nothing inside fraisier needed changing: `cli/_info.py`, `cli/ops.py`, `notifications/base.py` and the webhook already handle `rolled_back`.

### Fixed

- **The generated sudoers rule now authorises the command the deploy actually runs** (`core/sudoers.j2`, #294). Every rule was rendered as `NOPASSWD: <cmd> *`, but the deploy invokes `sudo -H -u <install_user> <install.command>` and appends nothing. sudo requires a trailing ` *` to match **at least one** further argument, so the rule never matched: the `sudo -u` install fallback has never been authorised for any project, and would have prompted for a password and failed non-interactively. Settled against sudo's own policy engine (1.9.17p2) with `sudo -l`, which checks the policy without executing — the real invocation reported NO MATCH while a control with a trailing argument matched. The wildcard is gone.
- **`fraisier setup` now populates `scaffold.state_dir`** (`setup.py`, #284). After a bare `setup`, `{state_dir}/install.sh` did not exist. That exact path is baked into the scaffold-install-helper unit as its `ExecStart` argument, and the helper daemon exits 1 at startup when it is missing — so the socket never came up and every deploy silently fell back to the slower subprocess install path, until the first config-changing deploy regenerated the tree. Same silent-degradation class #283 closed for `bootstrap`, narrowed to the manual-`setup`-without-a-deploy window.
- **Stale `__pycache__` sweeps now match any foreign owner, not just root** (`core/install.sh.j2`, #303). v0.48.0 widened the owner filter for the *new* uv-tool sweep only; the app-venv and `~/.local/lib` sweeps kept `-user root`. Residue written by `service.user` — an identity independent of both `deploy_user` and `install.user`, i.e. exactly the #292 failure class — therefore survived every sweep. The app-venv sweep now targets the identity that actually owns that venv (`install.user`, falling back to `deploy_user`).
- **The `__pycache__` remediation advice no longer hands you a command that cannot work** (`deployers/mixins.py`, `install_helper.py`, #303). Both `Permission denied` advice strings hardcoded `find … -user root`. The writer can be `service.user` (#292) or `install.user` (#286) — neither is root — so an operator hitting this exact error was told to run something that would match nothing. Both now resolve the venv's owner at paste time with `stat -c %U`.

### Changed

- **A git-only revert reports `ROLLED_BACK`** (`deployers/api.py`, #296). It is what actually happened: the tree is back on the previous commit and the service is running it. Reporting `FAILED` left an operator unable to tell that from an undefined half-deployed state — the same ambiguity #293 closed on the database axis, on the axis #293 deliberately left open. The status message names what was restored and does not mention the database when no migrations ran.
- `fraisier setup --dry-run` lists two new actions under a `scaffold` category: the copy into `state_dir` and the recursive chown of the result. No existing action changed.
- `/var/lib/fraisier/<project>/scaffold` is now created and owned by `deploy_user` by `setup`, matching what `bootstrap` already did.

### Docs

- **The two provisioning flows differ on purpose, and that is now written down** (`docs/deployment-guide.md`, `docs/cli-reference.md`, #284). A table contrasting `output_dir` (what you render and review) with `state_dir` (what the machine reads at deploy time), why `setup` writes both while `bootstrap` writes only the second, and a pointer from `setup` to `bootstrap` for fresh servers.

### Corrected

- **v0.50.1's release notes claimed existing `__pycache__` residue was already cleared by the sweep.** It was not, for the residue that entry is about. The v0.50.1 entry below now carries a correction pointing here. On v0.50.1 that residue must be cleared by hand:
  ```sh
  sudo find <app_path>/.venv -name __pycache__ ! -user "$(stat -c %U <app_path>/.venv)" -type d -exec rm -rf {} +
  ```

### Notes for operators

- **Re-run `fraisier scaffold && sudo fraisier scaffold-install --yes`** on every host. This release changes three generated artifacts — the sudoers fragment, `install.sh`'s sweeps, and (via `setup`) the scaffold state tree. Until you do, the installed copies keep the old behaviour.
- **The sudoers change is a tightening.** The rule now authorises exactly the configured `install.command` and nothing else. If you ran `sudo -u <user> uv sync --frozen --offline` by hand and relied on the fragment, that stops working — deliberately. Nothing automated did.
- **`fraisier setup` still installs from `output_dir`.** The render → `git diff` → install loop that makes it the manual flow is unchanged; `state_dir` is added as what the machine consumes, not substituted as what you install from.
- **The webhook env file is deliberately excluded from the `state_dir` copy.** It carries `FRAISIER_WEBHOOK_SECRET`, its only install target is `/etc/fraisier/<project>.webhook.env` at mode `0640`, and `state_dir` is world-readable.
- **A revert that itself failed still reports `FAILED`, deliberately.** `ROLLBACK_FAILED` means "the schema may be half-migrated, do not restart the service"; that is false when no migrations ever ran.

### Known limitations

- **#294 was verified on sudo 1.9.17p2 only.** The behaviour relied on is documented shell-style wildcard semantics, not a version quirk, but the probe answered for one version. A second, permissive rule was deliberately **not** rendered: it would authorise arbitrary extra arguments under NOPASSWD, for a caller that does not exist.
- **`setup` still installs from `output_dir`, so the trees can diverge** (#284). Hand-edit something under `state_dir` and the next `setup` overwrites it. The copy is also unconditional, not a sync — files present in `state_dir` with no counterpart in `output_dir` survive it, and nothing prunes `state_dir`.
- **Only the automatic post-failure restore changed** (#296). The explicit `rollback()` entry point already reported `ROLLED_BACK` in `ETLDeployer` and `ScheduledDeployer`; the timeout path builds its own result and is untouched. `fraisier status` still cannot distinguish a database rollback from a git-only revert — both write `rolled_back`, and only the message differs.
- **The sweep clears residue; it does not stop every writer** (#303). A process invoked *outside* systemd (a manual `sudo -u <user> …` while debugging) is still unconstrained — the leading candidate for #286's residue, and why #286 closed without a code cause being found. The sweep also runs at `scaffold-install` time only.

## [0.50.1] - 2026-07-28

Closes #292. Found while re-auditing the unit templates for #286 — not a cause
of #286, whose residue is elsewhere.

### Fixed

- **The app unit no longer byte-compiles into its own venv** (`core/service.j2`, #292). It was the only Python-invoking unit template without `Environment=PYTHONDONTWRITEBYTECODE=1`; the other eight all set it. It is also the one unit whose `User=` can be a **third** identity — `service.user`, independent of both `scaffold.deploy_user` and `install.user` — so the running app wrote `__pycache__` under `app_path/.venv/` owned by an identity the install user cannot unlink on the next `uv sync --frozen`. That is the #196 failure class from a direction #196 did not cover.

### Notes for operators

- **This costs startup time, deliberately.** `--compile-bytecode` is used nowhere, so the venv has never been precompiled at install time; with bytecode writing off, every start recompiles site-packages *and* your app modules. On a `Restart=on-failure` unit that starts rarely this is the right trade against an un-cleanable venv, but it is a real cost on a large codebase.
- **It is overridable.** The directive is emitted *before* the `service.environment` loop, and systemd resolves a repeated `Environment=` assignment last-wins, so `service.environment: {PYTHONDONTWRITEBYTECODE: "0"}` restores the old behaviour if you measure the cost and decide against it.
- Re-run `fraisier scaffold` and reinstall the unit to pick this up; an already-running service keeps its current environment until restarted. Existing `__pycache__` residue is cleared by the stale-cache sweep at `scaffold-install` time, as it was in v0.48.0.
  - **Correction (v0.51.0, #303):** that last sentence was wrong. The app-venv sweep filtered `-user root` only, so `service.user`-owned residue — exactly what this entry is about — survived it. Fixed in v0.51.0; on v0.50.1 the residue must be cleared by hand.

### Known limitations

- **Redirecting the cache rather than disabling it is not implemented.** `PYTHONPYCACHEPREFIX` pointed at a `service.user`-writable directory would keep both the compile cache and the ownership guarantee, but needs a state directory, its creation and ownership in scaffold, a `ReadWritePaths` entry and an uninstall sweep. Tracked separately (#298) rather than bundled here.
- The guard added over the unit templates classifies fail-closed from `ExecStart`: a unit whose executable is a Jinja variable is assumed to be Python. Only a concretely shell `ExecStart` is exempt, and the exempt set is pinned by name, so a wrong exemption fails the suite rather than passing silently.

## [0.50.0] - 2026-07-28

Closes #290. v0.48.0 fixed the half where source *deletes* a file and named the
half it did not fix; this is that half — source *reverting* a previously
promoted change.

### Fixed

- **A source-side content revert is no longer silently undone by the pre-merge** (`cli/sync.py`, #290). When source reverts a file to exactly its merge-base content, git's 3-way merge sees `ours == base` and resolves it as *take theirs* — with a zero exit code and no conflict, so the tier loop never saw it and target's stale promoted copy won. Given a correct merge-base that resolution is right; under squash promotion the base is the ancient fork point that never advances, so `ours == base` no longer means "source never touched this" but "source added it and then reverted it". A new pre-pass restores source's version when target's copy is source-derived. This is the same stale-anchor root cause as the deletion half, reached through a different code path.
- The `--dry-run` plan describes the second pre-pass alongside the first.

### Notes for operators

- **Only the exact revert to base content was affected.** A revert to any *other* content produces a real conflict, which tier 3 has resolved since v0.48.0; and a revert in one hunk while target edits a different hunk auto-merges correctly with both changes surviving. If you worked around this by hand-deleting or hand-reverting files directly on the target branch, that is no longer needed.
- **Target-authored content is never overwritten.** When target's copy of a differing file is not source-derived, sync keeps target's version and prints a warning naming the path. Promotion silently clobbering a target-side hotfix is a worse failure than a missed revert, so the gate fails closed in that direction.

### Changed

- `_propagate_source_deletions` and the new `_propagate_source_reverts` share a `_diff_paths` helper. Both compute candidates target-side (`origin/<tgt>` vs `origin/<source>`) rather than from a merge-base, and both keep `--no-renames`, without which git's default rename detection reports a rename as `R` and hides every rename-shaped deletion.

### Known limitations

- Detection is anchored on what the merge did to the **index**, so it only applies to the pre-merge inside `fraisier sync`. A revert lost by a merge performed outside the tool is not recovered retroactively.
- `_target_blob_is_source_derived` stops after 200 commits of source history for one path and treats exhaustion as "target-authored", leaving the file alone and warning. A revert to a file with a very long history on source can therefore still need a manual decision.

## [0.49.0] - 2026-07-28

Completes the other half of #272. v0.48.0 made the automatic database rollback
*run* when a migration batch partially applies; this makes the deploy *report*
what it did, so an operator can tell a clean schema revert from a dirty one.

### Fixed

- **A deploy now reports the outcome of its automatic database rollback** (`deployers/api.py`, #293). `_restore_previous_state` returned `None` and its only caller unconditionally built `DeploymentStatus.FAILED`, so `ROLLBACK_FAILED` was reachable from exactly two sites — the health-check and timeout paths — and never from a migration failure. A deploy that rolled the schema back cleanly and one that left it half-migrated reported identically. It now returns a `RestoreOutcome` the caller honours: `ROLLED_BACK` when the DB rollback succeeded, `ROLLBACK_FAILED` when it did not.
- **The rollback incident message is no longer overwritten** (`deployers/api.py`, #293). `_rollback_database` writes the operative text — *"Rolled back N of M migrations; K still applied. Do NOT restart the service until resolved."* — to the status file, and the failure handler then clobbered it with the original deploy error. The status write is now ordered after the restore and carries the restore's own message when it produced one. The incident *file* was always correct; this is about where an operator looks first.
- **`fraisier status` no longer shows a rolled-back deploy as a green success** (`cli/_info.py`). `_compute_deployment_state` matched `deploying|pending|failed|idle|success` only, so the `rolled_back` state — written since well before this change, by both `_finalize_rollback` and the timeout path — fell through to version comparison and rendered as `deployed ✓` whenever the reverted tree happened to match the latest tag. `rolled_back` and the new `rollback_failed` are now rendered explicitly, the latter as `ROLLBACK FAILED — schema dirty`.
- **`GET /api/status/{fraise}/details` reports rollback failures instead of "No failure to report"** (`webhook.py`). The authenticated details endpoint gated its failure payload on `state != "failed"`, so it would have answered `{"message": "No failure to report"}` for the new `rollback_failed` state — on the endpoint whose entire purpose is surfacing failure detail, for the one state that means the schema may be half-migrated. It now tests membership in `status.FAILURE_STATES`. This also fixes the pre-existing case of `rolled_back`, which the endpoint has always reported as no-failure.

### Changed

- `DeploymentStatusFile.state` may now be `rollback_failed`, in addition to the `rolled_back` it could already hold. The docstring documented neither. **Branch on `status.FAILURE_STATES` rather than `state == "failed"`** — new in this release precisely because equality checks silently mis-report a dirty schema as healthy.

### Known limitations

- **A failed deploy that was only git-reverted still reports `FAILED`.** `_restore_previous_state` git-reverts and restarts the service on *every* deploy failure that has a previous SHA, not just migration failures. Promoting those to `ROLLED_BACK` would change the reported status of nearly every deployment failure and break anyone alerting on `FAILED`, so it is deliberately out of scope here. Whether it *should* change is tracked as #296, which records both sides of the argument; two tests in `tests/test_rollback_status_reporting.py` pin the current behaviour on purpose.
- `_migrations_applied` is set in `__init__` and never reset between deploys within a single deployer instance.
- A **first** deploy (no previous SHA) with a partial batch still leaves the schema dirty — `_restore_previous_state` early-returns without one. Unchanged from v0.48.0.

## [0.48.0] - 2026-07-28

Five reported issues, plus five adjacent faults found while tracing them. Three
are fresh-box provisioning defects (#287, #288, #286); the other two are silent
data-correctness bugs — a partially-applied migration batch left a production
schema dirty (#272), and `sync` resurrected deleted files on every run (#290).

### Fixed

- **A partially-applied migration batch is now rolled back** (`dbops/confiture.py`, `errors.py`, `deployers/api.py`, #272). Confiture commits each migration as it goes, so a batch that fails part-way leaves the earlier ones applied and tracked — and it reports that via `MigrateUpResult.migrations_applied` even on the failure path. `migrate_up` discarded the count when raising, and `_run_strategy` only recorded it after a *successful* return, so `_migrations_applied` stayed `0` and `_restore_previous_state` took the "nothing applied, skip DB rollback" branch. A deploy could end `FAILED` with migration N still applied, no `migrate down`, and no incident file. `MigrationError` now carries `steps_applied` — distinct from the pre-existing `step`, which is a 1-indexed position rather than a count — and the deployer reads it off the exception before re-raising. `steps_applied=None` means "unknown" and deliberately skips the DB rollback: rolling back a guessed number of migrations is worse than leaving the schema for an operator. **This widens beyond `MigrateStrategy`** — `migrate_up` is shared with `ConfitureMigrateStrategy` and `RestoreMigrateStrategy`, and `RestoreMigrateStrategy.rollback` performs a template reset / drop+create, which previously never fired on this path.
- **`sync` no longer resurrects files deleted on the source branch** (`cli/sync.py`, #290). fraisier squash-merges its own sync PRs, so `git merge-base origin/<source> origin/<tgt>` never advances past the original fork point — the promotion model keeps its own anchor stale by construction. Deletion detection was anchored on it, so a file created on source *after* that ancient base and later deleted there was invisible, while the target still carried a copy from an earlier squash-sync; it resurrected on every subsequent sync. Candidates now come from the target side (present on target, absent on source), gated on source's history actually containing the deletion and on target's blob being source-derived rather than target-authored. Two further faults surfaced while building real-repo fixtures: `git rm` needs `-f` (the pre-merge stages the file's *addition*, so a plain `git rm` refused and the deletion degraded to a warning — the feature could not remove a file even when it found one), and tier-3 conflict resolution used the same stale anchor, so it almost never fired and resolvable conflicts fell through to a hard abort.

- **Sudoers `NOPASSWD` rules now carry a fully-qualified command path** (`scaffold/renderer.py`, #287). `_resolve_command_path` was a lookup against a six-entry dict, so any other `install.command[0]` — `bash`, `sh`, `python3`, `make` — was written into the `Cmnd` position verbatim. sudoers requires an absolute path there, so `visudo` rejected the whole fragment and `scaffold-install` aborted: the systemd units, the per-fraise install-helper socket, nginx and the PostgreSQL config were all left uninstalled (the apt packages and the systemctl-helper unit install run earlier and did complete). Resolution is now `_COMMAND_PATH_MAP` → `shutil.which` → a fixed FHS search list, with the hardcoded map deliberately outranking `PATH` because `fraisier scaffold` often runs on a machine that is not the target server. A token that resolves nowhere raises at scaffold time, naming the fraise, the environment and the directories searched, instead of emitting a fragment we know the parser will reject. Note this is a regression against our own advice — `README.md` recommends `command: [bash, scripts/deploy-install.sh]` as the stable-entrypoint convention introduced in 0.46.
- **`install.sh` validates the sudoers fragment before installing it** (`scaffold/templates/core/install.sh.j2`). It previously ran `sudo install` and *then* `visudo -c -f` against the **installed** copy, so a bad fragment was left in `/etc/sudoers.d/` — which sudo treats as fatal — while printing "File was not installed." Validation now runs against the staged source, and nothing is written unless it passes.
- **Relocated install-tool cache dirs are pre-created with the right owner** (`manifest.py`, #288). The install-helper unit points `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `UV_CACHE_DIR`, `CARGO_HOME` and `npm_config_cache` at paths under `app_path`, because `ProtectSystem=strict` makes that the only writable root. But `ReadWritePaths` lifts systemd's sandbox without changing ownership, and `app_path` is `deploy_user`-owned `0755` — so on a fresh box the install user could not create them and the first `uv sync` died with `Permission denied`. All seven paths (including `.local`, whose parent would otherwise be left root-owned by `_ensure_dir`, which chowns only the leaf) are now manifest-managed and created by `scaffold-install` owned by the install user. A drift test asserts every cache path the unit exports is manifest-registered.

- **Stale-`__pycache__` cleanup now reaches the uv tool directory, and runs privileged** (`scaffold/templates/core/install.sh.j2`). The existing sweeps could not have helped on three counts: they looked under `app_path/.venv` and `/home/$DEPLOY_USER/.local/lib` but **not** `~/.local/share/uv/tools/`, where uv actually installs tools; they matched `-user root` only, missing foreign-owned residue; and they ran `rm -rf` **unprivileged**, so the deploy user could not unlink another user's `.pyc` and the failure was swallowed by `2>/dev/null || true`. All three sweeps now run under `sudo` via `_run`, and a fourth covers the uv tool dir matching any owner other than the deploy user. Bounded to `-name "__pycache__" -type d` throughout. Side effect: because the sweeps now go through `_run`, `install.sh --dry-run` and `--validate-only` no longer delete anything — previously they did. This is **not** a fix for #286, whose stated cause (a missing `PYTHONDONTWRITEBYTECODE=1` on the install-helper unit) does not reproduce — that unit has set it since v0.6.0. It makes the reported symptom self-heal at the next `scaffold-install` whatever the true cause turns out to be.

### Changed

- **Ownership reconciliation no longer deletes non-regenerable paths** (`deployers/preflight_ownership.py`). A wrong-owner manifest path was `shutil.rmtree`d so it could "be recreated" — but that premise holds only for the venv. The check runs inside `_install_dependencies`, i.e. *after* the git checkout, so a wrong-owner `app_path` deleted the freshly checked-out tree and then ran the install command in a directory that no longer existed; nothing in the deploy path recreates it. Deletion is now opt-in via `ManagedPath.reconcile_ownership`, set on the venv alone; every other path raises `DeploymentError` naming the path, the actual owner, the expected owner and the `chown` that fixes it. This affects the sudo-fallback path only (first bootstrap) — the socket path never ran this check.

### Known limitations

- A deploy whose migration batch partially applies now **rolls the schema back**, but still reports `FAILED` rather than `ROLLBACK_FAILED`, and `_write_status` overwrites the rollback message in the status file. The incident *file* is written correctly. Tracked in #293.
- A **first** deploy (no previous SHA) with a partial batch still leaves the schema dirty — `_restore_previous_state` early-returns without one.
- #290's **content-revert** half is not fixed: when source reverts a change it previously promoted, the squash topology hides that from the 3-way merge in the same way. Different mechanism, separate fix; #290 stays open.

### Note for operators

- After upgrading, `fraisier validate-remote` will report **NOT READY** on existing boxes until `scaffold-install` is re-run, because the seven relocated cache dirs are new `create_if_missing` manifest entries. Re-running `scaffold-install` creates them and clears the report.
- `_ensure_dir` chowns only the leaf directory. On a box where a relocated cache dir already exists with wrong-owned *contents* (e.g. created root-owned by an earlier run), run `chown -R <install_user> <app_path>/.cache <app_path>/.local <app_path>/.cargo <app_path>/.npm` once.

## [0.47.1] - 2026-07-25

### Fixed

- **`install.sh` no longer restarts the scaffold-install-helper's own socket when it is being run *by* that helper** (`scaffold/templates/core/install.sh.j2`, `scaffold_install_helper.py`). Follow-up to #283: now that the socket daemon is valid at startup, a config-changing deploy under `NoNewPrivileges` finally *reaches* the socket install — and hit a self-inflicted race. The helper serves a request by running the baked `install.sh` as root, and that `install.sh` unconditionally ran `systemctl restart …scaffold-install-helper.socket`, which SIGTERMs the very helper process serving the request. The client then read an empty reply (`json.loads("")` → `Expecting value: line 1 column 1`), and because the webhook runs `NoNewPrivileges=true` it could not fall back to the neutered subprocess — so every config-changing deploy aborted *before* the DB step with a misleading "install-helper allowlist could not be re-baked" error, and no re-trigger or manual re-bake could clear it. The helper now exports `FRAISIER_VIA_SCAFFOLD_INSTALL_HELPER=1` when invoking `install.sh`, and `install.sh` skips restarting its own socket under that marker (the freshly-copied unit is `daemon-reload`ed and takes effect on the next non-helper `scaffold-install` / post-upgrade restart). Manual and subprocess-fallback runs are unchanged.

## [0.47.0] - 2026-07-23

Follow-up to the 0.46 install-path work: the #279 re-bake was correct but never
reached the unit, because deploy-time scaffold regeneration rendered into the
wrong tree.

### Added

- **`scaffold.state_dir` — a single, project-level server-side scaffold tree** (default `/var/lib/fraisier/{project}/scaffold`), the source of truth every deploy-path consumer resolves against (#283). See the Fixed entry below.

### Fixed

- **Deploy-time scaffold regeneration now renders into the app tree the install step reads from** (`deployers/base.py`, #282). `scaffold.output_dir` is relative (`scripts/generated`), so it resolves against the render process's CWD — and `_regenerate_scaffold` / the `_install_scaffold` subprocess fallback ran with `cwd = config_path.parent`, i.e. the fraisier daemon's config dir (`/opt/fraisier`). But the scaffold-install-helper socket runs a baked `install.sh` that self-locates under the project's app tree (`app_path`, `= /opt/{project}`) and copies the generated units into `/etc` from there. On any box where the project isn't literally named `fraisier`, regeneration wrote the fresh install-helper unit (with the new `--deploy-user` command) into `/opt/fraisier/scripts/generated/`, while the socket install re-installed the stale unit from `app_path/scripts/generated/` — so a changed `install.command` never reached the unit and every deploy failed at the install step with `command not allowed`, with no self-service recovery (the #279 re-bake was re-baking the wrong tree). Superseded within this release by the `state_dir` unification below (#283).
- **The server-side scaffold location is now a single, explicit, project-level `state_dir`** (`config/`, `scaffold/renderer.py`, `deployers/`, `bootstrap.py`, `cli/scaffold.py`, #283). Previously the "scaffold tree" was derived three inconsistent ways — a hardcoded `/opt/{project}` baked into the scaffold-install-helper's `ExecStart`, the per-env `app_path` used by regeneration/staleness, and the CWD-relative `output_dir` — which only agreed by the coincidence `app_path == /opt/{project}`. Since every `app_path` in the examples is under `/var/www/...`, that coincidence usually failed: the helper's baked `install.sh` path pointed at a tree bootstrap never populated, so the socket daemon exited at startup and every install *silently* fell back to the subprocess path (whose `sudo` is neutered under `NoNewPrivileges`). Now `scaffold.state_dir` (default `/var/lib/fraisier/{project}/scaffold`, already writable by every `ProtectSystem=strict` deploy unit and readable by the root install helper — so no `ReadWritePaths` changes and no upgrade ordering problem) is the one location that regeneration renders into (`scaffold --output-dir`), the socket helper bakes its `install.sh` path from, the subprocess fallback installs from (`scaffold-install --output-dir`), the staleness check compares against, and **bootstrap now persists the rendered tree into** (so the helper is valid immediately, not only after the first config-changing deploy). `app_path` no longer participates in locating scaffold units; `scaffold.output_dir` reverts to a purely local render/review concern. A scaffold-install-helper socket that is present but unreachable (crashed daemon) now logs a **WARNING** instead of silently degrading. Operators who prefer `/opt/{project}` can set `scaffold.state_dir` explicitly (that path additionally requires whitelisting it in the deploy units).

## [0.46.0] - 2026-07-22

Install-path hardening. Five defects on one deploy path, all surfaced by a
single incident: a `printoptim` `install.command` change (a `bash -c` wrapper)
that failed to deploy, gave no useful error, then "succeeded but did nothing"
once patched — and, when the socket path was investigated, turned out to be
blocked by a stale allowlist under a strict sandbox.

### Fixed

- **`sudo -u` install fallback now sets `HOME` via `-H`** (`deployers/mixins.py`). Without it, `sudo` kept the invoker's `HOME` (`/root` when the deploy runs as root) and HOME-writing tools (`uv`/`pip`/`cargo`/`npm`) failed with `Permission denied` writing their caches under `/root/.cache`. The install-helper socket path was already correct — it runs as the install user via systemd `User=`, so its `HOME` is right by construction — which is why the failure only showed on the sudo fallback (#276).
- **Install-failure errors now surface the captured `stderr`** in the `DeploymentError` message (bounded to the last 2000 chars), so the real cause lands in the deploy journal instead of only in the un-rendered context dict. A generic `.cache` + `Permission denied` heuristic points at the HOME fix above; the socket path gets the same surfacing with a reworded hint (it never uses sudo). Note: `stderr` now reaches `status.error_message` and the authenticated `/api/status/<fraise>/details` endpoint — a wider (token-gated, bounded) surface than before (#277).
- **A changed `install.command` is now re-baked into the running install-helper allowlist during the deploy** (#279). The install-helper enforces an exact-match allowlist baked into its systemd unit; when the command changed, the running helper still accepted only the *previous* command and rejected the deploy with `command not allowed`. `install.sh` used `enable --now` on the socket (a no-op on a running unit), so the stale-argv service kept running — now it stops the service and restarts the socket so the next install re-execs with the new allowlist. Those re-bake steps run via a new fatal `_run_strict` (not the failure-swallowing `_run`), and a failed post-pull scaffold re-bake now **aborts the deploy loudly**, naming the stale allowlist, instead of continuing into a masked `command not allowed`. The install-helper rejection message now names the expected vs received command. The allowlist boundary is kept (request-time config validation was explicitly rejected — it would validate against attacker-writable config).
- **Install caches relocated under `app_path` so the install step works under `ProtectSystem=strict` for any toolchain** (#280). The install-helper unit was hand-fitted for `uv` only (whitelisting `~/.cache/uv`); a wrapper running anything else hit the read-only sandbox. `XDG_CACHE_HOME`/`XDG_DATA_HOME`/`XDG_STATE_HOME`/`UV_CACHE_DIR`/`CARGO_HOME`/`npm_config_cache` are now redirected under `app_path` (already writable), covering uv's managed-Python store as well as pip/cargo/npm. Recommended convention: make `install.command` a stable entrypoint (`[bash, scripts/deploy-install.sh]`) so install content lives in a reviewed repo script and the allowlist effectively never changes.

### Changed

- **The running webhook now picks up a synced `fraises.yaml` without a restart** (#278). `get_config()` cheaply `stat()`s the resolved config path on each call and rebuilds the singleton only when the mtime moves, bounding config staleness to a single deploy instead of "forever, until `systemctl restart`". A failed reload (torn, removed, or invalid file) keeps the last-good config, stamps the offending mtime so it doesn't thrash, and logs a warning — a bad config sync can no longer take down the webhook. `SIGHUP` (wired in the webhook `lifespan`, guarded so it can never crash startup) forces an immediate reload, making `systemctl reload` a supported operation (`ExecReload=/bin/kill -HUP $MAINPID` added to the webhook unit template). Config sync (`deployers/base.py`) now writes atomically (copy to a pid-unique temp file in the destination directory, then rename) so a reader never observes a torn config.

## [0.45.0] - 2026-07-21

Unifies confiture's exit-code / error-code classification onto a single, confiture-owned source of truth shared by contract with the Rust adapter (`fraisier-core`), and corrects the pre-#146 misreadings that had drifted into the Python side.

### Changed

- **Confiture exit-code classification now derives from a confiture-owned table** (`dbops/confiture_contract.py`). Confiture is the single source of truth: it owns the `(exit_int → semantic class)` table (`confiture.core.error_codes.EXIT_CODE_SEMANTIC_CLASS`, emitted by `confiture --exit-codes-json`, new in confiture 0.38). fraisier reads that table live at runtime, with a vendored copy as a fallback for an older confiture. The nine classes are `ok`, `internal_error`, `precondition_failed`, `db_unreachable`, `schema_error`, `invalid_config`, `lock_contention`, `git_error`, `irreversible_rollback`, and the three confiture-facing call sites are thin projections of it rather than re-encoding the mapping ad hoc. `tests/test_confiture_contract.py` enumerates the matrix and asserts the vendored copy matches confiture's live table; the Rust adapter (`fraisier-core`) vendors and verifies the same `confiture --exit-codes-json` output. Replaces two independently hand-maintained copies that had drifted.
- **Confiture floor bumped to `fraiseql-confiture>=0.38,<0.39`** (was `>=0.35,<0.36`), to consume the machine-readable exit-code table above. The migrate/build/preflight surface fraisier depends on is unchanged across 0.35→0.38; 0.37's `migrate verify` exit-code change is a CLI-only break fraisier's Python paths do not touch. The full suite passes against 0.38.

### Fixed

- **`_classify_exit_code` no longer encodes the pre-#146 world.** It mapped exit `2` → `validation_error` and `3` → `migration_error`, both wrong under confiture's current contract (exit `2` is a *reachable-but-uninitialised database* — no migration ledger, `PRECON_1001`; exit `3` is a *database connection failure*). It now projects the canonical table: exit `2` → `precondition_failed`, `3` → `db_unreachable`, `5` → `invalid_config`, `6` → `lock_contention`. Latent today (the `fraiseql-confiture>=0.35,<0.36` bound and a `classify_error` stderr match usually shadow it), it would have mislabelled real failures the moment either changed.
- **`confiture_status` detects an uninitialised database by error code, not the bare exit integer.** It read `returncode == 2` directly; it now branches on the canonical `precondition_failed` class (which keys on `PRECON_1001` in confiture's `--format json` error envelope), so the "tracking table absent" signal is recognised by confiture's own code rather than by an integer that only *happens* to coincide.
- **The preflight fatal-exit path no longer mislabels exit `2` as "validation".** The guard's comment claimed exit `2` was a validation error; it is the no-ledger precondition. The comment is corrected to confiture's real meanings, and `_format_preflight_failure` now names the failure by its canonical class (e.g. "database unreachable", "no migration ledger") instead of a bare `exit N`.

## [0.44.0] - 2026-07-16

Closes the standalone-restore tail of the ACL-stripping bug first fixed for the deploy path in printoptim_backend#1681: `fraisier db restore` now re-applies the configured grant scripts, so a database refreshed outside a full deploy is no longer left grantless.

### Fixed

- **`fraisier db restore <fraise> <env>` now re-applies the configured `database.post_migrate` grant scripts after a successful restore** ([#273](https://github.com/fraiseql/fraisier/issues/273)). The `restore_migrate` strategy restores with `pg_restore --no-owner --no-acl` and, by design, applies no grants — re-applying `database.post_migrate` was the deployer's responsibility (`_run_post_migrate`). The webhook deploy path ran it; the standalone `db restore` CLI called `strategy.execute(...)` and returned on success **without** it. Any environment refreshed via the CLI — e.g. a nightly "restore staging from production backup" timer — therefore came back with every non-`public` schema stripped of `USAGE`, and any application/ETL/reporting role that isn't the schema owner could no longer connect. Real incident: a staging DB refreshed nightly by `restore-staging-from-production-backup.timer` wiped `printoptim_etl`'s grants every night at 00:00 UTC, yielding ~615 failed ETL runs over 7 days (`permission denied for schema tenant`) — each restore re-wiping the grants a human had manually replayed. `db_restore` now runs the post_migrate hooks after a successful restore and exits non-zero if a `halt` step fails.

### Changed

- **`post_migrate` load-and-run is now a single shared seam.** `deployers/api.py::_run_post_migrate` and the `db restore` CLI both delegate to `post_migrate.run_configured_post_migrate(database_config, *, app_path, runner)`, which resolves `database_url`, loads the configured steps, and runs them — a no-op when there is no `database_url` or no configured steps, and a `halt` step still raises `DeploymentError`. This keeps the deploy and CLI paths from drifting rather than duplicating the load-and-run logic in two places.

## [0.43.0] - 2026-07-07

Continues the confiture 0.33 migration shipped in 0.41.0: adopts fraiseql-confiture 0.35 and — the substantive change — moves the staging restore off a bare `pg_restore` onto confiture's three-phase `DatabaseRestorer`, so the matview-refresh fix (confiture #172) actually reaches fraisier's `restore_migrate` strategy.

### Changed

- **Confiture integration bumped to fraiseql-confiture 0.35.x** (pinned `>=0.35.0,<0.36`, was `>=0.33.0,<0.34`). The migrate/build/preflight surface fraisier targets is unchanged across 0.33→0.35 (`Migrator.from_config` → `MigratorSession.up/down/status/preflight`; the preflight `--against` `{ok, summary, issues[]}` JSON + exit `0`/`7` contract); 0.34 additionally defaults `Environment.name`/`include_dirs` so a migrate-only config validates directly, and 0.35 carries the restore matview fix adopted below. The upper bound still prevents silent drift onto a future schema change ([#262](https://github.com/fraiseql/fraisier/issues/262)).
- **Staging restore now runs through confiture's three-phase `DatabaseRestorer` instead of a single `pg_restore`** ([#270](https://github.com/fraiseql/fraisier/issues/270)). `dbops/restore.restore_backup` previously shelled out to `pg_restore -d <db> --no-owner --no-acl [-j N] <dump>` directly — so confiture's restorer, and its matview fix, never touched fraisier's restore path. A backup carrying a stats-sensitive materialized view then refreshed it **inside the parallel data phase on the empty `pg_statistic` of a freshly loaded database**, replanning into a catastrophic nested loop (25 min – 2h+) that hung the deploy and left staging unusable (the printoptim_backend#1960 incident: 2h+ on `REFRESH MATERIALIZED VIEW mv_maintenance_price` under `--jobs 4`). `restore_backup` now delegates to `confiture.core.restorer.DatabaseRestorer`, which holds every matview `REFRESH` out of the restore phases, runs a database-wide `ANALYZE`, then refreshes on real statistics (confiture #172). It maps the caller's `connection_url` to confiture's discrete `-h/-p/-U` (netloc **or** socket `?host=` form) with any password threaded through as `PGPASSWORD`, keeps the post-restore `REASSIGN OWNED` ownership fix and the identifier/path validation, sets `parallel_restore` for `jobs > 1`, and leaves confiture's own min-tables check off (the strategy validates the table count itself after `migrate up`). A confiture `RestoreError` (e.g. an unsupported plain-SQL dump) is returned as a failed `RestoreResult` rather than raised, so the strategy's `.success` branch is unchanged.

### Added

- **The restore phase breakdown now surfaces confiture's deferred-matview accounting.** `RestoreResult` carries `matviews_deferred` / `matviews_refreshed` / `analyze_ran` (`None`/`False` when the backup had no matviews), and `RestoreMigrateStrategy` logs them after the restore step so a deploy log shows when a refresh was held past `ANALYZE` ([#270](https://github.com/fraiseql/fraisier/issues/270), ask #3).
- **Real-PostgreSQL end-to-end regression for the deferred refresh.** `tests/integration/test_restore_matview_deferral.py` dumps a database carrying a populated matview, restores it through `restore_backup`, and asserts the matview comes out **populated** (not left `WITH NO DATA`) for both the serial (`jobs=1`) and parallel (`jobs=4`, the incident condition) paths — proving confiture and fraisier run together, not just that fraisier passes the right options. Existing restore unit tests were migrated onto the `DatabaseRestorer` seam.

## [0.42.0] - 2026-07-06

### Fixed

- **`fraisier sync` no longer aborts with the generic "HEAD is not a merge commit … this is a fraisier bug" message for states that are operator-fixable or outright safe** ([#268](https://github.com/fraiseql/fraisier/issues/268)). Two distinct triggers produced that abort, both with git's actual diagnostics swallowed by `capture_output`:
  - **`git merge` exiting non-zero without starting a merge** (e.g. `error: The following untracked working tree files would be overwritten by merge`). Sync treated every non-zero merge exit as "conflicts to resolve", found no unmerged paths, silently skipped the commit (nothing staged, no `MERGE_HEAD`), and tripped the pre-push guard with a single-parent HEAD — while the operator never saw git's stderr explaining the real problem. This failure class is now detected (non-zero exit + no unmerged paths + no `MERGE_HEAD`) and aborts immediately, printing git's own output verbatim plus remediation hints.
  - **A target strictly behind the source.** The pre-merge answers "Already up to date": no merge commit exists or is needed, and the push is safe (`origin/<target>` is already an ancestor of the sync head) — but the guard's `parents >= 2` assertion rejected it with the same misleading message.

### Changed

- **The pre-push guard now asserts the actual push-safety invariant** — `origin/<target>` must be an ancestor of HEAD and `MERGE_HEAD` must be cleared (the exact condition under which GitHub will not mark the sync PR `CONFLICTING`) — instead of requiring a two-parent HEAD. Its abort output now includes a HEAD summary, parent SHAs, and a `git status --porcelain` snapshot, so "please file an issue with the output above" ships with usable output.
- **`fraisier sync` now requires a clean working tree** and aborts pre-flight with the offending file list otherwise. `git checkout -B` carries uncommitted modifications onto the sync branch (where the pre-merge commit would silently sweep them into the sync PR), and untracked files can abort the merge mid-flow (#268's first trigger). fraisier never cleans, stashes, or deletes operator files itself; `--yes` does not bypass the guard. `--check` and `--dry-run` are unaffected.
- New end-to-end regression tests drive `fraisier sync` against real git repositories (bare origin + work clone, only `gh` stubbed), covering the reported conflicts-only-in-auto-resolved-files promotion, the strictly-behind target, and the dirty-worktree abort.

## [0.41.0] - 2026-06-30

Combines the confiture 0.33 migration and restore-preflight guard (developed as 0.40.0, never published) with the bounded worktree self-heal feature. The #262 *root-cause* fix (schema-qualified `--table` in `restore_tracking_data`) shipped in 0.39.1 and is included here.

### Added

- **Bounded worktree self-heal on a stale checkout (heal-once-then-escalate).** A `git checkout` that exits 0 but leaves the working tree stale would advance the deployed version over old code; v0.39.0 verified the worktree and failed hard on a mismatch. `fetch_and_checkout` now attempts a bounded recovery — force-repopulate the worktree from the tree (`read-tree --reset -u` + `update-ref --no-deref HEAD`) and re-verify — before giving up. The recovery is **bounded to avoid the silent-masking trap** ([#257](https://github.com/fraiseql/fraisier/issues/257)): if the *same* worktree mismatches again within N deploys of its last heal, the deploy fails hard and alerts instead of self-healing on every deploy, surfacing a recurring environmental cause (read-only FS, bad perms, flapping mount). Per-worktree deploy/heal state persists in the bare repo; only tracked files are touched (untracked `version.json`, virtualenvs, build output preserved). New guards in `tests/test_git_operations.py`.

### Changed

- **Confiture integration migrated to fraiseql-confiture 0.33.x** (pinned `>=0.33.0,<0.34`). The previous loose pin (`>=0.9.4,<1.0`) let environments resolve a newer confiture whose preflight `--against` JSON schema (`{ok, summary, issues[]}`) and exit codes (0/7) fraisier could not parse — the proximate surface of [#262](https://github.com/fraiseql/fraisier/issues/262). The integration (result shapes, preflight parsing, exit-code handling) now targets 0.33.x, and the upper bound prevents silent drift onto a future schema change.

### Fixed

- **Restore preflight no longer replays the entire migration history against a populated schema with an empty ledger** ([#262](https://github.com/fraiseql/fraisier/issues/262)). A schema-only restore recreates `tb_confiture` without its rows; a data-only load of zero rows leaves the preflight DB with the full schema but an empty applied-migration ledger, so confiture treats the whole history as pending and replays every already-applied migration — a cascade of false `already exists` / dependency failures (exit 7). The preflight now branches on the `tb_confiture` **row count** (not table existence), and `count_user_relations` distinguishes a *populated schema with an empty ledger* (a failed tracking-data restore) from a *genuinely fresh* backup, refusing to replay-everything against the former rather than emitting a false-positive cascade. (The underlying cause — `restore_tracking_data` passing a schema-qualified name to `pg_restore --table=` so zero rows loaded — was fixed in 0.39.1 and is included here; this guard is the belt-and-suspenders layer.)

## [0.39.1] - 2026-06-29

### Fixed

- **Restore-based migration preflight no longer replays the entire migration history against a populated schema** ([#262](https://github.com/fraiseql/fraisier/issues/262)). `PreflightDatabase.restore_tracking_data` makes the schema-only preflight DB *self-consistent* by loading the backup's migration-ledger rows, so the preflight replays only genuinely-pending migrations. It built that loader as `pg_restore --data-only --table={tracking_table}`, passing the configured tracking table **verbatim** — but the common configured value is schema-qualified (`public.tb_confiture`), and `pg_restore --table=` matches an *unqualified* relation name, so `public.tb_confiture` matched nothing and **silently loaded zero rows**. The ledger stayed empty, confiture saw nothing applied, and the preflight replayed the whole history (from the project baseline) against a DB that already had the full schema — failing en masse with `relation … already exists` / `cannot change name of view column …` and aborting the deploy on a false positive (exit 7) **before the real restore ever ran**, leaving the target DB untouched while the app version still advanced. (The 0.40.0 `count_user_relations` guard *detects* this populated-schema-but-empty-ledger state; this fixes the cause so the ledger actually loads.) `restore_tracking_data` now splits a schema-qualified value into `--schema=<schema> --table=<table>`. Verified empirically: `--table=public.tb_confiture` loads 0 rows; `--schema=public --table=tb_confiture` loads all. New regression guards in `tests/test_preflight_core.py` cover the qualified (split) and unqualified (passthrough, no `--schema`) cases.

## [0.39.0] - 2026-06-25

### Fixed

- **A deploy whose checkout leaves the worktree stale now aborts instead of recording the new version over old code.** `fetch_and_checkout` ran `git checkout -f <new_sha>` then `git reset --soft <new_sha>` against the bare repo and returned `(old_sha, new_sha)` with **no check that the worktree files actually became `new_sha`**. When that invariant broke — the incident behind this fix was a staging worktree frozen at an old commit for weeks while the recorded version advanced — every deploy ran preflight, migrations, and the restart against **stale code** while `version.json` and `/health` reported the new SHA as live, so a no-op deploy was indistinguishable from a real one. (Distinct from [#257](https://github.com/fraiseql/fraisier/issues/257), which stopped `version.json` being left *advanced ahead of the schema* on a failed migrate; here the **worktree itself** never advanced.) `fetch_and_checkout` now calls a new `verify_worktree_at_sha(bare_repo, worktree, sha)` immediately after the reset and **raises `DeploymentError` if the working tree does not match the deployed commit** — before the caller records the version. The guard diffs the working tree against `new_sha` with `git --git-dir/--work-tree diff --quiet <sha> --`, so it works whether or not the worktree carries a `.git` file and inspects only tracked files (untracked build artifacts and virtualenvs are ignored); on mismatch it lists the stale files, the expected SHA, and a `git … status` recovery hint. The check is deliberately **content-based, not HEAD-based**: the worktree's HEAD is read from the bare repo and `reset --soft` advances it even when the files do not, so a `get_worktree_sha(worktree) == new_sha` check would have passed straight through the exact reported symptom (frozen files, advanced version). It is also mechanism-agnostic — the originally suspected cause (a bare-repo `checkout -f` exiting 0 without populating the tree) did **not** reproduce in isolation (`checkout -f` populated correctly and `check=True` already catches a non-zero exit), so rather than guarding one hypothesised path the fix verifies the **end state** and catches any cause: frozen tree, partial checkout, or HEAD-advanced-but-files-stale. New regression guards in `tests/test_git_operations.py` cover the verification call wiring, the stale-worktree `DeploymentError` (with mocked and **real** bare-repo + worktree fixtures), and the healthy post-checkout pass.

## [0.38.0] - 2026-06-25

### Fixed

- **Migration preflight failures now show *why* — confiture's stdout diagnostics are surfaced instead of swallowed** (refs [#259](https://github.com/fraiseql/fraisier/issues/259)). When `confiture migrate preflight --against` exited with a fatal code (e.g. exit **7** = `PFLIGHT_REPLAY_FAILED`), `_run_confiture_preflight` raised `DatabaseError(f"confiture preflight failed (exit {code}): {result.stderr}")` — surfacing only **stderr**, which confiture leaves *empty* for these failures. confiture writes its structured failure report (the issue **code**, the **offending migration**, and the underlying **error/path**) to **stdout**, which fraisier discarded — so a restore-based deploy aborted with a bare `exit 7` and no cause, forcing operators to reproduce the failure by hand to learn what broke. `_run_confiture_preflight` now formats a diagnostic from confiture's stdout (`_extract_preflight_issues` / `_format_preflight_failure`): it lists each issue as `CODE message [migration N] — error`, falls back to raw stdout then stderr, and always appends the `--skip-preflight` escape hatch. Verified end-to-end against confiture **0.32.0**: a replay failure that reads a sibling repo file now reports `PFLIGHT_REPLAY_FAILED … Migration 0001 (recreate_widget) … [Errno 2] No such file or directory: '…/db/0_schema/funcs/widget.sql'`. This is the diagnostics layer of #259; it does not yet change *which* migrations source the replay sees (the suspected root cause), which the now-surfaced output will confirm.
- **Preflight no longer silently passes on an unrecognized confiture output schema** (refs [#259](https://github.com/fraiseql/fraisier/issues/259)). fraisier parses confiture's `--against` JSON as the 0.9.x shape (`{"against": {"migrations": [{success, error}]}}`). Newer confiture (0.30+, including the 0.32.0 some environments resolve under the `>=0.9.4,<1.0` floor) emits a different schema (`{"ok", "issues": [...]}`) with no per-migration `migrations` array — so `against_data.get("migrations", [])` returned `[]`, fraisier saw **zero** failures, and a soft (exit-1) preflight failure under a skewed confiture was silently treated as a pass. `_run_confiture_preflight` now raises a clear `DatabaseError` naming the version skew (and echoing the surfaced issues) when the output lacks the expected per-migration array, instead of swallowing it. JSON that fails to parse is likewise routed through the diagnostic formatter rather than raising a bare `JSONDecodeError`. New regression guards in `tests/test_preflight_run.py::TestRunConfiturePreflight` cover the fatal-exit stdout surfacing, the `--skip-preflight` hint, the stderr fallback, and the unrecognized-schema raise (they fail on 0.37.0 and earlier). **Follow-up (tracked, not in this release):** pinning `fraiseql-confiture` to a single tested version and aligning the parser to its `--against` schema, plus the migration-source root cause once the surfaced exit-7 output confirms it.

## [0.37.0] - 2026-06-25

### Fixed

- **A deploy that fails at the migration step no longer leaves `version.json` advanced — a failed deploy stops reporting the new version as live** ([#257](https://github.com/fraiseql/fraisier/issues/257)). In `APIDeployer.execute()`, `version.json` (what `/health` and `fraisier health` report) was written at Step 2.5, *before* the Step 3 database migration. `version.json` is a generated, git-ignored file, so when a migration (or its preflight) raised and aborted the deploy, neither the abort path nor the git rollback touched it: it was left **advanced ahead of the schema**. Because the deployed app reads `version.json` per request, `/health` then reported the **new** version while `tb_confiture` was unchanged and **no migration had applied** — and combined with a fire-and-forget webhook trigger (CI returns on webhook send, not on deploy result), a hard-failing deploy was indistinguishable from a successful one. The reported impact was a multi-day window where every deploy no-op'd migrations but reported the new version, caught only by manually inspecting `tb_confiture`. The naive fix — generate `version.json` *after* the migrate — is unsafe here: the **rebuild** strategy reads `version.json` *during* migration (`resolve_app_version` → stamps `app_version` into `tb_version`), so it must be fresh *before* migrations run; deferring it would make rebuild stamp the stale prior version into the database. Instead, `execute()` now **snapshots** `version.json` immediately before regenerating it (`_snapshot_version_json`) and **restores** it (`_restore_version_json`) on every aborted path — migrate / post-migrate / restart failure (the `except` handler), the health-check and smoke-test rollbacks (via `rollback()`), and deploy timeout. Restore rewrites the captured bytes, or removes the file when none existed before (first-deploy case), so a failed migrate leaves the **old** version reported — correctly signalling the deploy did not complete. The restore is gated on this run having snapshotted, so a standalone `fraisier rollback` never deletes a live `version.json`; and the health-check-rollback leak (which left `version.json` ahead under the same mechanism) is closed as a side effect. A smoke-test **halt** (no rollback) deliberately keeps the new `version.json`, since the new code is actually live and serving. New regression guards in `tests/test_deployers.py::TestVersionJsonAbortRollback` cover migrate-failure restore, the first-deploy removal, the successful-deploy keep, and the health-check rollback restore (they fail on 0.36.0 and earlier). Distinct from [#253](https://github.com/fraiseql/fraisier/issues/253)/[#255](https://github.com/fraiseql/fraisier/issues/255), which deferred the version bump in the `ship`/PR flow — this is the **deploy path's** `version.json` stamping.

## [0.36.0] - 2026-06-25

### Fixed

- **`fraisier ship` now shows *why* a check failed — the output tail, not the head** ([#255](https://github.com/fraiseql/fraisier/issues/255)). On a failed ship check, the pipeline printed only the **first 10 lines** of the captured output. Tools like pytest, ruff, and mypy print their verdict at the **end** (the failure summary, assertion diffs, the error list), so the ship log showed the startup banner / collection noise but not the actual cause — operators had to re-run the failing lane by hand to diagnose it (surfaced while fixing [#253](https://github.com/fraiseql/fraisier/issues/253)). `ShipPipeline._print_result` now prints the **last 30 lines** instead. When the output is longer than that window, it appends a `... N earlier line(s) hidden` note **and** writes the *full* captured output to a `0o600` log file under the XDG logs dir (`$XDG_DATA_HOME/fraisier/logs/ship-check-<name>-<stamp>.log`, shared with the existing `fraisier._output` tee layer), printing the path so nothing is lost. Output that fits the window is shown whole with no note and no log file (re-runs on a converged tree stay quiet). Log writes are best-effort — an `OSError` creating the directory or file degrades to "tail only" rather than masking the check failure. New regression guards in `tests/test_ship_pipeline.py::TestPrintFailureOutput` cover the tail-vs-head selection, the truncation note + full-log capture (including the `0o600` mode), and the short-output no-log path.

## [0.35.0] - 2026-06-25

### Fixed

- **`fraisier ship` no longer leaves a dirty, half-bumped working tree when a check fails** ([#253](https://github.com/fraiseql/fraisier/issues/253)). `ship <bump>` wrote the version bump to `pyproject.toml` (plus its byproducts — `uv.lock`, a regenerated `detect-secrets` baseline, etc.) *before* the validation/test phases ran. When a check then failed, the ship aborted correctly — nothing pushed, no PR — but the bump was already on disk: the operator had to `git restore` several files by hand before retrying, and a naive retry would bump *again* from the already-bumped number (`X → Y → Z` instead of `X → Y`). Because `ship` auto-commits uncommitted tracked changes, a later successful ship could also silently sweep the stray bump into its commit. The bump is now **transactional with respect to the check phase**: the next version is still computed up front, but the on-disk write is deferred behind an `apply_bump` callback that `_ship_with_pipeline` invokes **only after both the fix and verify phases pass**, immediately before staging and committing. A failed check therefore leaves the working tree exactly as the user left it — no stray bump to revert, no double-bump on retry. The two `git add --update` calls collapse into a single staging point after the bump (the check phases run against the working tree, not the index, so the pre-verify stage was redundant). The `ship --no-bump` path passes no callback and is unaffected. New regression guard `tests/test_ship.py::TestShipPipelineIntegration::test_ship_check_failure_leaves_version_unbumped` lets the fix phase pass, fails the verify phase, and asserts `pyproject.toml` is still at the original version (it fails on 0.34.0 and earlier).

## [0.34.0] - 2026-06-19

### Fixed

- **Migration preflight no longer false-positives when the backup is behind the live tracking state** ([#250](https://github.com/fraiseql/fraisier/issues/250) — the real root cause). After 0.33.0 shipped a diagnostic for the *non-transactional-skip* variant, the reporter confirmed their failing pair was fully transactional Python migrations — a **different, third mechanism**, now reproduced and fixed. `run_migration_preflight` restored the backup **schema-only** into the throwaway preflight DB (stripping the `tb_confiture` rows) and then asked confiture to resolve the *pending* set from the app's **live** config DB via `--config`. When the restored backup was several migrations behind that live DB (e.g. a multi-migration feature landed after the backup was taken), every migration the live DB already considered applied — but which was **absent from the older backup's schema** — was excluded from the pending set and never run. A later *pending* migration that depended on such an object (e.g. a `CREATE VIEW` over a table an earlier, already-"applied" migration creates) was then evaluated against a base that lacked it and failed with `relation "…" does not exist` — while `confiture migrate up` after a real restore applies the whole chain in order and succeeds. The tell was exactly what the reporter saw: **only the dependent migration appears in the failure list; the predecessor is absent** (because it was never pending). The pending source and the apply base were inconsistent. The preflight DB is now made **self-consistent**: after the schema-only restore, fraisier loads just the backup's tracking-table rows (`pg_restore --data-only --table=<tracking>`), then resolves the pending set from the preflight DB itself — so the set matches exactly what `migrate up` would apply after a real restore. Backups with no confiture tracking (a DB never managed by confiture) fall back to treating every migration as pending, preserving prior behaviour. The tracking-table name is read from the app's confiture config (`migration.tracking_table`, default `tb_confiture`); the synthesized preflight config is written `0o600` since it carries the throwaway DB URL. New regression guard `tests/test_preflight_e2e.py::TestPreflightBackupBehindTracking` reproduces the exact backup-behind-tracking scenario with real `pg_dump`/`pg_restore` and asserts it preflights green (it fails on 0.33.0 and earlier). The 0.33.0 non-transactional-skip diagnostic is retained and complementary — the two mechanisms are independent.

## [0.33.0] - 2026-06-19

### Added

- **Migration preflight now self-diagnoses the one genuine "inter-dependent pending" false alarm and names the escape hatch** ([#250](https://github.com/fraiseql/fraisier/issues/250)). The issue reported that `confiture migrate preflight --against` gives false positives for inter-dependent pending migrations on restore-based deploys — hypothesising that each pending migration is evaluated against the *pristine* restored schema inside its own rolled-back savepoint, so a later pending migration cannot see an object an earlier pending one creates. **That hypothesis is contradicted by the source and refuted empirically.** confiture's `MigratorSession.run_against()` applies pending migrations *cumulatively*: each migration runs in a per-version savepoint that is `RELEASE`d (kept, merged into the enclosing transaction) on success and only `ROLLBACK`ed at the very end via a single outer `preflight_run` envelope. A reproduction (`V0` backup → pending `V1 CREATE TABLE widgets` → pending `V2 CREATE VIEW … FROM widgets`) preflights **green** on confiture **0.9.4** (this repo's pinned floor), **0.18.0** (the version the issue reported), and **0.30.0** (latest) — at both the engine level and through the full fraisier chain (`extract_schema_only` → `PreflightDatabase` → confiture CLI subprocess). The reporter's symptom (*earlier passes, later fails with `relation … does not exist`*) is real but has a **different cause**: when an earlier pending migration is **non-transactional** (`CREATE INDEX CONCURRENTLY`, `ALTER TYPE … ADD VALUE`, or a Python migration with `transactional = False`), confiture *skips* it during preflight — it cannot run inside the SAVEPOINT the check uses, and fraisier never passes `--allow-non-transactional` — so any later pending migration that depends on the skipped one's object fails with `… does not exist`, blocking a deploy that would actually succeed in production. fraisier now detects this exact signature (a skipped non-transactional migration earlier in the run, plus a later missing-object failure) and, instead of a mysterious hard block, appends a diagnostic to `MigrationPreflightError` and surfaces the actionable escape hatch: *"N failing migration(s) reference objects created by earlier pending migration(s) that were skipped as non-transactional … re-run the restore with `--skip-preflight` to bypass the check."* The gate is **not** weakened for genuine failures — a missing-object error with no preceding skip is still a hard failure with the canonical "fix the migrations" hint, and `--skip-preflight` is surfaced on the `db preflight` failure footer as an emergency-only option. New on `MigrationPreflightResult`: `skipped_migrations`, `suspected_false_positive_failures`, and `false_positive_note` properties; `db preflight --format json` gains a `suspected_false_positive_count` field. Regression guards now lock the cumulative-success contract in both repos: confiture's `tests/integration/test_preflight_against.py::test_preflight_against_later_pending_sees_earlier_pending` (the missing counterpart to the existing failure-isolation test) and fraisier's `tests/test_preflight_e2e.py::TestPreflightE2EInterdependent` (full chain, real `pg_dump`/`pg_restore`). The `fraiseql-confiture>=0.9.4` floor is documented in `pyproject.toml` with the verified rationale.

## [0.32.0] - 2026-05-30

### Fixed

- **`fraisier sync` no longer aborts when a prior squash-merged PR left an orphan remote head branch behind** ([#248](https://github.com/fraiseql/fraisier/issues/248)). When a prior sync PR was squash-merged without `--delete-branch` (the GitHub Web UI default and the natural shape of `gh pr merge --auto --squash`), the remote head branch kept its pre-squash commits. The next sync run created a fresh local branch from `origin/<source>`, tried to push, and was rejected with `! [rejected] (fetch first)` — the whole sync aborted with `subprocess.CalledProcessError` and the operator had to `git push origin --delete <branch>` by hand and re-run. The push path now runs through a new `_push_sync_branch(...)` helper that pre-flights the orphan check (delete the remote branch when its most recent PR is MERGED or CLOSED, skip when OPEN) and retries once on the canonical non-fast-forward shape if the race window between pre-flight and push elected a PR merge in the gap. The retry uses plain push when the orphan can be reclaimed, and `--force-with-lease` only when a live OPEN PR is still using the branch — and only inside the new `fraisier/**` namespace. The push subprocess runs with `LC_ALL=C` so the English-only stderr sniffer (`"non-fast-forward"`, `"fetch first"`) is locale-stable regardless of operator environment. Reclaim deletes use `subprocess.run(..., check=False)` so a branch that vanishes between the exists-check and the `--delete` (concurrent fraisier run, manual cleanup) is treated as the desired end state rather than a fresh failure.

### Changed

- **BREAKING (operational, not API): sync branches moved into the `fraisier/**` namespace.** `fraisier sync` now creates `fraisier/sync/<target>-from-<source>` instead of `sync/<target>-from-<source>`. The new namespace is declared **fraisier-owned** in `README.md` — any branch under `fraisier/**` may be created, updated, deleted, or force-pushed by fraisier without warning. This contract is what makes the orphan-reclaim and the live-PR-race `--force-with-lease` retry above safe in principle, not just in practice. The previous flat `sync/*` namespace is no longer touched by 0.32; the rename has no fallback shim. **Upgrade note:** merge or close any in-flight pre-0.32 sync PRs **before** upgrading. After upgrade, a new sync run will not find the old PR via `_find_existing_pr` (it looks for the new branch name) and will open a fresh PR alongside the stale one — you will end up with two open PRs into the same target if the old one is still around. Old `sync/*` branches left in the remote are not auto-cleaned; delete them by hand (`git push origin --delete sync/<target>-from-<source>`) once their PRs are closed.

### Added

- **`fraisier/**` reserved as the tool-owned branch namespace.** Future fraisier commands will colocate their throwaway branches under this prefix (`fraisier/<command>/...`). Hand-authored work pushed under this prefix will be reclaimed on the next fraisier run — keep your branches outside it. See the new README "Branch namespace" section.

## [0.31.0] - 2026-05-30

### Fixed

- **Webhook self-upgrade no longer kills concurrent deploys** ([#246](https://github.com/fraiseql/fraisier/issues/246)). On multi-environment hosts (e.g. dev + staging served by one webhook), the canonical "merge fraisier bump → ship dev → ship staging" promotion previously hit a race: the second deploy arrived during the upgrade worker's restart window and was killed by systemd's `SIGTERM` mid-flight, with no retry. The upgrade worker now touches a `.draining` flag in `lock_dir` **before** running `uv tool install`, sleeps a short settle window so dispatch-accepted tasks reach their `with deployment_lock(...)` line, waits for any in-flight `*.lock` to be released (default 10-minute timeout), and only then issues the restart RPC. During that window the webhook returns `HTTP 503 Service Unavailable` with a `Retry-After: 60` header and a structured JSON body identifying the refused fraises, so upstream callers (GitHub Actions, curl, monitors) record a loud, retriable failure rather than the previous silent drop. Drain-timeout exits with rc `2` (distinct from install-failure / restart-RPC-failure rc `1`), logs the held lock basenames, and leaves the unit unrestarted for operator intervention. Four new defaulted `webhook.self_upgrade_*` config keys (`drain_timeout_s` = 600, `drain_poll_s` = 1.0, `drain_settle_s` = 2.0, `retry_after_s` = 60) tune the timing; no `fraises.yaml` change is required for existing hosts to pick up the fix. The coordination is correct for `lock_backend=file` (the default); `lock_backend=database` hosts get the dispatch refusal but the drain loop sees no `*.lock` files and proceeds to restart immediately — behaviour for those hosts is no worse than today and a SQL-backed drain helper is tracked as a follow-up. New operator doc at [`docs/operations/self-upgrade.md`](docs/operations/self-upgrade.md) covers the knobs, failure modes, an optional GH Actions retry snippet, and the explicit residual-race scope.

### Changed

- **CLI output is now LLM-friendly by default**. Inspired by [rtk-ai/rtk](https://github.com/rtk-ai/rtk)'s compression strategies, with the flag polarity inverted: rtk opts *into* compact, fraisier opts *out* via `--verbose`. fraisier is increasingly invoked by non-human callers (GitHub Actions runners, webhook subprocesses, Claude Code Bash tool calls, ship pipeline self-invocations); today's Rich-markup output is verbose, ANSI-heavy, and noisy in those contexts. The new default mode strips Rich markup tags from all `console.print(...)` paths (Phase 1 leverage: one `_LazyConsole.print` rewrite covers ~149 call sites in `ship`, `sync`, `_deploy`, and adjacent CLI modules) and routes the structural success events (`Shipped vX.Y.Z`, `PR created: <url>`, `Auto-merge enabled (<method>)`, `Deploy successful!`, `Already up to date`, `Done. PR created/updated`, `Deployment successful`, `Version bumped: X -> Y`) through a new `fraisier._output.success(...)` helper that prefixes each line with `ok ` so LLMs and CI parsers can scan deterministically. Failure paths route through `fraisier._output.failure(...)` which prints `FAILED: <label>` + a focused detail line + the path to a tee'd full log under `~/.local/share/fraisier/logs/<command>-<ts>.log` (XDG-compliant, `0o600` permissions; clean exits delete the log, failures keep it for inspection). Three new global flags wired by `fraisier._output.install_cli_flags`: `--verbose`/`-v` (count flag; `-v` restores today's Rich story, `-vv` adds DEBUG logging, `-vvv` includes full subprocess pass-through), `--json` (one structured payload on stdout, mutually exclusive with `--verbose`), and `--no-tee` (skip the failure log file). Auto-detect *never upgrades* to verbose — `CLAUDECODE=1`, `CI=1`, and absent-TTY environments stay on compact unless the explicit flag is passed, so CI logs never get unexpected Rich noise. **Migration**: workflows wanting today's verbose output should add `--verbose` to their fraisier invocations; new tooling should prefer `--json`. The compact format preserves the key success-line substrings (`Shipped`, `PR created:`, `Deploy successful!`, `Auto-merge enabled`, `Already up to date`, `Done.`, `Deployment successful`, `Deployment triggered successfully`, `Version bumped:`) so existing `grep` patterns in CI workflows continue to match. v0.32.0 extends this treatment to `fraisier doctor`, `preflight`, `scheduled-install`, `status`, and the webhook server's `journalctl` output. New operator doc at [`docs/operations/llm-friendly-output.md`](docs/operations/llm-friendly-output.md) covers the output modes, tee location, the explicit `--verbose` opt-in story, and worked examples for each command.

## [0.29.0] - 2026-05-29

### Added

- **Webhook-driven scheduled install** ([#240](https://github.com/fraiseql/fraisier/issues/240)). After running `fraisier scaffold-install` once per host, webhook deploys of `type: scheduled` fraises now automatically copy declared unit files into `/etc/systemd/system/`, `daemon-reload`, and `enable --now` the timers — no more remembering to ssh in and run `sudo fraisier scheduled-install` after every config change. Manual `scheduled-install` becomes an override (rollback debugging, change-control), not a routine step. Per-fraise drift policy in `fraises.yaml` (`scheduled.auto_install.{on_missing,on_drift}`) controls behaviour when source and dest differ; default `on_drift: fail` aborts the deploy rather than silently overwriting operator hand-edits to `/etc/systemd/system/` (which is operator-editable in a way `app_path` is not). `overwrite` and `skip` are opt-in per-fraise. `deploy_event` grows `drift_overwrites`, `skipped_drift_units`, and `retried_busy` fields so external tooling can surface the structured change records.
- **`fraisier-unit-installer` socket helper** ([#240](https://github.com/fraiseql/fraisier/issues/240)). New root-privileged daemon receives manifest-driven install requests over a Unix socket. Enforces real `SO_PEERCRED` (UID must match the configured `deploy_user`), a render-time allowlist (`--allow <src_prefix>:<dest_prefix>` pairs baked at scaffold time), full TOCTOU defense via snapshotted `(dev, inode)` on each `dest_prefix` + `os.open(dir_fd, basename, O_NOFOLLOW)` on every file open (no `shutil.copy2`), per-op subprocess timeouts (30s `daemon-reload`, 60s per unit-targeting action), 5-minute overall wall-clock cap, and `fcntl.flock`-protected manifest execution so two concurrent deploys can't interleave (second gets `{"status": "busy"}` and retries). Marker-presence runtime gate on `disable_now`/`stop` post-actions prevents a deploy_user from disabling `sshd.service` via a crafted manifest. Manifest size capped at 1 MiB. New `--via-socket` flag on `fraisier scheduled-install` routes the manual command through the helper too, dropping the operator-sudo requirement for that path.
- **`SO_PEERCRED` retrofit across all four socket helpers** ([#240](https://github.com/fraiseql/fraisier/issues/240)). The existing `systemctl-helper`, `scaffold-install-helper`, and `install-helper` had docstrings implying a peer-creds check that the code never actually performed — the v0.28 trust boundary was purely `SocketUser=root, SocketGroup=<deploy_user>, SocketMode=0660` (any process in the deploy group could connect). All three now read `SO_PEERCRED` via the shared `fraisier._peer_creds` module and reject non-matching UIDs. The unit's `ExecStart` carries `--deploy-user <name>` injected by the renderer; the helper resolves to UID at startup via `pwd.getpwnam` (deferred resolution survives renderer/target-host split). A transitional shim (`--deploy-uid` numeric override, falls back to "log a one-time warning, skip the check, continue" when neither flag is passed) keeps pre-v0.29 units on disk working through the upgrade window; v0.30 will make the flag mandatory.
- **`scheduled-install --prune` for orphan removal** ([#240](https://github.com/fraiseql/fraisier/issues/240)). New `*.fraisier-managed` marker convention writes a JSON sidecar (mode 0600, root-owned) next to each fraisier-installed unit, carrying `fraises_yaml_path` (always `Path.resolve(strict=True)`), `fraise_name`, `environment`, `job_name`. `fraisier scheduled-install --env <env> --prune --dry-run` lists units with markers but no current `fraises.yaml` declaration; `--prune --yes` disables/stops them (timer-first ordering so a timer can't fire mid-prune), removes the unit + marker, and `daemon-reload`s once. Per-yaml + per-env scoping (both sides resolved to absolute paths) keeps cross-project hosts safe. Stale markers (corrupt JSON or no paired unit) are classified separately and cleaned up without invoking `systemctl`. Markers are **advisory, not authenticated** — `/etc/systemd/system/` is root-only-write so unprivileged adversaries can't plant fakes; root-side attackers can do anything anyway, so the marker convention is scoped to honest cross-project / cross-env mistakes, not security. v0.28-installed units auto-backfill their markers on the first v0.29 webhook deploy via a new `write_marker` op kind that the client emits for IDENTICAL diffs whose marker is missing on disk (idempotent: re-runs after the marker exists emit no op for that unit). `--prune --via-socket` lands in v0.30 with `RemoveFileOp` + ordered pre-actions in the helper protocol; v0.29's `--prune` runs under operator-typed sudo.
- **Drop the synthesised `<project>_<scheduled-fraise>_<env>.service` from `_collect_allowed_services`** ([#240](https://github.com/fraiseql/fraisier/issues/240)). The systemctl-helper allowlist no longer carries this phantom entry for `type: scheduled` fraises — no such unit exists on disk; the real per-job entries (added in v0.28.0 [#239](https://github.com/fraiseql/fraisier/issues/239)) are what actually need to be reachable. Cosmetic cleanup of the rendered allowlist + the legacy `systemctl-wrapper.sh` allowed-services array.

## [0.28.0] - 2026-05-29

### Added

- **`fraisier scheduled-install` — one-shot bootstrap of `type: scheduled` job systemd units** ([#239](https://github.com/fraiseql/fraisier/issues/239)). When a consumer adds a new `type: scheduled` job to `fraises.yaml`, the unit files land in the deployed worktree at `<app_path>/scripts/systemd/` but never reach `/etc/systemd/system/`. The operator previously ssh'd in, `cp`'d files, ran `daemon-reload`, and `systemctl enable --now <timer>` by hand — for every host, every new job, forever. The new `fraisier scheduled-install --env <env>` reads `fraises.yaml`, walks every `type: scheduled` fraise's `jobs.*`, and for each declared `systemd_service`/`systemd_timer`: compares source bytes against `/etc/systemd/system/<unit>`, copies + chmods 0o644 on `ABSENT`, classifies `DRIFTED` and fails closed (operator must pass `--force` to overwrite — no backup files are left behind), `daemon-reload`s once per call when any write happened, and `enable --now`s each timer that was written. `IDENTICAL` units are left untouched (re-runs are idempotent: zero file writes and zero `daemon-reload` invocations on a converged host). `MISSING_SOURCE` (operator declared a unit in yaml but the file isn't on disk) raises before any mutation. Flags: `--env` (required; lists available envs on omission), `--dry-run`, `--validate-only` (exits 0/1/2 for identical/missing/drifted), `--force` (overwrite drifted), `--yes/-y`, `--verbose/-v` (full unified diffs for drifted), `--fraise NAME` (narrow scope). Runs under operator-typed `sudo` — file copy and `systemctl` invocations both need root, and no new sudoers rule is rendered by `scaffold` for this command. Path-traversal defences run *before* any filesystem mutation: `unit_name` containing `/` or `..` is rejected (`validate_service_name`'s regex `^[a-zA-Z0-9_@.\-]+$` accepts `..` because `.` is in the allowlist, so an explicit substring check guards against that gap), `dest_path.parent` must resolve to `/etc/systemd/system`, and `source_path.resolve()` must be contained under `<app_path>/scripts/systemd/` (blocks a hostile worktree symlinking `scripts/systemd/foo.timer` outside the app dir). No automated CI coverage for the apply phase — `systemctl daemon-reload` and `enable --now` need a real init system; the operator-runtime contract is verified by the manual smoke checklist in the PR description only. Enumeration, classification, CLI surface, and the helper-allowlist fix below are fully unit-tested.
- **`_collect_allowed_services` now includes scheduled-job systemd units** ([#239](https://github.com/fraiseql/fraisier/issues/239)). The systemctl-helper allowlist (`fraisier/scaffold/renderer.py`) walked each fraise's environments but never descended into `jobs.*`, so the `systemd_service` / `systemd_timer` names declared on `type: scheduled` fraises' jobs were absent from the helper allowlist. The webhook-driven `ScheduledDeployer` runs unprivileged through this helper socket, so any newly-declared scheduled unit was rejected at deploy time until the operator separately re-ran `scaffold-install` — defeating the seamless-redeploy property webhook deploys are supposed to provide. NOT [#218](https://github.com/fraiseql/fraisier/issues/218) (which was the missing webhook unit, closed in v0.22.2); this is a separate, previously-untested gap surfaced by #239's work. **Staleness caveat**: the change updates the *generator*; the rendered `/etc/systemd/system/fraisier-<project>-systemctl-helper.service` on a target host is a separate artefact that only refreshes when `scaffold-install` runs. After declaring a new `type: scheduled` job, the operator must run `fraisier scaffold && fraisier scaffold-install --yes && sudo fraisier scheduled-install --env <env>` in that order. Skipping the middle step leaves the helper carrying the old allowlist; webhook-driven redeploys of the new timer will then be rejected until `scaffold-install` runs. Documented in `docs/cli-reference.md` under the `scaffold-install` "Adding a type: scheduled job" subsection.

### Deferred / out of scope

- `fraisier scheduled-install --prune` (marker-file-based orphan removal) — deferred to a follow-up issue. The hard part is "fraisier-owned naming pattern"; the design wants more data on the post-install path before committing to a marker-file convention.
- Webhook-driven install — letting `ScheduledDeployer.execute()` invoke the install path on first sight of a new unit, so operators never need to run `scheduled-install` by hand. Worth designing after a few cycles of operating the manual command.
- Generalising `scaffold_install_helper.py` to accept install-job manifests so this command could run unprivileged via socket. Larger redesign; bare `sudo fraisier scheduled-install` is the right MVP shape.

## [0.27.1] - 2026-05-29

### Added

- **Documented "bring-your-own batched releases" workflow** ([#234](https://github.com/fraiseql/fraisier/issues/234)). New [`docs/release-strategies.md`](docs/release-strategies.md) explains how to pair fraisier with [release-please](https://github.com/googleapis/release-please) (or any equivalent batcher) so feature PRs land code only via `fraisier ship --no-bump`, and a single release PR accumulates the version bump and CHANGELOG. Includes a working release-please GitHub Actions example, a trade-off table against the default per-PR workflow, and notes on how `--wait-deploy` and `fraisier sync` behave in each. No `release_strategy` yaml field is added; the design memo for that proposal (#234) is summarised in the doc page itself — the existing `--no-bump` flag plus a release-please workflow file is sufficient, and committing the schema surface is premature without adoption data.
- **`fraisier ship --no-bump --wait-deploy` now prints an explanatory note** before the health poll: `--no-bump: no version change — polling vX.Y.Z to confirm the current redeploy stays healthy. A later release-PR merge (if any) produces a separate deploy.` Operators running a bring-your-own release-please workflow could otherwise mistake the immediate health-poll success for the release-PR-triggered deploy they were expecting; the note makes the semantics explicit. `_trigger_deploy_for_current_branch` gained a keyword-only `no_bump` parameter; default `False` preserves the per-PR path's output verbatim.

## [0.27.0] - 2026-05-28

### Added

- **`fraisier ship` detects version-bump races against origin at push time** ([#232](https://github.com/fraiseql/fraisier/issues/232)). Two concurrent `fraisier ship` invocations starting from the same base both computed the same next version locally; the second to finish pushed a duplicate-version branch whose PR was `mergeable=CONFLICTING`, auto-merge never fired, and nothing paged anyone. After `run_verify_phase` succeeds and immediately before `git commit`, ship now runs `git fetch --quiet origin <race-branch>` (30 s timeout) and re-reads the `version` field from `pyproject.toml` at `origin/<race-branch>`. The race branch is the PR target when `--pr` is set (falls back to `ship.pr_base` in `fraises.yaml`) — the race is on the destination, not the working feature branch — else the current branch. When origin no longer matches the version observed at start, ship rolls back the on-disk pyproject bump (`git checkout HEAD -- pyproject.toml`) and exits 1 with `✗ Version race detected.` naming both versions and a copy-pasteable rebase recipe (`git pull --ff-only` on the base, `git rebase` on the working branch, re-run `fraisier ship <bump-kind>`). For `--no-bump` re-ships the recovery message tells the operator to decide whether to abandon or pick a new version, since re-shipping at the stale version is regressive. The check runs in both the pipeline and legacy ship paths; `--dry-run` skips it (no real push); `--no-deploy` does not skip it (still pushes, race still matters). Cost: one `git fetch` (~1 s) on a ~10-minute ship. "Branch missing on origin" (first push) degrades silently. Other fetch failures (network blip, auth, timeout) print a yellow `Warning:` and proceed — a flaky network shouldn't block ship.

## [0.26.1] - 2026-05-28

### Fixed

- **`fraisier sync` now propagates source-side deletions when target is unchanged** ([#235](https://github.com/fraiseql/fraisier/issues/235)). `git merge` doesn't surface "source deleted X, target never touched it" as a UU-style conflict — it silently keeps target's copy, so the sync PR carried the deleted file forward and the next deploy still saw the legacy file. A new `_propagate_source_deletions` pre-pass runs after `git merge --no-commit` and before the existing conflict loop: for each path in the merge-base→source `--diff-filter=D` list that target hasn't modified since merge-base, `git rm` mirrors the deletion onto the sync branch. Files that target *did* modify since merge-base are left for the operator (the existing tier-1 auto-resolver still handles the source-deleted-target-modified conflict case via `cat-file -e`). Surfaces as `Auto-resolved (source deletion): <path>` in the operator output.
- **`fraisier sync --prefer-source` now always creates the merge commit** ([#233](https://github.com/fraiseql/fraisier/issues/233)). When the conflict resolver auto-resolved every file back to source's version (the sync branch's tip), the index after `git add` was byte-identical to HEAD, the old `_commit_if_staged` helper's `git diff --cached --quiet` guard skipped the commit, MERGE_HEAD stayed set, and `git push` silently dropped the merge parent. GitHub then marked the PR `mergeable=CONFLICTING` and the CI sync workflow re-ran the merge and re-discovered the conflicts — the operator saw green local output but a stuck PR. The new `_commit_merge_or_staged` helper detects MERGE_HEAD via `git rev-parse --verify --quiet MERGE_HEAD` and commits unconditionally during a merge — git is happy to create a merge commit with no tree diff, which is exactly what records both parents. The non-merge fallback path keeps the original commit-if-staged semantics so empty commits outside a merge remain refused.
- **`fraisier sync` aborts before push if HEAD isn't a merge commit** ([#233](https://github.com/fraiseql/fraisier/issues/233) Layer 2 defence-in-depth). A new `_assert_merge_finalized` pre-push assertion verifies that `git log -1 --pretty=%P HEAD` reports at least two parent SHAs (so octopus merges with ≥3 parents pass too) *and* `MERGE_HEAD` is cleared. If either invariant is broken (a future code path skips the commit, a corrupt merge state, …) the push is refused with `✗ Sync abort: auto-resolve completed but HEAD is not a merge commit` rather than silently producing a CONFLICTING PR.
- **Deletion-propagation log no longer claims success when `git rm` fails** (post-review polish on #235). When the underlying `git rm` exits non-zero (submodule pathspec, sparse-checkout exclusion, …), the operator no longer sees `Auto-resolved (source deletion): <path>` followed by an unchanged index. The path is omitted from the propagation log and a `Warning: could not propagate deletion of <path>: <git stderr>` line is printed instead. Sync continues with the unresolved deletion still pending; downstream checks (the conflict loop, the pre-push merge-commit guard) handle the remainder.
- **`fraisier sync` surfaces a clear error if `git commit` fails mid-merge** (post-review polish on #233). `_commit_merge_or_staged` now catches `subprocess.CalledProcessError` from the underlying `git commit` and prints `✗ Sync abort: git commit failed during {merge finalization | pre-merge commit} (exit N). Inspect git status …` before exiting. The outer cleanup still resets the working branch and deletes the half-built sync branch; the new message just replaces the previous opaque traceback.

## [0.26.0] - 2026-05-27

### Added

- **`fraisier.introspection` — subcommand → config-section map + `!envvar` walker** ([#221](https://github.com/fraiseql/fraisier/issues/221), foundation for bundle B). Internal API that answers "if I ran `fraisier <subcommand>`, which YAML paths would it touch, and which `!envvar` refs would be reachable?" without running the subcommand. `ConfigPath` (dotted globbing), `EnvVarRef` (named tuple of name / yaml_path / is_set), `reachable_envvars(config, subcommand, *, fraise, environment)`. Walker never resolves `LazyEnv` — only inspects placeholders. A drift-guard test asserts every registered CLI command is either in `SUBCOMMAND_CONFIG_SECTIONS` or in `COMMANDS_WITHOUT_CONFIG_ACCESS`.
- **"Reads envvars:" section in every subcommand `--help`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 2). Click `format_epilog` hook derives the listing from the introspection map and the loaded config. Marks unset variables with `[unset]`. Renders a graceful placeholder when no `fraises.yaml` is discoverable. Verified safe across both `fraisier <cmd> --help` and `fraisier --config X <cmd> --help` invocation orders via a Cycle-0 spike before designing.
- **`fraisier env-check <subcommand>`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 3). Standalone preflight that prints which env vars a given subcommand would read and which are currently unset. Designed for CI gates: exit 0 if all set, 1 if any unset, 2 if subcommand invalid. `--format text` (rich.Table) or `--format json` for piping. `--required-only` filters to unset variables. `--fraise` / `--environment` narrow scope.
- **`fraisier doctor`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 4). Host-wide self-diagnosis independent of any particular fraise; answers "is this install OK to use?" Pluggable check registry with seven shipped checks: `python_version`, `fraisier_version`, `confiture_version`, `fraises_yaml_loadable`, `fraises_yaml_resolves` (uses the introspection walker), `secrets_env_readable`, `helper_sudoers` (stat-only — never invokes `visudo`, never parses sudoers syntax; byte-diffs against the expected fragment via `fraisier.scaffold.sudoers_diff` when content is readable). Each check is side-effect-free and isolated — one failing check never aborts the rest. `--format text|json`, `--check NAME` (repeatable) for filtering, `--skip-network` for offline environments. Exit codes: 0 / 1 / 2 for pass / fail / warn-only. Documented in `docs/doctor.md`.
- **`--format json` on `ship` (dry-run) and `deployment-status`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 6). New shared `--format text|json` option pattern via `fraisier/cli/_json.py`. `fraisier ship <bump> --dry-run --format json` emits a structured `{version: {old, new, bump_type}, dry_run, create_pr, pr_base, auto_merge, ...}` payload suitable for piping into `jq`. `fraisier deployment-status --json` is preserved as a hidden deprecated alias for `--format json`; the legacy flag emits a one-time per-process stderr warning. Other commands' existing `--json` flags are left untouched in this release — migration of the remaining ~10 sites will follow as a separate change.



### Added

- **Worked-example epilogs on every `--help` body, plus a `ship` flag-interaction matrix** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 1). Every subcommand under `fraisier ...` now ends its `--help` with at least two `fraisier ...` example lines so an agent or operator can grok flag interactions without source-reading. `ship --help` additionally enumerates the `--pr` / `--auto-merge` / `--wait-deploy` / `--no-deploy` / `--no-bump` / `--skip-checks` relationships in a single block. A new `tests/test_help_epilogs.py` parametrizes over every leaf command in `fraisier.cli.main.main` and asserts the contract holds — drift catches a missing epilog at CI time.
- **Per-instance `recovery_hint` on framework errors, surfaced into `str(err)`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 7). `FrameworkError.__init__` accepts a `recovery_hint` kwarg; when set, `str(err)` renders the hint as a trailing `Recover with: <hint>` line. A new `fraisier.errors.RECOVERY_HINTS` catalog supplies canonical strings keyed by scenario tag (`migration_preflight`, `migrate_partial`, `health_check_unhealthy`, `rollback_failed`, `deploy_timeout_unknown_phase`) so wording stays consistent across raise sites. The `MigrationPreflightError` raise site in `fraisier/strategies/_restore.py` is wired through to the canonical hint — the exact incident the issue references. CLI error rendering picks up the trailing line for free since handlers interpolate `str(exc)`. Empty-string explicitly suppresses the line.
- **`docs/webhook-protocol.md`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 5). Public contract for the JSON payload fraisier POSTs to `type: webhook` notification destinations. Documents every `DeployEvent.to_dict()` field, all four `event_type` values, the operator-supplied header auth pattern (fraisier does not HMAC-sign outbound notifications — the inbound `_verify_signature` machinery is unrelated), stability guarantees, three worked examples covering `failure` / `rollback` / `success`, and idempotency semantics. A new `tests/test_docs_webhook_protocol.py` parses every fenced JSON block in the doc and asserts every documented field is emitted by `DeployEvent.to_dict()` (and vice-versa) so doc and code can't silently drift.

## [0.24.0] - 2026-05-27

### Added

- **`scaffold-install` now diffs the current `/etc/sudoers.d/<project>` against the rendered fragment and warns about rules that would be removed** ([#224](https://github.com/fraiseql/fraisier/issues/224)). Previously the install silently overwrote the target file, so any operator-added rule (workarounds, non-fraisier components' rules, leftover entries from prior fraisier versions) silently disappeared on next `scaffold-install`. The wrapper now reads the current sudoers file via `sudo cat` (piggybacking on the existing sudo timestamp — no extra password prompt in practice), diffs against the rendered fragment using a line-set comparison that ignores comments and normalizes whitespace, and prints a clearly-formatted list of rules that would be removed. In interactive mode the existing `Proceed with installation?` confirm covers both the install plan and the sudoers diff — there is deliberately no second prompt. In `--yes` / `--dry-run` / `--validate-only` modes the diff still prints (`--yes` opts out of the prompt, not out of visibility). The check is silently skipped when `/etc/sudoers.d/<project>` doesn't exist yet (fresh installs).
- **`scaffold-install --strict-sudoers` flag** for CI/automation that should fail loud on any sudoers removal. Exits with code 3 (distinct from generic `1` and user-abort `2`) when rules would be removed OR when the current sudoers state can't be read — "couldn't verify" is treated as failure under strict, not silently as "no changes". Naming is deliberately per-resource: future nginx/systemd safety nets get sibling flags (`--strict-nginx`, ...) rather than a single coarse `--strict-overwrites`.
- **New module `fraisier.scaffold.sudoers_diff`** — pure-function `diff_sudoers(current, new) -> SudoersDiff` with `added` / `removed` rule lists and a `has_changes` property. Stdlib-only, no I/O, fully unit-tested.

## [0.23.1] - 2026-05-27

### Fixed

- **`scaffold-install` no longer crashes with an unhandled `PermissionError` on `Path.exists()` / `is_file()` / `chmod()`** ([#222](https://github.com/fraiseql/fraisier/issues/222)). On hosts where a parent directory of the generated `install.sh` isn't traversable by the invoking user, `Path.exists()` propagates `EACCES` as `PermissionError` rather than returning `False`. `is_file()` has the same failure mode. The wrapper now treats any `OSError` from those probes as "can't see the file" and prints the friendly "not found or not readable" error instead of a traceback. `chmod(0o755)` is similarly hardened: when the file is owned by another user the chmod is allowed to fail silently as long as the file is already executable (the common case for a freshly-generated script).

### Changed

- **`scaffold-install` failure messages now include the install.sh exit code and a copy-pasteable rerun command** ([#225](https://github.com/fraiseql/fraisier/issues/225)). The previous "Review the output above for details" line was misleading when no output existed (the wrapper inherits stdio, so a quiet `install.sh` produces no preceding output). Failures now print the actual exit code (`Preview exited with code 42` / `Validation exited with code 42` / `Installation failed (exit code 42)`) and a verbose+tee command the operator can paste to reproduce with a persistent log: `sudo /opt/<project>/scripts/generated/install.sh <flags> --verbose 2>&1 | tee /tmp/install.log`. `--verbose` is added only when not already present in the original invocation. The success path is unchanged. No behavioral change to subprocess invocation — this is strictly an error-message UX fix.

## [0.23.0] - 2026-05-26

### Changed

- **Lazy `!envvar` resolution** ([#220](https://github.com/fraiseql/fraisier/issues/220)). Env vars referenced via `!envvar` are now resolved at consumption time, not at config load. Subcommands that don't enter a section (`fraisier --help`, `fraisier --version`, `fraisier ship --help`, `fraisier ship patch --pr ...`, `fraisier list`) no longer require those env vars to be set. Resolution failures raise `ConfigurationError` (or the consumer's wrapping exception, e.g. `SmokeTestError` for smoke probes) with a message that names the full dotted YAML key path of the offending placeholder — `fraises.<name>.environments.<env>.smoke_tests[0].headers.Authorization` instead of an opaque `KeyError: 'SMOKE_TEST_JWT'`.
- **Read-each-access semantics for `!envvar`.** Each consumption of a `!envvar` field re-reads `os.environ`; there is no resolution cache. Previously the value was baked into the loaded config dict at parse time. The one-shot CLI invocation is unaffected; long-running consumers (the webhook daemon, tests using `monkeypatch.setenv`) now observe env-var mutations made after `FraisierConfig` construction.
- **Section-lazy validation.** Deep section validators (`fraises`, `notifications`, `hooks`) now run on first access of the matching property, not at `FraisierConfig.__init__`. Stage 1 of load is restricted to cheap structural / cross-reference checks (`servers`, `branch_mapping`, `service_manager`). Callers that depended on eager load failure for a specific section should use `fraisier validate` or access the relevant property explicitly.
- **`fraisier validate` semantics.** Default validate traverses every section and reports structural errors but does NOT resolve `!envvar` references — CI workflows that lint YAML shape without secrets are first-class. Pass `--resolve-envvars` to opt into the previous eager-resolution behavior (recommended for pre-deploy CI gates that need every secret materialized).
- **`fraisier validate --json` placeholder output.** Unresolved `!envvar` fields in dump output now appear as `"<envvar:NAME>"` placeholders rather than resolved values. Downstream JSON parsers should not assume resolved secrets in `validate --json` output.
- **YAML anchor / alias path tracking.** When an `!envvar` node is anchored and reused (`&shared !envvar X` … `*shared`), the resolution error names the *first* location encountered during the depth-first walk, not all of them. Operators diagnosing a shared anchor see only one of the use sites. PyYAML constructs each anchored node once, so this is the only stable single-path attribution available.

### Added

- **`fraisier validate --resolve-envvars` flag.** Walks the parsed config tree, resolves every reachable `LazyEnv`, and collects errors. Each unset variable surfaces with its env var name AND the YAML key path where it was declared. Shared LazyEnv instances (anchors / aliases) are resolved once per invocation.
- **`LazyEnv` placeholder and `to_str()` boundary helper** exported from `fraisier.config`. Plugin and external-callsite authors handling config values should call `to_str()` at any non-trivial consumer boundary (subprocess argv, HTTP headers, file paths). `LazyEnv` carries the env var name and YAML path for diagnostics; its `__repr__` never resolves, so logging a raw placeholder cannot leak the secret.

### Breaking

- **`format: !envvar X` rejected for token providers.** The token-provider `format` field must now be a literal string. Migration: replace `format: !envvar BEARER_FORMAT` with the literal pattern, e.g. `format: "Bearer {token}"`. Rationale: `format` is a `str.format`-style template whose `{token}` placeholder must be visible at config-load time for the placeholder-validity check; lazy resolution would defer that check until deploy time, by which point the template error is harder to diagnose.

## [0.22.2] - 2026-05-23

### Fixed

- **Webhook self-upgrade now surfaces helper allowlist rejections in the journal** ([#218](https://github.com/fraiseql/fraisier/issues/218)). The scaffold-template fix landed in 0.18.0 (`fraisier-{project}-webhook.service` is unconditionally prepended to the helper allowlist by `_collect_allowed_services`), so any host re-scaffolded on 0.18.0+ already self-upgrades end-to-end. Hosts originally bootstrapped before 0.18.0 still carry a stale `/etc/systemd/system/fraisier-{project}-systemctl-helper.service` whose `ExecStart` argv omits the webhook unit — `uv tool install` upgrading the binary doesn't rewrite that file — so the detached worker's restart RPC silently fails, recorded only in `/var/lib/fraisier/self-upgrade/<project>-<ts>.log`.
- **Pre-flight check in `maybe_self_upgrade`.** Before spawning the detached worker, the parent webhook now sends a read-only `is-active` RPC for its own service unit (`fraisier/webhook_self_upgrade.py:_preflight_helper_allowlist`). On a `service not allowed` rejection the webhook logs a WARNING **in its own journal** naming the cause and the remediation — `fraisier scaffold && fraisier scaffold-install --yes` — and skips the upgrade entirely (avoiding the half-state where the binary is upgraded but the process isn't). Operators watching `journalctl -u fraisier-<project>-webhook.service` see the actionable error on the next deploy attempt instead of having to discover the per-event log file.
- **Narrow scope.** Empty `FRAISIER_SYSTEMCTL_SOCKET` (install-only mode) and transient `ConnectionRefusedError` (startup races) fall through unchanged — those are non-actionable here and let the existing worker behaviour log its own per-event failure.

## [0.22.1] - 2026-05-23

### Fixed

- **Deploy daemon now finds `fraisier` under `uv tool install`** ([#216](https://github.com/fraiseql/fraisier/issues/216)). `BaseDeployer._get_fraisier_executable()` is replaced by a layered, cached resolver (`fraisier/deployers/base.py:_resolve_fraisier_executable`) that probes, in order: the `sys.executable` sibling (correct by construction for `uv tool install`, venv, pipx, and system-package layouts), `shutil.which("fraisier")`, then a hardcoded fallback list expanded to cover `~/.local/bin/fraisier` and `~/.local/share/uv/tools/fraisier/bin/fraisier`. Candidates are accepted only when they are regular files (or symlinks to one) with the executable bit set, so directories and broken symlinks no longer slip through. Lazy probe iteration means the happy path resolves with a single `stat(2)` — `$PATH` is not consulted when the sibling matches. The resolver is cached for the process lifetime (`functools.cache`); since the deploy daemon restarts after every self-upgrade, the cache cannot outlive a relocation of the binary it points at.
- **Self-diagnosing failure mode.** When no candidate resolves, the raised `RuntimeError` lists every probed location by name (`sys.executable sibling`, `$PATH`, each fallback path) plus the current `sys.executable` and a remediation hint (`uv tool install fraisier`, or symlink workaround). The journald entry is now actionable without re-running with `-v`.
- **Operator observability when the sibling probe misses.** When resolution falls back past the sibling, an `INFO` line is emitted once per process naming the strategy that succeeded and instructing the operator to re-run `uv tool install fraisier` to bring the daemon's Python and the `fraisier` CLI back into lockstep. The `ERROR` swallowing in `APIDeployer._sync_config_if_needed` is unchanged, so transient discovery anomalies still do not fail the deploy.

## [0.22.0] - 2026-05-22

### Added

- **Pluggable token providers for `smoke_tests`** ([#215](https://github.com/fraiseql/fraisier/issues/215)). New optional `token_provider:` block per smoke test acquires a short-lived bearer credential at deploy time, then injects it into the configured `header` using `format` (default `"Bearer {token}"`). Closes the gap where a long-lived JWT in `os.environ` doesn't fit — vault-issued tokens, OIDC machine clients, federated assume-role workflows. Three provider types are built in:
  - **`exec`** — runs a configured `command` (typed as a list of strings, invoked with no shell) and uses stdout (trailing newline stripped). Non-zero exit, timeout, or unexpected failure raises `DeploymentError` naming the exit code; the subprocess stderr tail is emitted at DEBUG only (never in the exception message, since a `set -x` wrapper could leak the token there). `argv[0]` logs at INFO; full argv at DEBUG; the resolved token never appears in any log line. `cwd` and `env_passthrough` are deferred to a later release and are **rejected at config-load time** if set — the subprocess inherits the deploy user's environment (same envelope as `post_migrate`).
  - **`oauth2_client_credentials`** — POSTs `grant_type=client_credentials` to `token_url` with `client_id`, `client_secret`, and optional `audience` / `scope`. Returns the response's `access_token`. Non-2xx, missing `access_token`, or network errors raise `DeploymentError`; the response body is never echoed in the error message (some IdPs include the `client_secret` in error envelopes). `client_secret` is redacted in all log lines.
  - **`oauth2_refresh_token`** — POSTs `grant_type=refresh_token` with `client_id` and `refresh_token`. Rotated `refresh_token`s in the response are **discarded** — fraisier never writes to your secrets store; rotation is the operator's responsibility.
- **`fraisier/token_providers.py`** — abstract `TokenProvider` base plus one concrete frozen dataclass per type: `ExecTokenProvider`, `Oauth2ClientCredentialsTokenProvider`, `Oauth2RefreshTokenProvider`. Each subclass carries only its own type-specific fields (no `None`-typed sentinels for sibling-type fields) and owns its `.resolve()` method. `parse_token_provider(raw)` dispatches on the YAML `type:` to the matching subclass. Pure-structural parse, never shells out or hits the network at config-load time. `materialize_test_headers(tests)` in `fraisier/smoke_tests.py` resolves each provider instance at most once per deploy (cache keyed on `id(provider)`); smoke tests sharing a `token_provider:` block via a YAML anchor (`&p` / `*p`) get the same token. Plain YAML duplication of the block produces distinct instances and distinct resolution calls — use anchors when sharing matters. The deploy pipeline enters via `smoke_tests.resolve_and_run(tests)`, which sequences materialization and execution and surfaces the two failure modes as distinct exception classes (`DeploymentError` vs `SmokeTestError`).
- **Header collision check.** A smoke test that declares both `headers.<X>` and a `token_provider.header=<X>` is rejected at config-load time with `ConfigurationError`. Comparison is case-insensitive per RFC 7230.
- **Strict `token_provider:` schema.** Unknown keys (typos, the deferred `cwd` / `env_passthrough` options, fields valid on a different provider type) are rejected at config-load time with the valid-key list for the declared type. Prevents silent no-ops at deploy time.
- **`format` placeholder validation.** The `format` value must contain the literal `{token}` placeholder and no other placeholders. A `format: "Bearer ABC"` would silently drop the resolved token; a `format: "Bearer {access_token}"` would `KeyError` mid-deploy. Both shapes fail at config-load now.

### Changed

- **`load_smoke_tests` now raises `ConfigurationError` instead of `ValueError`** for all schema errors (unknown `method`, unknown `on_failure`, malformed JSONPath, relative URL without base, unknown `token_provider.type`). The `_validate_smoke_tests` wrapper in `fraisier/config/_validation.py` catches both classes during the transition. Brings `smoke_tests.py` in line with the rest of `fraisier/config/`, which has always raised `ConfigurationError`. Callers catching `ValueError` from `load_smoke_tests` should switch to `ConfigurationError`.

### Upgrade notes

- Opt-in: a smoke test without a `token_provider:` block keeps v0.21.x behavior — the static `Authorization: !envvar X` header flows through verbatim. No deployment changes for users not using the new feature.
- A v0.22.0 fraises.yaml that uses `token_provider:` requires v0.22.0+ fraisier to parse. Roll forward, not back, once a provider is configured.
- Provider runtime failures (exec script crash, IdP 401, network error) raise `DeploymentError` and **halt** the deploy with `status=failed`. By this point migrations have run and the service has restarted on the new code; a transient IdP issue is not a code regression, so the previous version is **not** automatically restored — operators investigate and re-deploy. (Code regressions are still caught by `run_smoke_tests`'s `on_failure: rollback` default, which is unaffected by this change.)
- `exec` subprocess `stderr` is **no longer included** in the raised `DeploymentError` message: a wrapper with `set -x` would otherwise leak the token to the deploy journal. Operators who need the stderr tail can re-run with `FRAISIER_LOG_LEVEL=DEBUG`.
- See `fraises.example.yaml` and `docs/deployment-guide.md` for worked examples.

## [0.21.1] - 2026-05-22

### Fixed

- **`fraisier sync` is now idempotent on retry** ([#213](https://github.com/fraiseql/fraisier/issues/213)). Re-running after a previous interrupted or completed sync no longer fails:
  - The local sync branch is force-created (`git checkout -B`), so a stale branch from an interrupted prior run is silently overwritten by the fresh checkout. Sync branches are fraisier-owned end-to-end; any commits on them are re-derived from `origin/<source>` + pre-merge on every run.
  - After pushing, fraisier checks for an existing PR for the sync branch via `gh pr view`. When the prior PR is OPEN, it re-enables auto-merge (idempotent for `--squash`) on the existing PR instead of failing on `gh pr create`. The PR URL appears in the success message as `PR updated and auto-merge enabled: <url>`.
  - When the prior PR is CLOSED or MERGED, fraisier opens a fresh PR (GitHub allows reusing a head ref after closure). The prior PR URL is logged for operator context.

### Upgrade notes

- A user who has manually committed to a sync branch will lose those commits on the next `fraisier sync` invocation (the new `-B` overwrites the local branch). Sync branches were already documented as fraisier-owned; if you want to keep commits on a sync branch, push them to a non-sync ref.

## [0.21.0] - 2026-05-22

### Added

- **Authenticated smoke tests** ([#204](https://github.com/fraiseql/fraisier/issues/204) PR B). New `smoke_tests:` list runs configured HTTP requests with bearer credentials and JSONPath assertions immediately after the unauthenticated `/health` poll succeeds. Closes the class of regressions where a new table or `CREATE OR REPLACE VIEW` slips past unauthenticated `/health` and only fails when authenticated traffic hits. Each test takes `method`, `url` (absolute, or relative — joined onto `health_check.url`), `timeout`, `headers`, optional `body`, an `on_failure` policy (`rollback` default, `halt`, or `warn`), and a list of `assert` entries. JSONPath is the minimal `$.dotted.path` subset — no `$..foo` recursion, no array indexing, no wildcards (rejected at config-load time with a clear "use $.dotted.path only" message).
- **`fraisier/smoke_tests.py`** — `Assertion`, `SmokeTest`, `SmokeTestError`, `_walk_json_path`, `load_smoke_tests`, `run_smoke_tests`. HTTP via `httpx.Client`. `Authorization`, `Cookie`, and `X-API-Key` header values are redacted in log output.
- **`!envvar` YAML tag** in `fraises.yaml`. Values like `Authorization: !envvar SMOKE_TEST_JWT` are resolved from `os.environ` at config-load time; missing variables raise `ConfigurationError` immediately. Implemented as a `yaml.SafeLoader` subclass so safety guarantees are preserved.

### Upgrade notes

- Opt-in: a fraise without a `smoke_tests:` block is unchanged.
- `rollback` is the default `on_failure`: an authenticated probe that fails after `/health` passes is by definition a regression the unauthenticated probe missed — the safest default is to restore the previous version.
- `halt` is for cases where rolling back would be more disruptive than serving a partially-broken version (rare; typically only useful when the failure points to *external* state).
- Smoke tests run before the success result is constructed, so a rollback or halt failure is recorded as `status=failed` in `tb_deployment` with the smoke-test error message.
- See `fraises.example.yaml` for the config block shape.

## [0.20.0] - 2026-05-22

### Added

- **Post-migration SQL hooks** ([#204](https://github.com/fraiseql/fraisier/issues/204)). New `database.post_migrate:` list runs configurable SQL files between `confiture migrate up` and the service restart — typically the project's idempotent `db/7_grant/*.sql` sweep, but any cross-script reconciliation (post-migration ACL fixups, default-value sync) fits the slot. Each entry takes exactly one of `sql_dir` (runs all `*.sql` lexicographically) or `sql_file` (single file), plus an `on_error` knob (`halt`, default — raises `DeploymentError` and aborts before the service restart, so no rollback is needed; or `warn` — logs and continues). Closes the gap that lets a new table without grants slip past unauthenticated `/health` and fail under authenticated traffic.
- **`fraisier/post_migrate.py`** — `PostMigrateStep`, `load_post_migrate_steps(database_config, *, app_path)`, `run_post_migrate_steps(steps, *, database_url, runner)`. psql shellout uses `["psql", database_url, "-v", "ON_ERROR_STOP=1", "-f", str(sql_file)]`.

### Upgrade notes

- Opt-in: a fraise without a `database.post_migrate` block is unchanged.
- The hook runs **before** the service restart. A `halt` failure aborts the deploy with `status=failed` and leaves the previous service version serving — there is nothing to roll back because nothing was restarted yet.
- The SQL files run under the `database.database_url` role (the same role confiture uses for the migration). This is intentional: drift from a non-owner `CREATE OR REPLACE` is the specific bug the hook closes.
- See `fraises.example.yaml` for the config block shape.

## [0.19.1] - 2026-05-22

### Added

- **`fraisier trigger-deploy` prints the duration estimate before dispatching** ([#201](https://github.com/fraiseql/fraisier/issues/201) follow-up). Human operators now see `Estimated completion: ~Nm (history|fallback, N samples)` immediately above `Deployment triggered successfully`, closing the gap between the webhook surface (which already carried the estimate) and the CLI surface. Best-effort: any estimator failure (missing local fraisier DB, import error, etc.) is swallowed so the deploy still dispatches.
- **`tb_deployment.db_size_mb` is now populated** ([#201](https://github.com/fraiseql/fraisier/issues/201) follow-up). After each successful deploy with a configured `database.database_url`, fraisier samples `SELECT pg_database_size(current_database())` via `psql` and stores the byte count converted to MB. Lights up the fallback path of the duration estimator (which already knew how to use `db_size_mb` — it just got `None` before). Best-effort: psql failures, missing `database_url`, or unparseable output leave the column NULL. The estimator continues to work via its per-strategy floor — only the size-aware scaling is lost. Debug-level log entries carry a password-redacted form of the URL so misconfigured deploy roles are diagnosable from logs.

### Internal

- **Shared estimate helpers in `fraisier/duration_estimate.py`** — `build_estimate`, `to_dispatch_dict`, `format_estimate_line`. The webhook surface (`webhook._build_estimate`) and the new CLI surface both consume these; the previous in-module copy in `webhook.py` is gone.
- **`fraisier/dbops/sizing.py`** — `query_database_size_mb(database_url, *, runner)` standalone helper. One consumer (`_complete_db_record`); kept separate from `dbops/operations.py` for test focus.

### Upgrade notes

- No schema migration. The `db_size_mb` column already existed (introduced in v0.19.0); this release only changes the write path.
- The new sampling adds one `psql` round-trip per successful deploy. On databases with millions of relations `pg_database_size` can take 1-2 seconds; on typical sizes the cost is negligible against a multi-minute deploy.
- Deploy roles that cannot CONNECT to the **app DB** (only the maintenance DB) will see `db_size_mb` stay NULL silently. The estimator's fallback path still works, but the history-aware path never benefits. Check the debug log for `query_database_size_mb: psql failed` entries if estimates do not improve over time.

## [0.19.0] - 2026-05-21

### Added

- **Deployment duration estimates in webhook responses** ([#201](https://github.com/fraiseql/fraisier/issues/201)). Webhook dispatch responses now carry `estimated_duration_s`, `estimated_ready_at` (UTC), and `estimate_confidence` (`"history"` or `"fallback"`) for each triggered deployment that has a `database.strategy`. Agentic and human callers can use these to size their `sleep`/poll loops at trigger time instead of guessing.
- **`tb_deployment.strategy` + `tb_deployment.db_size_mb` columns**. Recorded per successful deploy via `complete_deployment(..., strategy=..., db_size_mb=...)`. Legacy installs are migrated in place via idempotent `ALTER TABLE ... ADD COLUMN`. ETL and docker_compose fraises (no database section) record `strategy=NULL` and are excluded from the estimator.
- **`FraisierDB.get_successful_deploy_durations(*, fraise, environment, strategy, limit)`**. New repository method consumed by the estimator; returns the most recent successful deploy durations filtered by the trinity, excluding NULL durations and non-success rows.
- **`fraisier/duration_estimate.py`** — `estimate_duration(db, *, fraise, environment, strategy, db_size_mb)` returns an `EstimateResult(seconds, confidence, samples_used)`. Uses the median of the most recent up-to-5 successful runs (with a 1.20 buffer) when ≥3 samples exist, otherwise falls back to a per-strategy seconds-per-MB rate clamped to a per-strategy floor (180s rebuild, 120s restore_migrate, 30s migrate, 60s for unknown strategies). Wrapped in `try/except` so a flaky history store cannot block a deploy.

### Upgrade notes

- Schema additions are additive; no migration required beyond starting the upgraded fraisier process (which runs the `ALTER TABLE` step on first connect).
- The CLI surface (`fraisier trigger-deploy` printing `Estimated completion: ~Nm`) is deferred to a follow-up PR — the webhook surface is the higher-value one for agentic callers and the CLI version can land independently once this lands.
- `db_size_mb` is reserved for future use; values are recorded as NULL today. Plumbing it through requires a `pg_database_size()` query before each deploy.

## [0.18.0] - 2026-05-21

### Added

- **Webhook self-upgrade when deployed pyproject pins a newer fraisier** ([#162](https://github.com/fraiseql/fraisier/issues/162)). After every successful deploy the webhook inspects the deployed `pyproject.toml`. If `[project].dependencies` (or any `[project.optional-dependencies.*]` group) contains an exact `fraisier==X.Y.Z` pin newer than the running webhook, fraisier detaches a worker that runs `uv tool install --force --refresh-package fraisier fraisier==X.Y.Z` against the webhook user's own uv tool directory, then asks the systemctl-helper socket to restart the webhook unit. The current deploy is unaffected — the upgrade applies to the next deploy. Range-pinned (`>=`, `~=`, `!=`) and unpinned dependencies are intentionally skipped. Complements the bootstrap-side restart already shipped in [#156](https://github.com/fraiseql/fraisier/issues/156) by covering the deploy-driven path (the operator-driven path was already covered).
- **`fraisier-{project}-webhook.service` in the systemctl-helper allowlist**. The self-upgrade above restarts the webhook via the existing root-privileged `fraisier-systemctl-helper.service` socket — the webhook process is not root, so it cannot `systemctl restart` itself directly. The helper rejects any service not in its compile-time allowlist; the webhook unit is now included by `_collect_allowed_services` in `fraisier/scaffold/renderer.py`.
- **`webhook.self_upgrade` config knob**. Default: `true`. Set `webhook: { self_upgrade: false }` in `fraises.yaml` to opt out per webhook.

### Upgrade notes

- Existing installations must re-scaffold and re-run `install.sh` to add the webhook unit to the systemctl-helper allowlist. Until they do, the install side of the self-upgrade succeeds but the restart RPC is rejected (logged at `error` level, deploy is not affected).
- Self-upgrade logs land in `/var/lib/fraisier/self-upgrade/{project}-{ts}.log` (one file per upgrade attempt). Inspect there when a self-upgrade does not seem to have taken effect.

## [0.17.0] - 2026-05-21

### Added

- **Parallel `pg_dump` via `backup.jobs`** ([#202](https://github.com/fraiseql/fraisier/issues/202)). Set `backup.jobs: N` in `fraises.yaml` or pass `--jobs N` to `fraisier db backup` to run pg_dump with N parallel workers. When `jobs == 1` (default), behaviour is byte-identical to today — a single-stream `pg_dump -Fc` producing one `.dump` file. When `jobs > 1`, fraisier switches to directory format (`pg_dump -Fd -j N`) producing a `<db>_<mode>_<ts>_<algo>.dump/` directory containing `toc.dat` plus per-table `*.dat` blobs. Parallelism comes from concurrent table COPYs.
- **`find_latest_backup` discovers directory-format dumps**. All restore consumers (the CLI restore path, `RestoreMigrateStrategy`) now transparently locate either form. `Path.glob` matched both already; the change is in behaviour lock-in and tests. `pg_restore` auto-detects `-Fd` from the positional directory path, so no caller needed adjusting.
- **`OnFailure=` hook on scaffolded backup units** ([#202](https://github.com/fraiseql/fraisier/issues/202) Phase 4). Closes the alerting gap that hid the original prod incident — when a backup exits non-zero (pg_dump SIGTERM, disk full, TOC verification rejection, size-sanity rejection), systemd now triggers `fraisier-<project>-backup-alert@%n.service`. The default alert is passive — pipes one line through `systemd-cat` into the journal. Operators who want real alerting can override the template with `systemctl edit fraisier-<project>-backup-alert@.service` or replace the file. fraisier ships no notifier choice to avoid lock-in.

### Changed

- **Size sanity check for directory dumps** uses recursive content size, not the directory inode's bare `stat().st_size` (~4096 bytes). Extracted as `_dump_size(path)` in `fraisier/dbops/backup.py`. Without this, a directory dump would falsely trigger the size check whenever a prior file dump existed.
- **`cleanup_old_backups` removes both file and directory dumps** via `shutil.rmtree` for directories, guarded by a resolved-path containment check against `backup_dir` so a glob result cannot escape via a symlinked entry.

### Upgrade notes

- Existing single-file `.dump` backups remain readable; nothing in the v0.16.x line is invalidated.
- Existing installations must re-run `install.sh` to pick up the new `fraisier-<project>-backup-alert@.service` unit and the `OnFailure=` line on the existing backup unit.
- Setting `backup.jobs > 1` requires that the database disk, the backup target disk, and the network between them can sustain N parallel reads/writes; higher `jobs` does not always mean faster. Tune to your hardware.
- The pre-deploy `BackupHook` (the `pg_dump | gzip > *.sql.gz` path in `fraisier/hooks/backup.py`) is intentionally unchanged — that code path is separate from `run_backup()` and does not benefit from parallelism in its current form.

## [0.16.6] - 2026-05-21

### Fixed

- **Scaffolded deploy service unit silently dropped `StrictHostKeyChecking`** ([#152](https://github.com/fraiseql/fraisier/issues/152)). The `Environment="GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new"` line was tokenised by systemd before quote handling, so `-o` was rejected with `Invalid environment assignment, ignoring: -o` and the option silently dropped — first-time `git fetch` then failed with SSH exit 255 (the symptom #116 had originally tried to address). The template now uses the equivalent no-space form `-oStrictHostKeyChecking=accept-new`; existing installations need to re-scaffold to pick up the fix.
- **Generated sudoers fragment ended with a double newline** ([#161](https://github.com/fraiseql/fraisier/issues/161)). `pre-commit-hooks/end-of-file-fixer` flagged the file on every commit. The blank-line separator between rules is now suppressed on the final iteration via `{% if not loop.last %}`, preserving readability between rules without trailing whitespace.
- **`fraisier sync` aborted with "nothing to commit" when conflicts auto-resolved back to source HEAD** ([#164](https://github.com/fraiseql/fraisier/issues/164)). The clean-merge path already guarded its pre-merge commit with `git diff --cached --quiet`; the conflict-resolution path did not. When every conflicted file was a fraisier-owned file that resolved to source HEAD (which was already the sync branch's tip), the staged index ended byte-identical to HEAD and `git commit --no-edit` exited non-zero. Both paths now route through a single `_commit_if_staged(message)` helper.

## [0.16.5] - 2026-05-21

### Fixed

- **`RebuildStrategy` / `RestoreMigrateStrategy` fail on re-deploy when template database already exists** ([#200](https://github.com/fraiseql/fraisier/issues/200)). Postgres refuses to drop a database with `datistemplate=true` (even `WITH (FORCE)`), so when `create_template=true` is configured, the drop step silently failed on every re-deploy and the subsequent `create_db` failed with "database already exists" until manual intervention. `drop_db` now accepts `clear_template_flag=True`, which issues `UPDATE pg_database SET datistemplate=false WHERE datname=...` before the drop. Both rebuild and restore-migrate strategies pass this flag when re-creating the template, and now check the drop return code so future failures surface immediately instead of cascading.

### Added

- **Post-dump backup verification** ([#202](https://github.com/fraiseql/fraisier/issues/202)). `run_backup()` now runs two cheap defences after `pg_dump` exits successfully:
  - **TOC integrity check** via `pg_restore --list <path>`. Reads the archive header only — no database connection needed — and rejects the backup if the TOC fails to parse. Catches the truncation pattern seen in the recent prod incident (pg_dump SIGTERMed mid-write, leaving the TOC intact but data blocks truncated).
  - **Size sanity check** vs the most recent same-mode dump in the output directory. A new dump under 50% of the previous one's size is rejected with a `BackupResult` that names the byte counts.
  Both checks shrink the silent-truncation window from "until someone tries to restore" down to "next backup attempt." Implements step 1 of #202; parallel `pg_dump -Fd -j N` support and systemd `OnFailure=` scaffold hooks (steps 2 and 3) follow separately.

## [0.16.4] - 2026-05-19

### Added

- **Template database version stamping** ([#198](https://github.com/fraiseql/fraisier/issues/198)). `RebuildStrategy` now writes a build-time version stamp into the source database's `public.tb_version.app_version` immediately before cloning the template. The atomic `CREATE DATABASE … TEMPLATE …` carries the stamp into the template, so downstream consumers (e.g. a reseed endpoint) can read `tb_version.app_version` from the template and reject reseeds from stale templates.
  - The version is auto-discovered from `<project>/version.json` (preferred) or `<project>/pyproject.toml`. No `fraises.yaml` change is required for standard projects.
  - New optional `database.app_version` key in `fraises.yaml` to override auto-discovery. Invalid values (anything outside `[A-Za-z0-9._+\-]`, including PEP 440 epoch versions like `1!2.3.4`) cause `RebuildStrategy` to fail loudly at construction with a `ValueError` so typos do not silently produce unstamped templates.
  - The stamp is best-effort: if no version is resolvable, if `tb_version` is missing, or if psql returns a non-zero exit, fraisier logs a warning and the rebuild succeeds normally — the protection is fail-safe on the consumer side.
  - **Requires `public.tb_version` to contain at least one row.** The UPDATE has no WHERE clause and would silently affect zero rows on an empty table; fraisier detects this case (`"UPDATE 0"` in psql stdout) and emits a distinct warning ("`tb_version` is empty; template will be unstamped") instead of logging false-positive success. Projects must seed `tb_version` with one row as part of their schema for the stamp to take effect.
  - The race against a reconnecting app overwriting the stamp between commit and clone is closed by a `terminate_backends → stamp → terminate_backends → create_db(template, template=source)` window inside the rebuild's `create_template` block. `CREATE DATABASE … TEMPLATE …` itself fails closed if any backend slipped through.

### Upgrade notes

- Templates created by earlier fraisier versions are not stamped. Consumers that strictly reject unstamped templates will refuse reseeds from these pre-existing templates until the next rebuild. Operators should either trigger a rebuild after upgrade, or implement a grace period in their consumer that treats a missing stamp as "unknown" rather than "stale" during the cutover.

## [0.16.3] - 2026-05-18

### Fixed

- **`gateway.conf` crashes nginx in multi-server setups** ([#197](https://github.com/fraiseql/fraisier/issues/197)). When per-environment nginx configs exist (`gateway_env.conf`), `gateway.conf` no longer emits environment-specific HTTPS `server {}` blocks with SSL certificate paths that may not exist on every machine. It now contains only shared directives (`limit_req_zone`, HTTP catch-all) so it can be safely installed unconditionally on all servers. The legacy single-server path (no per-env nginx) is unchanged.

## [0.16.2] - 2026-05-16

### Added

- **`create_template` support in `RebuildStrategy`**. Set `create_template: true` (and optionally `template_name: <name>`) in fraises.yaml under `database:` to snapshot the freshly-rebuilt database as a PostgreSQL template database after every rebuild. The default template name is `template_<db_name>`. This allows downstream tooling (e.g. a reseed endpoint) to restore from the template using `CREATE DATABASE … TEMPLATE …` for a fast, schema-current reset without re-running the full rebuild.

## [0.16.1] - 2026-05-16

### Fixed

- **`uv sync` fails when root-owned `__pycache__` dirs exist in app venv** ([#196](https://github.com/fraiseql/fraisier/issues/196)). Three-layer fix: (1) `fraisier/__init__.py` now sets `sys.dont_write_bytecode = True` and `PYTHONDONTWRITEBYTECODE=1` at import time to prevent future `__pycache__` creation regardless of invocation context; (2) `install.sh` cleans up existing root-owned `__pycache__` directories in app venvs and the deploy user's tool install dir on every scaffold run; (3) the install helper and deployer now detect the `__pycache__` permission pattern and provide actionable advice in the error message.

## [0.16.0] - 2026-05-11

### Added

- **Parallel `pg_restore` via `restore.jobs`** ([#195](https://github.com/fraiseql/fraisier/issues/195)). Set `restore.jobs: N` in fraises.yaml or pass `--jobs N` on the CLI to run pg_restore with `-j N` for parallel restore. Default is 1 (no change in behavior).

- **Compression preference for backup selection** ([#195](https://github.com/fraiseql/fraisier/issues/195)). Set `restore.preferred_compression: lz4` (or `zstd`, `gzip`) to prefer backups compressed with a faster algorithm. Falls back to the newest backup if no match is found. Also pass `--preferred-compression` on the CLI.

- **Compression algorithm in backup filenames** ([#195](https://github.com/fraiseql/fraisier/issues/195)). `run_backup()` now encodes the compression algorithm in the dump filename (e.g. `mydb_full_20260511_0030_lz4.dump`), enabling compression-aware backup selection.

- **Restore timing observability** ([#195](https://github.com/fraiseql/fraisier/issues/195)). The restore pipeline now tracks per-phase timing (pg_restore, migration, total). Durations are logged, returned in `StrategyResult`, printed by the CLI after restore, and recorded in the `fraisier_restore_duration_seconds` Prometheus histogram.

## [0.15.0] - 2026-05-05

### Added

- **SSH dispatch for `history`, `rollback`, and `stats`** ([#194](https://github.com/fraiseql/fraisier/issues/194)). When an environment is SSH-configured, these commands now fetch data from the remote server's database instead of reading the local (nearly empty) database. The remote invocation is controlled by two new optional SSH config fields: `ssh.db_path` (sets `FRAISIER_DB_PATH` on the remote) and `ssh.fraisier_bin` (path to the remote executable, default `"fraisier"`).
- `stats` command gains `--env/-e` flag to filter by environment and `--json` flag for structured output.
- `webhooks` command prints a `Note: Showing local webhook events only.` notice to set expectations when SSH-configured environments exist.

## [0.14.3] - 2026-05-05

### Security

- **`fraisier-install-helper` accepted arbitrary commands with no allowlist** (adversarial review). Any caller with write access to the Unix socket could invoke any subprocess as the privileged deploy user. Fixed by baking the allowed command into the systemd unit at scaffold render time (`ExecStart=fraisier-install-helper uv sync --frozen`) and enforcing an exact-match check in the handler. Requests with a mismatched command are rejected with a structured error response.

- **Shell injection in `_regenerate_scaffold` and `_install_scaffold`** (adversarial review). Both methods built `sh -c "cd {project_dir} && {fraisier_exe} ..."` using f-strings with unquoted paths. A project directory or executable path containing shell metacharacters would be executed verbatim. Fixed by replacing the shell invocation with `runner.run([...], cwd=str(project_dir))`, eliminating the shell entirely.

- **`_DeliveryDedupe` webhook replay store was not thread-safe** (adversarial review). Concurrent webhook requests could race on `_store` dict mutation, causing missed deduplication or corruption. Fixed by adding a `threading.Lock()` around all read/write access.

### Fixed

- **`is_deployment_locked` had a TOCTOU race** (adversarial review). An `exists()` pre-check before `open()` could return `False` for a directory that was created between the check and the open, or `True` for a file deleted in the same window. Removed the pre-check entirely; `FileNotFoundError` from `open()` is now caught and returns `False`.

- **`mkstemp()` file descriptor leak in `status.py`** (adversarial review). `tempfile.mkstemp()` returns an OS-level fd that must be closed with `os.close()` before `Path.write_text()` opens the same path. Fixed by adding `os.close(_fd)` immediately after `mkstemp()`.

- **Remote tmp dir collision in `_upload_tree_with_password`** (adversarial review). The hardcoded path `/tmp/.fraisier-upload-tree` would be shared by concurrent uploads to the same host. Fixed by appending a 12-character random hex suffix (`uuid4`). Added try/except to clean up the remote dir if the tar step fails.

- **Jinja2 context mutation without cleanup in `_render_scaffold_install_helper`** (adversarial review). A `KeyError` or render failure after `self.context["scaffold_install_script"]` was set left the renderer in dirty state for subsequent calls. Fixed by wrapping the mutation in `try/finally` so the key is always deleted.

- **`systemctl enable --now` does not reload an already-running unit** (adversarial review). `install.sh` used `enable --now`, which starts a stopped unit but does not restart a running one after the unit file changes. Split into `systemctl enable` + `systemctl restart` so re-running install always picks up changes.

- **Starlette `QueryParams` private dict mutated in legacy webhook route** (adversarial review). The `github_webhook` compatibility endpoint set `request._query_params` to a plain `dict`, but Starlette's routing layer expects a `QueryParams` instance. Fixed by assigning `QueryParams("provider=github")`.

- **`_try_scaffold_install_via_socket` swallowed `json.JSONDecodeError` and `UnicodeDecodeError`** (adversarial review). A malformed response from the helper socket would propagate as an unhandled exception rather than triggering the sudo fallback. Added both error types to the `except` clause.

## [0.14.2] - 2026-05-05

### Fixed

- **`fraisier sync` aborts when pre-commit hooks modify auto-resolved files** ([#192](https://github.com/fraiseql/fraisier/issues/192)). The two internal bookkeeping commits made during a sync operation (`Pre-merge <tgt> into sync branch`) were invoked without `--no-verify`, so hooks such as `end-of-file-fixer` or `trailing-whitespace` could modify a resolved file and exit with code 1, causing fraisier to abort and clean up the branch. Fixed by adding `--no-verify` to both commit calls — these commits are fraisier internals, not user code, and must never trigger user-configured hooks.

## [0.14.1] - 2026-05-04

### Fixed

- **`confiture migrate preflight` fails with `FileNotFoundError` when confiture is installed in a venv** ([#190](https://github.com/fraiseql/fraisier/issues/190)). `_run_confiture_preflight` invoked `confiture` as a bare subprocess command, which requires the binary to be on `PATH`. When fraisier and confiture are installed together via `uv sync` (not `uv tool install confiture`), the binary lives in the venv's `bin/` directory rather than on `PATH`. Fixed by resolving the executable relative to `sys.executable` (`Path(sys.executable).parent / "confiture"`), which is always correct regardless of how the venv is activated.

## [0.14.0] - 2026-04-29

### Added

- **Migration preflight for `RestoreMigrateStrategy`** ([#187](https://github.com/fraiseql/fraisier/issues/187), [#188](https://github.com/fraiseql/fraisier/issues/188), [#189](https://github.com/fraiseql/fraisier/issues/189)). Before the expensive `pg_restore`, fraisier now optionally runs all pending migrations against a schema-only copy of the backup — using `confiture migrate preflight --against` (requires fraiseql-confiture ≥ 0.9.4). Each migration executes inside a SAVEPOINT that is always rolled back; the original database is never touched. A structured `MigrationPreflightResult` with per-migration pass/fail details is returned. On failure, a `MigrationPreflightError` (recoverable, code `MIGRATION_PREFLIGHT_FAILED`) is raised before any destructive operation begins.

  Key details:
  - Controlled via `preflight:` block in `fraises.yaml` (`enabled`, `timeout_seconds`); enabled by default for `restore_migrate` strategy.
  - `RestoreMigrateStrategy.execute()` gains a `skip_preflight=True` escape hatch.
  - New CLI command `fraisier db preflight <fraise> -e <env>` runs the check standalone with text or JSON output.
  - `PreflightConfig` replaces the old `preflight_enabled: bool` field on `RestoreConfig`.
  - Bumped `fraiseql-confiture` dependency to `>=0.9.4` to pick up `--against` support.

## [0.13.3] - 2026-04-29

### Fixed

- **Restore strategy fails when deploy user has no matching PostgreSQL database** ([#185](https://github.com/fraiseql/fraisier/issues/185)). `_pg_cmd` silently dropped the database path from `connection_url`, so `terminate_backends`, `drop_db`, `check_db_exists`, `create_db`, and `prune_templates` connected to a database named after the unix user instead of the maintenance database. Fixed by extracting the URL's database path and injecting `-d` (for psql/pg_restore) or `--maintenance-db` (for createdb/dropdb) when the caller hasn't already specified one. `admin_url` must now include an explicit maintenance database path (e.g. `/postgres`).

- **Resolved all `ty` type-checking errors** across `bootstrap_freebsd.py`, `service_managers/systemd.py`, and several test files.

## [0.13.2] - 2026-04-27

### Fixed

- **`trigger-deploy` path fails: `sudo -u <app_user> uv sync` cannot find `uv`** ([#184](https://github.com/evoludigit/fraisier/issues/184)). When the socket-activated install helper is unavailable, the fallback `sudo -u` path ran the install command with a bare binary name (e.g. `uv`). Since `sudo` resets `PATH`, per-user installations of `uv` (e.g. `~/.local/bin/uv`) were not found. Fixed by resolving the binary to an absolute path via `shutil.which()` before constructing the `sudo` command. Falls back gracefully to the bare name if resolution fails.

## [0.13.1] - 2026-04-27

### Fixed

- **`is_active()` crashes when service is already stopped (exit code 3)** ([#183](https://github.com/fraiseql/fraisier/issues/183)). `_call_via_socket()` raised `CalledProcessError` for any non-zero exit code, but `systemctl is-active` returns exit code 3 for inactive services. The `check` parameter was not forwarded to the socket path, so `status()` (which passes `check=False`) still crashed. Fixed by adding a `check` parameter to `_call_via_socket()` and forwarding it from `_run_systemctl()`. This unblocks all automated restore-migrate deployments when the target service happens to be stopped.

## [0.11.1] - 2026-04-21

### Fixed

- **Nginx config not regenerated when `fraises.yaml` scaffold configuration changed** ([#176](https://github.com/evoludigit/fraisier/issues/176)). The `ConfigWatcher.save_hash()` method was never called after scaffold regeneration, so the `.config_hash` file was never written. This caused either unnecessary regeneration on every deployment, or (if the hash file existed from a previous operation) changes were never detected. Additionally, scaffold install failures were silently logged as warnings, leaving nginx unreloaded with no visible indication of failure. Fixed by:
  - Calling `save_hash()` after successful scaffold regeneration + install to persist state across deployments
  - Upgrading error logging from `logger.warning` to `logger.error` with helpful hints for manual recovery
  - Removing `# pragma: no cover` annotations from scaffold methods so they're properly tested

## [0.11.0] - 2026-04-20

### Added

- **`--prefer-source` flag for `fraisier sync` command** — auto-resolves sync conflicts by preferring the source (upstream/target-branch) when the current branch is behind. Useful for unattended sync workflows where local changes should be discarded in favor of the upstream version.

## [0.10.0] - 2026-04-18

### Added

- **`fraisier db exec` — run read-only SQL on any fraise, locally or over SSH.** New CLI command and supporting `fraisier.dbops.exec` module providing:
  - `is_readonly_sql()` — strips leading comments and validates the first keyword against a safe allowlist (`SELECT`, `EXPLAIN`, `SHOW`, `WITH`, `TABLE`). Rejects all DDL/DML (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `CREATE`).
  - `build_psql_argv()` — constructs a `psql --no-psqlrc` invocation with `SET statement_timeout` injected, supporting `table` (default), `--csv`, and `--json` output formats.
  - Runs locally via `subprocess.run` when no `ssh` block is configured, or tunnels through `fraisier.ssh.short_cmd` when an SSH target is present — same code path, zero duplication.
  - Production safety gate: prompts for confirmation when `--env production` is passed.
  - `--timeout` (default 30 s) with validation (must be > 0).
  - `--file` flag to read SQL from a file; mutually exclusive with the inline SQL argument.
  - Accepts a plain database name or a full `postgresql://` URL from `database.admin_url`.

## [0.8.5] - 2026-04-17

### Fixed
- **`uv tool install --force` fails with Permission denied on __pycache__ in tool environment** (#167). Python creates root-owned `__pycache__/` directories inside the deploy user's tool installation when the systemctl helper runs as root, causing reinstalls to fail. Four service templates (deploy-service.j2, fraisier-webhook.service.j2, poll-deploy.service.j2, restore-staging.service.j2) now set `PYTHONDONTWRITEBYTECODE=1`, consistent with the existing fix in systemctl-helper.service.j2 and install-helper.service.j2.

## [0.8.4] - 2026-04-17

### Fixed
- **Restore strategy fails with "database already exists" on re-deploy** ([#166](https://github.com/evoludigit/fraisier/issues/166)). The `drop_db` return code was silently ignored: if `dropdb` failed (e.g. a connection reconnected in the window between `pg_terminate_backend` and the drop), execution continued to `createdb`, which then failed with a confusing "already exists" error. The return code is now checked and raises `DatabaseError` immediately. `dropdb` also uses `--if-exists` so a first deploy (database absent) is handled cleanly.

## [0.8.3] - 2026-04-14

### Fixed
- **Zombie uvicorn workers surviving service restart** ([#165](https://github.com/evoludigit/fraisier/issues/165)). Generated systemd units now include `KillMode=control-group` and `TimeoutStopSec=10`, ensuring all worker processes are terminated on stop within a bounded window. Previously, orphaned workers could hold the listening port and cause `Address already in use` crash loops on fast restarts.
- **Default `service.type` changed from `notify` to `exec`.** uvicorn does not implement `sd_notify`, so `Type=notify` confused systemd's lifecycle tracking during stop. `Type=exec` reflects actual server behavior. Existing configs that explicitly set `service.type` are unaffected; configs relying on the default will now get `Type=exec`.

## [0.8.2] - 2026-04-11

### Fixed
- `install-helper` systemd template now allows `AF_INET`/`AF_INET6` so `uv sync` can reach PyPI when the local cache is cold.

## [0.8.1] - 2026-04-11

### Security
- SQL identifier validation in `strategies._provision_roles` — role and database names are now validated against an allowlist before being interpolated into SQL.
- Webhook replay protection via `X-GitHub-Delivery` deduplication — duplicate delivery IDs within a sliding window are rejected.

### Improved
- Replaced 10 unnamed `except Exception:` clauses with named, narrowed handlers across CLI, config, and strategies modules.
- Health check failures now include the check name in log output for easier diagnosis.
- Type annotations in refactored modules (`cli/`, `config/`, `strategies/`) and `webhook.py` — `Any` usage reduced from 52 to ≤20 files.

### Refactored
- `cli/main.py`, `strategies.py`, and `config/loader.py` split into focused sub-modules.
- Timeout constants centralized in `fraisier/constants.py`.

### Tests
- Coverage extended to the CLI module; unjustified `# pragma: no cover` markers removed.

### Known follow-up
- ~100 `except Exception as e:` broad handlers across `strategies/`, `providers/`, `deployers/`, and `health_check.py` still need a focused audit. Deferred to a future remediation round.

## [0.8.0] - 2026-04-11

### Changed — Breaking

- **Privilege model collapsed to `admin_url` only.** Strategies that perform privileged database operations (`rebuild`, `restore_migrate`) now require a superuser connection string in `database.admin_url`. Fraisier no longer falls back to `sudo -u postgres`, generates a `pg-wrapper.sh` script, emits a `(postgres) NOPASSWD:` sudoers rule, or injects `FRAISIER_PG_WRAPPER` into the generated systemd units. The runtime probes that previously tried to detect a broken sudo+`NoNewPrivileges=true` combination are gone — there is no sudo path left to probe. `validate-deployment` / `validate-remote` check only the systemctl wrapper. `fraisier test-wrapper` only accepts `systemctl` as its wrapper type.

  **Migration.** Set `database.admin_url` on every environment using `rebuild` or `restore_migrate`. The recommended form is peer-auth over the Unix socket:

  ```yaml
  database:
    strategy: rebuild
    admin_url: "postgresql:///postgres?host=/var/run/postgresql"
  ```

  Or via environment variable:

  ```yaml
  database:
    strategy: rebuild
    admin_url: "${PG_ADMIN_URL}"
  ```

  **Backups are unaffected.** Backups now run `pg_dump` against the fraise's own `database_url` instead of `sudo -u postgres`. `pg_dump` only needs SELECT on the app's own tables, which `database_url` already provides. No action required if `database_url` is already set — and it is, for every fraise with a database.

  **Post-upgrade cleanup.** After re-running `fraisier scaffold` and `fraisier scaffold-install`, the `pg-wrapper` script and its sudoers rule are no longer generated, but the old files remain on disk. Remove them to close the residual privilege:

  ```bash
  sudo rm -f /usr/local/libexec/fraisier/pgadmin-<project>
  sudo rm -f /etc/sudoers.d/<project>-pg-wrapper
  ```

  (Substitute `<project>` with your scaffold `project_name`.)

### Fixed

- **Unified SSH invocation path.** `runners.py` SSH commands now include the same `ConnectTimeout` and `BatchMode` defaults as `fraisier logs`, fixing a latent hang on IPv6-only networks. All subprocess-based SSH calls now route through `fraisier.ssh`, which applies the full defensive flag set (`BatchMode=yes`, `ConnectTimeout=30`, `StrictHostKeyChecking=accept-new`, `-n` where appropriate) by construction.

---

## [0.7.12] - 2026-04-10

### Fixed

- **Issue #156: bootstrap restarts the webhook service after a fraisier self-upgrade** — `fraisier bootstrap` now runs a `systemctl restart fraisier-{project}-webhook.service` step immediately after upgrading fraisier, so the new binary takes effect without manual intervention. If the service is not yet running (fresh install), the step is a no-op.

---

## [0.7.11] - 2026-04-10

### Fixed

- **Issue #153: deploy daemon no longer errors on config sync when `/opt/fraisier/` is absent** — `_sync_fraises_yaml` now runs `mkdir -p` on the destination directory before `cp`, so the first deployment on a freshly bootstrapped server succeeds silently instead of logging a recurring "Config sync failed" warning.

---

## [0.7.5] - 2026-04-10

### Fixed

- **`fraisier logs` works correctly from background processes and scripts** — Replaced `os.execvp` with `subprocess.Popen` (inherited stdin/stdout/stderr). TTY behaviour is identical for interactive use (colours, terminal size, Ctrl-C), but the Python process now stays alive so background runners and scripts can track the PID properly.
- **`fraisier trigger-deploy --follow` was silently broken** — The `--follow` path imported `_resolve_deploy_unit_pattern` by its old name after the #154 refactor, causing a silent `ImportError` and never exec-ing into journalctl. Fixed by reusing the `socket_stem` already computed just above the call site.

---

## [0.7.4] - 2026-04-10

### Added

- **Issue #154: `fraisier logs` fetches logs from remote servers** — `fraisier logs <fraise> <env>` now detects whether the target environment is remote (has an `ssh:` block in its config) and SSHs to the target server to run `journalctl` there instead of querying the local machine. `os.execvp` is used so the TTY is inherited and `--follow` / Ctrl-C work correctly over SSH. A new `--service [app|deploy]` option (default: `deploy`) lets operators switch between the deploy-daemon template instances and the main app service. Unit pattern generation now uses the same naming logic as the scaffold (`deploy_socket_name` / `app_service_name`), so patterns always match installed units. `app_service_name()` is extracted to `fraisier.naming` as the single source of truth; the scaffold renderer delegates to it. The `ssh:` block is now validated by the config loader (`host` required, typed fields enforced).

---

## [0.7.3] - 2026-04-10

### Fixed

- **Issue #151: `fraisier sync` auto-resolves file deletions from the source branch** — When the source branch has deleted a file that still exists in the target branch, the sync now detects the deletion (via `git cat-file -e origin/{source}:{file}`) and accepts it with `git rm`, instead of halting with an unresolved conflict error. A log line is printed for each auto-resolved deletion. Conflicts where both sides modified the same file are still surfaced to the user.

---

## [0.7.2] - 2026-04-09

### Fixed

- **Issue #149: stale deploy socket/service files not cleaned up on re-scaffold** — `ScaffoldRenderer` now removes `fraisier-*.socket` and `fraisier-*@.service` files from the `systemd/` output directory that are no longer part of the current render set (e.g. after a fraise rename or the 0.7.1 socket naming change).
- **Issue #150: install.sh migrates pre-0.7.1 generic deploy socket units** — `install.sh` now detects and removes stale `fraisier-{env}.socket` / `fraisier-{env}@.service` / `fraisier-{env}.service` units left by older scaffold versions before installing the new fraise-specific units, preventing duplicate or conflicting unit names on the server.

---

## [0.7.1] - 2026-04-09

### Fixed

- **Issue: deploy socket name collision with multiple fraises** — `deploy_socket_name()` now includes the fraise name in the default unit name: `fraisier-{fraise_name}-{env_key}.socket`. Previously, multiple fraises sharing the same env key (e.g. `production`) generated the same socket unit name, causing last-write-wins overwrites. Resolution order: `systemd_deploy_socket` override → `fraisier-{env.name}.socket` (if `env.name` set) → `fraisier-{fraise}-{env}.socket` (new default) → `fraisier-{env}.socket` (legacy fallback).

---

## [0.7.0] - 2026-04-09

### Added

- **`fraisier sync` CLI command (#140)** — Replaces the `sync.sh` scaffold template. `fraisier sync [pair]` fetches the source branch, detects drift, creates a squash sync branch, auto-resolves fraisier-owned files on merge conflict, and opens a PR with auto-merge enabled. Supports `--list`, `--check` (drift detection only), `--dry-run`, and `--yes` flags.

### Fixed

- **Issue #148: nginx configs always generated for all servers** — `fraisier scaffold` (with or without `--server`) now generates per-environment nginx configs for every server, not just the local one. `scripts/generated/nginx/` is always a complete, committable artifact; each server's `install.sh` uses `_env_active()` to install only its own configs.
- **Issue #147: CORS wildcard regex anchored and dot-escaped** — Wildcard patterns (e.g. `https://*.example.com`) now produce correctly anchored nginx regexes: dots in the domain are escaped before `*` is expanded, and `^`/`$` anchors are added.
- **Issue #145: legacy rate_limit.conf removed before nginx reload** — `install.sh` now deletes `/etc/nginx/conf.d/rate_limit.conf` before reloading nginx when `gateway.conf` owns `limit_req_zone`, preventing duplicate-directive errors on servers upgraded from older fraisier versions.
- **Issue #144: install.sh SCAFFOLD_DIR always derived from script location** — The `SCAFFOLD_DIR` default no longer depends on the `STANDALONE` flag; it is always inferred from the script's own path, fixing silent no-ops when run outside the standard directory.

### Removed

- **`sync.sh.j2` scaffold template** — Replaced by the `fraisier sync` CLI command. Projects that referenced a generated `sync.sh` should switch to `fraisier sync`.

---

## [0.6.0] - 2026-04-08

### Breaking Changes

- **CORS origins configuration format changed** — `cors_origins` now requires structured objects. Plain strings are no longer accepted. Update `["https://*.example.com", "https://api.example.com"]` to `[{"pattern": "https://*.example.com", "type": "wildcard"}, {"pattern": "https://api.example.com", "type": "literal"}]`.

### Added

- **Structured CORS origins with explicit types** — `cors_origins` entries now support:
  - `"type": "literal"` — Escapes dots for nginx regex (e.g., `https://api.example.com` → `~https://api\.example\.com`)
  - `"type": "wildcard"` — Converts `*` to subdomain regex (e.g., `*.example.com` → `~https://[a-zA-Z0-9-]+\.example\.com`)
  - `"type": "regex"` — Raw nginx regex without processing (e.g., `^https://.*\.custom\.com$`)
- **Conditional WatchdogSec support** — `service.watchdog_sec` field enables `WatchdogSec` in systemd service units for servers that support `sd_notify` (e.g., gunicorn). Defaults to disabled for uvicorn compatibility.
- **Server type configuration** — `service.server_type` field (`"uvicorn"` or `"gunicorn"`) for future server-specific optimizations.

- **Socket-based install helper (#124)** — New `fraisier-install-helper` daemon (socket + service systemd units) runs as `install_user`, accepts install commands over a Unix socket owned by `deploy_user`. Eliminates the `sudo -u` dependency at deploy time. `fraisier-webhook` emits `FRAISIER_INSTALL_SOCKET_*` env vars; `install.sh` installs and enables the units automatically.
- **Scaffold sync promotion scripts (#139)** — New `sync.sh` scaffold template generates a git-based promotion script per `scaffold.sync` pair defined in `fraises.yaml`. The script fetches, detects drift, creates a sync branch, pre-merges the target, auto-resolves fraisier-owned files, and opens a PR via `gh`.
- **Stale service file warning at deploy time (#142)** — `fraisier deploy` now warns when the live systemd service file differs from what fraisier would generate, prompting a `fraisier scaffold` run before the next deploy.

### Fixed

- **Issue #138: nginx CORS map supports wildcard subdomain patterns** — Wildcard patterns like `https://*.example.com` now generate proper nginx regex patterns.
- **Issue #137: WatchdogSec properly omitted for non-sd_notify servers** — WatchdogSec is now conditionally included only when configured, preventing uvicorn services from being killed every 60 seconds.
- **Issue #127: gateway.conf spurious SSL catch-all block suppressed** — `server_name` is now aggregated across per-environment nginx blocks when not set at the fraise level, so `has_server_names` is correctly `True` and the `server_name _` SSL catch-all is omitted.

---

## [0.5.28] - 2026-04-07

### Added

- **`fraisier health --width` option** — Table now expands to terminal width by default (`os.get_terminal_size()`), eliminating column truncation. Pass `--width N` to override. Falls back to 120 when terminal size is unavailable (e.g. CI/piped output).

## [0.5.27] - 2026-04-07

### Added

- **Custom per-environment health endpoints with headers and field mappings (#136)** — `fraisier health` now uses the full `health_check.url` from each environment config as-is (no path stripping). Optional `health_check.headers` dict allows authenticated endpoints. Optional per-environment `version_field` and `migration_field` override the global defaults with dot-notation path support (e.g. `versions.app`). Falls back to global `health.version_field` / `health.migration_field` when not set per-environment.

### Fixed

- **pytest-asyncio missing from dev dependency group** — `pytest-asyncio` was declared in `[project.optional-dependencies]` but not in `[dependency-groups]`, causing async tests to fail silently after `uv sync`.

## [0.5.25] - 2026-04-07

### Fixed

- **fraisier health only shows last environment when multiple environments share the same fraise name (#130)** — The services dict used `fraise_name` as key, causing overwrites for each environment iteration. Changed to use composite key `f"{fraise_name}-{env_name}"` so all environments appear in the health table.

- **fraisier health URL column strips scheme for remote services (#131)** — The URL display logic incorrectly split on `:` and took the last part, dropping `https://` for remote URLs. Now displays the full base URL.

- **fraisier health table lacks Environment column (#132)** — The table merged fraise name and environment into a single Service column. Added a dedicated Environment column for clarity, with Service showing only the fraise name.

- **fraisier health does not display migration number** — Added support for parsing and displaying migration information from health endpoint responses. Added `include_migration` config option and Migration column in health table.

## [0.5.24] - 2026-04-07

### Added

- **`fraisier db restore` command (#129)** — Expose the staging restore workflow
  (pg_restore → rollback template → migrate → validate) as a user-facing command. The
  nightly `restore-staging-from-production-backup.service` systemd unit previously called
  a broken `trigger-deploy` path and did nothing useful; it now calls
  `fraisier db restore api staging` and works correctly. Supports `--from-backup` to restore
  from a specific dump file (useful for testing), `--dry-run` to preview the plan, and
  `--no-service-restart` to skip service management if needed.

- **Scaffold templates for staging restore** — Generated `restore-staging.service` and
  `restore-staging.timer` systemd units for nightly restores. Auto-discovers fraises with
  `restore_migrate` strategy and generates correct `fraisier db restore` commands.
  When running `fraisier scaffold-install --apply` on existing servers, the broken service
  unit is automatically replaced with the corrected one.
  Conditionally rendered only when `restore_migrate` is configured to avoid invalid
  systemd units in configs without staging restore.

- **CLI consistency** — All fraise database commands now use positional `ENVIRONMENT`
  arguments instead of flags (e.g., `fraisier db restore api staging` instead of
  `fraisier db restore api -e staging`), matching the pattern of `trigger-deploy`, `logs`,
  and other top-level commands.

### Fixed

- **Server-scoped scaffold collectors** — `pg_allowed_databases`, `allowed_services`, and
  `has_database` now use server-filtered fraises (`local_fraises`) instead of the unfiltered
  global list. Previously, rendering with `--server` included databases and services from
  all servers, causing cross-server leakage in generated pg-wrapper and systemctl-wrapper
  allowlists.

- **ReadWritePaths use configured `app_path`** — `restore-staging.service` and
  `poll-deploy.service` templates now derive `ReadWritePaths` from the actual `app_path`
  in environment config instead of hardcoded `/opt/<fraise_name>`. restore-staging further
  restricts paths to environments with `restore_migrate` strategy.

- **Socket-based systemctl for restore and poll-deploy** — `restore-staging.service` and
  `poll-deploy.service` now use `FRAISIER_SYSTEMCTL_SOCKET` (the socket-activated helper
  running as root) instead of the legacy `FRAISIER_SYSTEMCTL_WRAPPER` which lacked
  privilege escalation inside systemd's security namespace.

- **CWD-independent `db restore` migrations** — Relative `confiture_config` paths (default:
  `confiture.yaml`) are now resolved against `app_path` before being passed to confiture.
  Previously, migrations failed when the service's working directory was not the app
  directory, because confiture could not find `db/environments/<env>.yaml` relative to CWD.

- **Deduplicate ReadWritePaths in service templates** — `poll-deploy.service` and
  `restore-staging.service` now emit each `app_path` once even when multiple fraises
  share the same path on a server.

- **Deduplicate sudoers pg-wrapper rules** — The sudoers fragment now emits each
  `(user, pg-wrapper)` rule once regardless of how many environments use admin
  strategies. Also scoped to `local_fraises` to avoid cross-server leakage.

---

## [0.5.17] - 2026-04-06

### Fixed

- **Multi-server `install.sh` silent overwrites (#125)** — Running `fraisier scaffold --server A`
  then `--server B` silently overwrote `install.sh` with B's config, corrupting deployments on
  server A. The problem occurred because `install.sh` was rendered per-server at scaffold time,
  and successive runs overwrote the same output file. Fixed by adding runtime server detection:
  `install.sh` now detects the current machine's hostname at startup and conditionally installs
  only the units relevant to that machine. Requires new `servers:` section in `fraises.yaml`
  mapping logical server hostnames to machine hostnames (e.g., `prod.example.com` → 
  `[backend-prod-01, backend-prod-02]`). Single `install.sh` now works across the entire cluster
  with zero ambiguity.

---

## [0.5.16] - 2026-04-06

### Fixed

- **`poll-deploy.service.j2` deploys wrong fraise on wrong environment** — The service
  hardcoded `{{ fraise_names | first }} production`, so only the first fraise was polled and
  the environment was always `production` regardless of which server the unit ran on. The
  service now loops over `local_fraises` (server-filtered) and generates one `ExecStart`
  line per (fraise, environment) pair. The binary path was also wrong (`/usr/local/bin/fraisier`
  instead of the deploy user's uv-tool path) and is now consistent with all other templates.

- **`ScaffoldConfig` missing `socket_user`/`socket_group` fields** — The deploy-socket
  template referenced `scaffold.socket_user` and `scaffold.socket_group` via Jinja
  `|default('www-data')` filters, making them silently unconfigurable. Both fields are now
  declared in `ScaffoldConfig` with `"www-data"` as the default.

- **Bootstrap uploads scaffold without checking for placeholder files** — When a template
  file is missing from the package, `ScaffoldRenderer` writes a `# Placeholder:` stub. The
  bootstrap step now inspects each rendered file and fails early with a clear error rather
  than uploading broken stubs that produce confusing failures on the remote server.

- **Config module refactor lint cleanup** — The `config/` package re-export module had
  stale unused imports (`_config`, `_config_lock` without `noqa`), an unsorted `__all__`,
  and test files had `noqa` directives that were no longer needed after adding `F401` to the
  test ignore list. All cleaned up.

---

## [0.5.15] - 2026-04-06

### Fixed

- **Watchdog kills uvicorn-based services every 60 seconds** — `service.j2` unconditionally
  added `WatchdogSec=60` but uvicorn does not implement `sd_notify` keepalive pings, so
  systemd would kill every instance exactly 60 seconds after startup. Removed `WatchdogSec`
  from the template (the fraisier-webhook template already omitted it with an explanatory
  comment for the same reason).

---

## [0.5.14] - 2026-04-06

### Fixed

- **Bootstrap installs stale fraisier version due to uv index metadata cache** — `uv tool install
  --force` reinstalls but uses cached PyPI index metadata, so newly published versions are invisible
  until the cache expires. Added `--refresh-package fraisier` to the bootstrap install command so
  the index is always refreshed when bootstrapping, ensuring the server receives the exact client
  version.

---

## [0.5.13] - 2026-04-06

### Fixed

- **`uv tool install --force` fails with "Permission denied" on reinstall** — the systemctl
  helper service runs as root and causes Python to create root-owned `__pycache__/`
  directories inside the deploy user's fraisier tool installation. When uv later tries to
  force-reinstall fraisier it cannot remove those root-owned directories. Added
  `Environment=PYTHONDONTWRITEBYTECODE=1` to the helper service so Python never writes
  bytecode cache files, keeping the tool directory entirely owned by the deploy user.

---

## [0.5.12] - 2026-04-06

### Fixed

- **`/run/fraisier/` still root-owned: `systemctl-helper.socket` had `RuntimeDirectory=fraisier`** —
  the socket unit (which runs as root via `SocketUser=root`) also declared `RuntimeDirectory=fraisier`
  with `RuntimeDirectoryMode=0755`. On every socket start/restart, systemd recreated `/run/fraisier/`
  owned by root, overwriting the `printoptim_deploy`-owned directory managed by the webhook service.
  Removed `RuntimeDirectory` and `RuntimeDirectoryMode` from the socket unit; `/run/fraisier/` is
  now managed exclusively by the webhook service.

---

## [0.5.11] - 2026-04-06

### Fixed

- **`/run/fraisier/` still wiped during v0.5.7 → v0.5.10 upgrade** — the previous fix
  stopped the helper service *before* daemon-reload, which meant systemd used the OLD
  unit (which had `RuntimeDirectory=fraisier`) for the stop phase and still wiped the
  directory. The correct approach is to copy the new unit files, run `daemon-reload`
  first (so systemd loads the new unit without `RuntimeDirectory`), and *then* restart
  the service — the stop phase now uses the new unit and `/run/fraisier/` is preserved.

---

## [0.5.10] - 2026-04-06

### Fixed

- **`/run/fraisier/` wiped during upgrade from v0.5.7 → v0.5.9** — `install.sh` now
  explicitly stops the old helper service and socket *before* copying the new unit files
  and running `daemon-reload`. This prevents the old helper service (which had
  `RuntimeDirectory=fraisier`) from wiping `/run/fraisier/` at daemon-reload time. After
  the new files are in place, deploy sockets are restarted to ensure their socket files
  exist, then the helper socket is started.

---

## [0.5.9] - 2026-04-06

### Fixed

- **Every deployment wiped `/run/fraisier/` and destroyed all socket files** — the
  `systemctl-helper.service` template included `RuntimeDirectory=fraisier` (and
  `RuntimeDirectoryMode=0755`). Since the helper runs as root, systemd recreated
  `/run/fraisier/` as a root-owned directory on every activation, wiping the deploy
  socket files and the helper socket file itself. Removed `RuntimeDirectory` from the
  helper service; `/run/fraisier/` is now managed exclusively by the webhook service
  (which uses `RuntimeDirectoryPreserve=restart`).

---

## [0.5.8] - 2026-04-06

### Fixed

- **systemctl helper socket file missing after webhook restart** — `install.sh` installed the
  helper service and socket unit files but never restarted the socket, so the socket file in
  `/run/fraisier/` was never (re)created. Added `systemctl restart fraisier-{project}-systemctl-helper.socket`
  after the install step, mirroring the pattern used for deploy socket units.

---

## [0.5.7] - 2026-04-06

### Fixed

- **Duplicate `ReadWritePaths` in generated deploy service units** — the `deploy-service.j2`
  template had hardcoded `ReadWritePaths=/run/fraisier` and `ReadWritePaths={{ scaffold.config_dir }}`
  entries below the manifest loop. After the v0.5.6 fix those same paths are now emitted by the
  manifest loop, producing duplicates. The hardcoded lines have been removed; the manifest is now
  the sole source of all `ReadWritePaths` entries.

---

## [0.5.6] - 2026-04-06

### Fixed

- **Deploy service missing `ReadWritePaths` for `/opt/fraisier`, `/var/lib/fraisier`, `/run/fraisier`** —
  `build_manifest()` was collecting deploy socket stems *after* building the global paths, so the deploy
  service unit stems were absent from `read_write_units` on the three shared fraisier directories.
  Stems are now collected first and included in `shared_rw_units` used by all global paths.
- **`GIT_SSH_COMMAND` env assignment ignored by systemd** — the unquoted value
  `Environment=GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new` caused systemd to log
  `Invalid environment assignment, ignoring: -o` (spaces split the value into separate tokens).
  Fixed to `Environment="GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new"`.

---

## [0.5.5] - 2026-04-06

### Fixed

- **Deploy socket files not recreated after first-time webhook install** — `install.sh` now
  restarts all local deploy socket units after the webhook service restart, ensuring their
  socket files are always present in `/run/fraisier/` for `validate-setup` to find.

---

## [0.5.4] - 2026-04-06

### Fixed

- **Deploy socket files wiped on webhook restart** — the webhook service's `RuntimeDirectory=fraisier`
  caused systemd to remove and recreate `/run/fraisier/` on service restart, destroying socket
  files created by co-located deploy socket units. Added `RuntimeDirectoryPreserve=restart` so
  `/run/fraisier/` and its contents survive webhook restarts (still cleaned on full stop/reboot).

---

## [0.5.3] - 2026-04-06

### Fixed

- **Webhook service unit not installed by `install.sh`** — `install.sh` installed the
  systemctl helper, sudoers, app units, and deploy socket units, but never the
  `fraisier-{project}-webhook.service` file itself. Upgrades to webhook configuration
  (e.g. `FRAISIER_SYSTEMCTL_SOCKET`, `NoNewPrivileges`, `ReadWritePaths`) were silently
  not applied on re-bootstrap. Now copies the webhook service file and restarts it.
- **`fraisier-{project}-webhook.service` added to `get_install_mapping()`** so
  `scaffold-diff` correctly detects divergence between generated and installed webhook units.

---

## [0.5.2] - 2026-04-06

### Fixed

- **sudoers install rule not generated when `install.user` is defined at fraise level** —
  `_collect_deduplicated_sudoers_rules` only checked `env_config` for the `install` key,
  missing the common pattern where `install` is declared once at the fraise level and
  inherited by all environments. Now falls back to fraise-level install config.

---

## [0.5.1] - 2026-04-06

### Breaking Changes

- Scaffold output changed: regenerate and reinstall all systemd service units after upgrading.
  Generated webhook and deploy-daemon services now use `FRAISIER_SYSTEMCTL_SOCKET` instead
  of `FRAISIER_SYSTEMCTL_WRAPPER`. The generated sudoers fragment no longer contains
  a systemctl-wrapper NOPASSWD entry.

### Added

- **`fraisier-systemctl-helper`** — root-privileged Unix socket helper for service management.
  Replaces the sudo/wrapper mechanism entirely. Runs as root under systemd socket activation,
  validates commands against an allowlist baked into `ExecStart` args, calls `systemctl`
  directly. `NoNewPrivileges=true` on all services including the helper itself.
- New scaffold templates: `systemctl-helper.service.j2`, `systemctl-helper.socket.j2`.
  Generated as `fraisier-{project}-systemctl-helper.{service,socket}`, installed to
  `/etc/systemd/system/` by `install.sh`, socket enabled on first install.
- `FRAISIER_SYSTEMCTL_SOCKET` environment variable in webhook and deploy-daemon service units —
  points to `/run/fraisier/systemctl-{project}.sock`.
- `resolver.py`: new `systemctl_socket` property.

### Fixed

- **#123: `NoNewPrivileges=true` blocked sudo in webhook service** — sudo is no longer used
  for service management; all services keep `NoNewPrivileges=true`.
- **#122: wrapper called without sudo** — the wrapper mechanism is superseded; the new helper
  requires no sudo at all.

### Removed

- systemctl NOPASSWD block from generated `sudoers` fragment (no longer needed).

---

## [0.5.0] - 2026-04-06

### Breaking Changes

- Scaffold output changed: regenerate and reinstall `install.sh` and all systemd service
  units after upgrading. Generated files are now derived from the path ownership manifest
  rather than per-concern template logic.

### Added

- **PathManifest** — declarative, typed registry of every filesystem path fraisier manages,
  with ownership, permissions, and systemd unit bindings. Replaces scattered path logic.
- **ConfigResolver** — single source of truth for environment variable overrides;
  replaces scattered `os.getenv` calls across the codebase.
- `validate-remote` path checks are now manifest-driven; new managed paths get validation
  and repair automatically without handwritten checkers.

### Fixed

- **#121: `.venv` owned by the wrong user** — now deleted and recreated by the install step
  instead of failing with a non-root `chown` error.
- **ReadWritePaths in generated service units** — now complete and derived from the manifest;
  no more per-bug additions.

### Removed

- Runtime `chown` call in `_install_dependencies` (replaced by manifest-driven delete-and-recreate).
- Hardcoded path-ownership checks in `remote_validator.py` (replaced by manifest loop).
- Scattered `os.getenv` calls outside `resolver.py` and `_env.py`.

---

## [0.4.18] - 2026-04-05

### Fixed
- **Cryptic permission error when `deploy-daemon` runs as wrong user** (#67) — instead of a
  bare `PermissionError` on the lock file, fraisier now checks the current user against the
  configured `deploy_user` before acquiring the lock and emits a clear error with a suggested
  `sudo -u <deploy_user>` command.

---

## [0.4.17] - 2026-04-05

### Fixed
- **Per-env nginx configs had wrong filename and were not installed** (#110) — files were named
  `{project}_{fraise}_{env}.conf` instead of `{server_name}.conf`, mismatching nginx conventions.
  The renderer now uses `nginx_config.server_name` as the filename stem. The generated `install.sh`
  now also copies and symlinks each per-env config into `/etc/nginx/sites-available/` and
  `/etc/nginx/sites-enabled/`, then reloads nginx — previously only `gateway.conf` was installed.

---

## [0.4.14] - 2026-04-04

### Fixed
- **Bootstrap double-sudo fails silently for steps 4 and 10** (#104) — when using `--sudo`
  with a non-root SSH user, the outer `sudo -S` consumed the password from stdin, leaving the
  inner `sudo -u <deploy_user>` with no stdin to read from. Steps 3, 4, and 10 now use
  `sudo -n -u` (non-interactive), which prevents any stdin read since root needs no password
  to switch users.
- **Bootstrap step 4 installs wrong fraisier version on server** (#103) — `_install_fraisier`
  read the version from the hardcoded `__init__.__version__` string, which could be stale
  relative to `pyproject.toml`. It now uses `importlib.metadata.version("fraisier")`, which
  always reflects the actually-installed package version.

---

## [0.4.13] - 2026-04-04

### Fixed
- **`validate-setup` and `deploy` used old socket path pattern** — both commands were
  building the socket directory as `/run/fraisier/{project_name}-{environment}` instead of
  using `deploy_socket_name()`, causing them to look in the wrong place after the naming
  overhaul in v0.4.9.
- **`_check_systemd_units` used a completely wrong unit name** — it was constructing
  `fraisier-{project}-{environment}-deploy.socket`, a pattern that never existed. It now
  receives the unit name directly from the caller via `deploy_socket_name()`.

---

## [0.4.12] - 2026-04-04

### Fixed
- **Bootstrap step 4 always pins server-side fraisier to the client version** (#101) — `uv tool install`
  was skipping the install if fraisier was already present on the server, leaving a stale version.
  It now runs `uv tool install --force fraisier==<client_version>` unconditionally, ensuring the
  server and client are always in sync.

---

## [0.4.10] - 2026-04-04

### Fixed
- **Bootstrap step 10 validates each fraise individually** (#98) — `validate-setup` requires
  a positional `FRAISE` argument; the bootstrap command was calling it without one. It now
  iterates all fraises configured for the target environment, calling `validate-setup <fraise>`
  once per fraise and failing fast on the first error.

---

## [0.4.9] - 2026-04-04

### Added
- **`fraisier/naming.py`** — new `deploy_socket_name(env_config, env_key)` helper as the
  single source of truth for deploy socket unit names. Resolves in order:
  1. explicit `systemd_deploy_socket` field in environment config
  2. `fraisier-{env.name}.socket` derived from the environment's `name` field
  3. `fraisier-{env_key}.socket` derived from the environment dict key
- **`systemd_deploy_socket` config field** — optional per-environment override for the
  deploy socket unit name (validated against the same regex as `systemd_service`)

### Changed
- **Deploy socket unit names** now derived from the environment `name` field (e.g.
  `fraisier-api.myapp.io.socket`) instead of the verbose
  `fraisier-{project}-{fraise}-{env}-deploy.socket` pattern
- **`ListenStream` socket path** in generated units updated to match the new naming
  (`/run/fraisier/{socket_stem}/deploy.sock`)
- **`fraisier/scaffold/renderer.py`** — all consumers call `deploy_socket_name()`;
  `socket_unit_name` and `socket_stem` added to socket/service template contexts;
  `deploy_socket_name` registered as a Jinja2 global for use in templates
- **`fraisier/scaffold/diff.py`** — filter logic replaced: pre-computes the set of
  matching deploy unit paths from config instead of parsing filenames with a regex
- **`fraisier/bootstrap.py`** — `_enable_sockets()` now iterates all fraises for the
  target environment and enables each socket; returns a clear error if no fraises match
- **`fraisier/cli/main.py`** — `diagnose` command derives `socket_unit` and `socket_path`
  via `deploy_socket_name()`

### Fixed
- **Bootstrap enabled the wrong socket unit** (#95) — bootstrap was building
  `fraisier-{project}-{env}-deploy.socket`, missing the fraise name, causing
  `systemctl enable` to fail. Fixed as a side effect of centralising naming in #96.

---

## [0.4.3] - 2026-04-03

### Added

#### CLI Enhancements
- **New `fraisier logs <fraise> <env>` command** - Tail systemd journal logs for deploy daemons
  - Supports `--no-follow`, `--lines N`, `--since` options
  - Automatically resolves unit patterns from configuration
  - Uses `os.execvp` for proper signal handling

- **Enhanced `fraisier history` command** - Improved deployment history viewing
  - Positional arguments: `fraisier history <fraise> <env>`
  - `--json` flag for structured output
  - `--since` filtering with relative time parsing (7d, 24h) and ISO dates
  - Enhanced table with SHA (truncated) and "Triggered By" columns
  - Better duration formatting with time units

- **New `fraisier scaffold-diff` command** - Detect infrastructure drift
  - Compare generated scaffold files against installed system files
  - Unified diff output with file-level summaries
  - Supports filtering by fraise/environment
  - `--apply` flag for automatic re-installation (future enhancement)

- **Enhanced `fraisier ship` command** - Post-deployment verification
  - `--wait-deploy` flag polls health endpoint after shipping
  - `--deploy-timeout` option controls verification timeout (default 300s)
  - Shows progress during health polling with elapsed time
  - Supports multiple health endpoint version field names

- **Enhanced `fraisier trigger-deploy` command** - Synchronous execution
  - `--wait` flag blocks until deployment completion
  - `--follow` flag streams deployment logs in real-time
  - JSON result parsing from daemon responses

#### Daemon & Status Improvements
- **Enhanced deployment status display** - Real-time deployment visibility
  - `fraisier status` shows deploying/pending/failed states with elapsed time
  - Status file reading prioritizes over version comparison
  - Added "pending" state to deployment lifecycle

- **Improved daemon error diagnostics** - Better troubleshooting
  - Config search path display when config files not found
  - Project availability hints when fraise not in configuration
  - File existence checks in search locations

- **Rollback enhancements** - Safer rollback operations
  - `--dry-run` shows rollback plan without executing
  - Improved target resolution finds most recent successful deployment
  - Safety limit prevents rollback beyond 10 deployments (unless --force)
  - Better output with current vs target version display

#### Internal Improvements
- **Health polling system** - `fraisier.ship.health_poll` module
  - Robust HTTP polling with configurable timeout/intervals
  - Version extraction from multiple health endpoint field names
  - Progress display during long-running health checks

- **Scaffold drift detection** - `fraisier.scaffold.diff` module
  - File comparison with unified diff generation
  - Install path mapping from scaffold to system locations
  - Support for systemd, nginx, and sudoers file types

- **Enhanced status file management**
  - Daemon writes status updates during deployment lifecycle
  - Atomic status file operations with proper error handling

### Changed
- **Daemon result output** - Now writes JSON results to stdout for socket clients
- **Status computation** - Prefers status file data over version comparison
- **Rollback target selection** - Uses smarter algorithm for finding rollback targets

### Fixed
- **Config validation** - Better error messages for missing fraises/environments
- **Socket communication** - Improved response parsing in trigger-deploy

### Technical Details
- **8 major features** implemented across multiple phases
- **15+ new CLI commands/options** added
- **20+ test files** with comprehensive coverage
- **Zero breaking changes** - Full backward compatibility maintained
- **Enterprise-grade reliability** with proper error handling and timeouts

---

## [1.0.0] - 2026-03-15

Initial release of Fraisier deployment management system.

### Added
- Core deployment functionality for multiple providers (Docker Compose, API, ETL)
- Configuration-driven deployment with `fraises.yaml`
- Systemd socket-activated deployment daemons
- Scaffold generation for infrastructure files
- Version management and git integration
- Database migration support via confiture
- Comprehensive testing framework
- Rich CLI with progress indicators and error handling
