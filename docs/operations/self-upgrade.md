# Webhook self-upgrade

When a deployed `pyproject.toml` pins a newer fraisier version than the
webhook is running, the webhook process spawns a detached worker that
installs the new version and restarts itself via the systemctl-helper
socket. This document explains how the worker coordinates with in-flight
deployments so the restart does not kill concurrent work, and which
configuration knobs tune that coordination.

## How self-upgrade works

The flow is documented in detail in the
[`fraisier.webhook_self_upgrade`](../../fraisier/webhook_self_upgrade.py)
module docstring. Recap:

1. After a successful deploy, the webhook calls `maybe_self_upgrade`.
2. If a newer fraisier pin is detected, `maybe_self_upgrade` does an
   allowlist pre-flight against the systemctl helper, then spawns a
   detached worker (`start_new_session=True`).
3. The worker performs the install + restart sequence described in
   "Coordinated restart" below.
4. The worker survives the webhook restart because it is in its own
   session.

## What else is running the old code

The worker restarts one unit: the webhook. Every **root helper** —
systemctl, install, scaffold-install, unit-installer — runs from the same
deploy-user venv as a `Type=simple` daemon behind an `Accept=no` socket, so a
single process serves every connection and an upgrade on disk does not reach
it. Before v0.72.0 those processes kept serving the old code until someone ran
`scaffold-install`, which is how a host could end up with a webhook that sends
a request its own helper does not recognise
([#391](https://github.com/fraiseql/fraisier/issues/391)).

Each helper now compares the fraisier version it started with against the one
installed on disk — read fresh, `importlib.metadata` re-reads its dist-info in
a live process — and **exits cleanly when they differ**. Its socket unit is
still listening, so the next connection starts it again on the new code. The
check runs when `accept()` times out, so an idle helper turns over within
`DEFAULT_POLL_INTERVAL_S` (30s) of the upgrade rather than waiting for a
caller, and again after each request so a busy one turns over too. It never
runs between accepting a connection and answering it.

A version that cannot be read — a venv mid-upgrade, a half-removed dist-info
(#351) — leaves the helper running. Staleness detection is not a new way for a
root daemon to die.

In the journal:

```
INFO fraisier.helper_version: fraisier changed under this helper: 0.71.1 → 0.72.0.
Exiting so the next connection starts it on the new code.
```

## Replaying what the upgrade refused

While the `.draining` flag is up the webhook answers new dispatches with
HTTP 503, and records each `(fraise, environment)` it dropped in
`<lock_dir>/.refused-dispatches` ([#365](https://github.com/fraiseql/fraisier/issues/365)).
Since v0.72.0 the upgrade also re-fires them
([#367](https://github.com/fraiseql/fraisier/issues/367)).

The worker writes `<lock_dir>/.replay-on-start` immediately before it requests
the restart. The next webhook start **consumes** that marker — reads and
removes in one step — and replays only then. A webhook restarted for any other
reason finds no marker and replays nothing: "deploy everything in the ledger"
is not something an unrelated restart may mean.

Three things the replay does deliberately:

- **It deploys the branch head, not the recorded sha.** Without the refusal the
  push would have deployed, and any later push would have deployed after it —
  the end state is *branch head deployed*. Re-deploying the recorded sha would
  put back code that newer pushes had already superseded, which is a regression
  dressed as a recovery. There is no mode that does it.
- **Production goes last**, otherwise alphabetical by `(environment, fraise)`.
  Deterministic, and if the replay mechanism is itself broken it breaks on a
  lower-stakes target first. The entries are separate debts, so one failure does
  not hold the others back.
- **It never clears a ledger entry.** The replay dispatches through the same
  path a push takes, whose success branch already discharges the entry. A replay
  that failed, or that has not finished, leaves the entry standing and
  `fraisier doctor` still reports it.

A target that has been renamed or removed from `fraises.yaml` is not replayed;
its entry stands for the operator.

Turn it off with:

```yaml
webhook:
  replay_refused: off     # default: head
```

`off` still consumes the marker, so switching back on later cannot fire a stale
handoff.

## Coordinated restart

The worker sequences flag, install, settle, drain, and restart as follows:

```
1. touch <lock_dir>/.draining          # webhook starts refusing new deploys (HTTP 503)
2. uv tool install fraisier=={X}        # the install itself
3. sleep self_upgrade_drain_settle_s    # let dispatch-accepted tasks reach their lock
4. poll *.lock files until none held    # bounded by self_upgrade_drain_timeout_s
5. unlink <lock_dir>/.draining          # webhook stops returning 503
6. systemctl restart fraisier-…         # via the systemctl-helper RPC
```

**Why the flag is set _before_ install, not after.** Touching the flag
before `uv tool install` means dispatch refuses new deploys for the whole
upgrade window — install + drain. Touching it after install would leave a
several-second window during which new deploys queue in
`BackgroundTasks`, only to be killed by the impending restart. The
flag-before-install ordering is what closes the race in the common case.

**Why the settle delay.** There is a sub-millisecond window between
"dispatch accepted, HTTP 200 returned" and "BackgroundTask actually
reaches `with deployment_lock(...)`". The settle sleep
(`self_upgrade_drain_settle_s`, default 2.0s) gives any just-accepted
task time to take its flock before the worker counts. It shrinks the
residual race window to vanishingly small in practice; see "Known
residual race and scope" below for the honest scope statement.

## Configuration

All four knobs live under the existing `webhook:` mapping in
`fraises.yaml`. They have safe defaults in code — no scaffolded YAML
edits are required.

| Key | Default | Purpose |
|---|---|---|
| `webhook.self_upgrade_drain_timeout_s` | `600` | Max seconds the worker waits for in-flight deploys to release their `*.lock` files. |
| `webhook.self_upgrade_drain_poll_s` | `1.0` | Interval at which the worker re-counts held locks. |
| `webhook.self_upgrade_drain_settle_s` | `2.0` | Sleep after touching the flag and before the first count, so dispatch-accepted tasks reach their lock. |
| `webhook.self_upgrade_retry_after_s` | `60` | Value of the HTTP `Retry-After` header on 503 responses during drain. |
| `webhook.self_upgrade_flag_max_age_s` | `3600` | How long the `.draining` flag is believed. Past it the flag is **ignored** and deploys resume — see [a flag nothing clears](#a-flag-nothing-clears). |

Example override:

```yaml
webhook:
  self_upgrade_drain_timeout_s: 900
  self_upgrade_drain_settle_s: 3.0
  self_upgrade_retry_after_s: 30
```

## Failure modes

### Drain timeout

If the worker reaches `self_upgrade_drain_timeout_s` without seeing the
lock count drop to zero, it:

- logs a `WARNING` containing the timeout value and the basenames of the
  still-held lock files (e.g. `staging.lock, api.lock`),
- clears the `.draining` flag,
- **skips the restart RPC** and exits with rc `2` (distinct from
  install-failure rc `1` and restart-RPC-failure rc `1`).

The operator must then restart the webhook manually:

```bash
systemctl restart fraisier-<project>-webhook.service
```

The worker's per-event log lives under
`/var/lib/fraisier/self-upgrade/<project>-<timestamp>.log` — look for
`drain timeout` and the list of held locks to identify which fraise hung.

That log now opens with a `self-upgrade: starting` line naming the version and
the unit, and closes with `self-upgrade: finished — <outcome> (rc=N) after
Ns` on **every** exit. Both questions an operator holding a 503 actually has —
is an upgrade running, and how long should I wait — are answerable from it.
Before this, the last line written was an *intention* ("requesting restart")
and the other exits said nothing about having ended at all. The refusal WARNING
in the journal names this directory, because everything after the spawn runs
here and never reaches the journal.

### Install failure

If `uv tool install` returns a non-zero rc, the worker:

- logs the rc and stderr,
- clears the `.draining` flag (via the context manager — guaranteed),
- skips the drain loop and restart RPC,
- exits with the install's rc (typically `1`).

The webhook stays on its current version. Subsequent webhook events will
re-detect the version mismatch and re-spawn the worker on the next
successful deploy.

### A flag nothing clears

`draining_flag` unlinks the flag in a `finally`, which covers a clean exit and
an exception — but not a `SIGKILL`, not an OOM kill, and not a host that loses
the worker some other way. The lifespan hook that clears a stale flag only
fires when the webhook itself restarts, so a worker lost *without* one left
that host refusing **every** dispatch indefinitely, with a single WARNING line
as the only evidence.

Past `webhook.self_upgrade_flag_max_age_s` (default 3600, six times the drain
timeout plus room for the install) the flag **loses its authority** and deploys
resume. A WARNING names its age and points at the worker log directory.

Failing open here is deliberate. If the budget is wrong — an unusually slow
install, a drain legitimately holding past it — a deploy starts while an
upgrade is mid-install, which is loud and recoverable. The alternative is a
host that silently drops every deploy indefinitely, which is neither, and is
the failure that was actually reported. Raise the budget on a host whose
installs run longer rather than patching the guard.

The flag file itself is **not** removed by the guard: only its authority
expires. It stays on disk as the evidence an operator needs afterwards, and
clearing it remains the lifespan hook's job.

### Restart RPC failure

`ConnectionRefusedError` or `CalledProcessError` from the systemctl
helper produces an `ERROR` log line and an exit code of `1`. The new
binary is on disk; the operator can restart the unit manually to pick it
up.

## Upstream callers receiving 503

During the drain window, the webhook returns
`HTTP 503 Service Unavailable` with a `Retry-After` header and a
structured JSON body:

```json
{
  "error_type": "service_unavailable",
  "message": "Webhook is draining for self-upgrade.",
  "recovery_hint": "Self-upgrade in progress. Retry after the indicated delay.",
  "deployments": [
    {"fraise": "api", "environment": "staging",
     "status": "draining", "retry_after_s": 60,
     "draining_age_s": 41}
  ],
  "branch": "staging",
  "provider": "github",
  "webhook_id": 42
}
```

`draining_age_s` is how long the upgrade has been running. It is the one
number that separates the two cases a 503 cannot otherwise distinguish: 41
seconds is a healthy upgrade, six hours is a corpse. It is also the only thing
this body carries beyond what it always did — the response goes to an
unauthenticated caller, so no path, version or host detail belongs in it.

A refused dispatch is now also **recorded**, one entry per
`(fraise, environment)` the push would have deployed, in
`<lock_dir>/.refused-dispatches`. `fraisier doctor` warns while any entry
stands and names the command that re-fires it; the entry clears when a deploy
for that target succeeds, and only then. Before this the request simply
vanished: 503 as back-pressure is right, the branch staying undeployed with no
record of having been asked for is not.

GitHub Actions records the workflow as `failure` by default. To
re-trigger the deploy automatically once the webhook is back up, add a
retry step (uses the standard exit code from `curl --fail`):

```yaml
- name: Trigger deploy
  uses: nick-fields/retry@v3
  with:
    timeout_minutes: 5
    max_attempts: 3
    retry_wait_seconds: 60
    command: |
      curl -fSs -X POST "$WEBHOOK_URL" \
        -H "X-Hub-Signature-256: sha256=$SIG" \
        -H "X-GitHub-Event: push" \
        -H "X-GitHub-Delivery: ${{ github.run_id }}" \
        --data-binary @payload.json
```

A 503 from the webhook then triggers up to three retries, sixty seconds
apart — matching the default `Retry-After` value.

## Known residual race and scope

This fix is **scoped to `lock_backend=file`** (the default). The
coordination primitive — `count_held_deployment_locks` — reads `*.lock`
files via `fcntl.flock`. On `lock_backend=database` hosts there are no
`*.lock` files: the drain loop immediately sees zero and proceeds to
restart, matching today's behaviour. Hosts on the database backend get
the dispatch refusal (the `.draining` flag is backend-agnostic) but not
the wait-for-in-flight-deploys behaviour. A SQL-backed drain helper is
tracked as a follow-up.

Even on the file backend, there is a sub-millisecond window between
"dispatch accepted, response sent" and "BackgroundTask reaches
`with deployment_lock(...)`". The settle delay
(`self_upgrade_drain_settle_s`) shrinks this to near-zero in practice
but does not eliminate it — a fully race-free fix requires hoisting the
lock acquisition up to dispatch time, which conflicts with the
database-backend code path and is deferred to the same follow-up.
