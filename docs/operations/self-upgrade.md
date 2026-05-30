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

### Install failure

If `uv tool install` returns a non-zero rc, the worker:

- logs the rc and stderr,
- clears the `.draining` flag (via the context manager — guaranteed),
- skips the drain loop and restart RPC,
- exits with the install's rc (typically `1`).

The webhook stays on its current version. Subsequent webhook events will
re-detect the version mismatch and re-spawn the worker on the next
successful deploy.

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
     "status": "draining", "retry_after_s": 60}
  ],
  "branch": "staging",
  "provider": "github",
  "webhook_id": 42
}
```

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
