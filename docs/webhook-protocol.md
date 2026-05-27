# Fraisier outbound notification webhook protocol

This document is the contract for the JSON payload fraisier POSTs to a
`type: webhook` notification destination. For *inbound* Git-provider
webhooks (GitHub/GitLab/Gitea/Bitbucket → fraisier), see
[webhook-reference.md](webhook-reference.md).

## When this fires

Fraisier emits one POST per `type: webhook` notification destination per
deployment event. The four event types are:

| `event_type` | When fraisier sends |
|---|---|
| `success` | Deployment finished successfully (health check + smoke tests passed) |
| `failure` | Deployment failed and was not rolled back (or rollback wasn't attempted) |
| `rollback` | Deployment failed and was automatically rolled back to the previous SHA |
| `rollback_failed` | Both the deployment and the rollback failed — manual intervention required |

The dispatcher lives in `fraisier/notifications/dispatcher.py`; the
notifier itself is `WebhookNotifier` in
`fraisier/notifications/messaging.py`.

## Endpoint contract

| | |
|---|---|
| Method | `POST` (overridable via `method:` in the destination config) |
| Content-Type | `application/json` |
| Timeout | 10 seconds (`_TIMEOUT` in `messaging.py`) |
| Retry | None — fraisier does not retry failed POSTs at the notification layer; failures are logged. Consumers should rely on at-most-once semantics. |

### Headers

Fraisier sets only `Content-Type: application/json`. **Authentication is
the operator's responsibility** — pass any required headers (bearer
token, shared secret) via the destination's `headers:` block in
`fraises.yaml`:

```yaml
notifications:
  on_failure:
    - type: webhook
      url: https://ops.example.com/hooks/fraisier
      headers:
        Authorization: !envvar OPS_HOOK_TOKEN
        X-Source: fraisier
```

The header value goes through `!envvar` resolution at notification
time, so secrets stay out of `fraises.yaml` itself. Header keys are
sent verbatim — fraisier does not normalize, lowercase, or merge
headers with the same name.

> **Signature scheme.** Fraisier does **not** HMAC-sign outbound
> notification payloads. Authenticate via shared-secret headers as
> above, or terminate the webhook behind an authenticating reverse
> proxy. (The HMAC verification machinery in `fraisier/webhook.py` is
> for *inbound* webhooks fraisier receives from Git providers — not
> outbound.)

## Payload schema

The payload is the serialized form of `fraisier.notifications.base.DeployEvent`
via `DeployEvent.to_dict()`:

| Field | Type | Presence | Description |
|---|---|---|---|
| `fraise_name` | `string` | required | The fraise (deployment target) name from `fraises.yaml` |
| `environment` | `string` | required | Environment name (e.g. `production`, `staging`) |
| `event_type` | `string` | required | One of `success`, `failure`, `rollback`, `rollback_failed` |
| `error_message` | `string \| null` | optional | Human-readable error message (always `null` on `success`) |
| `error_code` | `string \| null` | optional | Machine-readable error code (e.g. `MIGRATION_PREFLIGHT_FAILED`); `null` on `success` |
| `recovery_hint` | `string \| null` | optional | Operator-facing recovery instruction sourced from `fraisier.errors.RECOVERY_HINTS`; `null` on `success` |
| `old_version` | `string \| null` | optional | The SHA fraisier deployed *from* (truncated to 8 chars in some flows). On `success` for a first deploy, may be `null`. |
| `new_version` | `string \| null` | optional | The SHA fraisier deployed *to*. On `rollback`, this is the SHA that's now live after the rollback (i.e., the previous SHA). |
| `duration_seconds` | `number` | required | Wall-clock duration of the deploy attempt, in seconds. Float. `0.0` if the event fired before timing started. |
| `triggered_by` | `string` | required | Trigger label — `deploy`, `webhook`, `manual`, etc. Defaults to `"deploy"` if not set. |
| `commit_sha` | `string \| null` | optional | Same as `new_version` (deprecated alias; consumers should prefer `new_version`). |
| `incident_path` | `string \| null` | optional | Path to a structured incident dump for `failure` and `rollback_failed` events; `null` otherwise. |
| `timestamp` | `string` | required | ISO 8601 UTC timestamp at event construction (e.g. `2026-05-27T19:42:00.123456+00:00`). |

`null` and absent are not equivalent — fraisier always emits every
field; optional fields are `null` when not applicable.

### Stability guarantees

- New fields **may** be added in any release; consumers must tolerate
  unknown fields.
- Existing field types and names will not change without a major
  version bump in fraisier.
- The `event_type` value set will not shrink; new event types may be
  added.

## Worked example: `failure`

```json
{
  "fraise_name": "my_api",
  "environment": "production",
  "event_type": "failure",
  "error_message": "Migration preflight failed (1 of 12 migrations would fail):\n  - 0042 (add_users_role.sql): permission denied for role app_role",
  "error_code": "MIGRATION_PREFLIGHT_FAILED",
  "recovery_hint": "rollback the restored snapshot, fix migrations, or run `confiture migrate baseline` to re-baseline.",
  "old_version": "abc12345",
  "new_version": null,
  "duration_seconds": 12.4,
  "triggered_by": "webhook",
  "commit_sha": null,
  "incident_path": "/var/log/fraisier/incidents/2026-05-27T19-42-00Z-my_api-production.json",
  "timestamp": "2026-05-27T19:42:00.123456+00:00"
}
```

## Worked example: `rollback`

```json
{
  "fraise_name": "my_api",
  "environment": "production",
  "event_type": "rollback",
  "error_message": "Health check failed; rolled back successfully",
  "error_code": "HEALTH_CHECK_FAILED",
  "recovery_hint": "`fraisier rollback <fraise> <env>` — the new revision is live but failing its health check.",
  "old_version": "abc12345",
  "new_version": "abc12345",
  "duration_seconds": 84.7,
  "triggered_by": "deploy",
  "commit_sha": "abc12345",
  "incident_path": null,
  "timestamp": "2026-05-27T19:45:12.987654+00:00"
}
```

After a successful rollback, `new_version` is the SHA that is now
serving — i.e., the previous good SHA. `old_version` is the same value.

## Worked example: `success`

```json
{
  "fraise_name": "my_api",
  "environment": "production",
  "event_type": "success",
  "error_message": null,
  "error_code": null,
  "recovery_hint": null,
  "old_version": "abc12345",
  "new_version": "def67890",
  "duration_seconds": 42.1,
  "triggered_by": "webhook",
  "commit_sha": "def67890",
  "incident_path": null,
  "timestamp": "2026-05-27T19:50:00.000000+00:00"
}
```

## Idempotency and ordering

Fraisier emits notifications **after** the deployment has completed (or
failed), so a given (fraise, environment, timestamp) tuple is sent at
most once. There is no delivery-ID header; consumers that need
idempotent processing should de-dupe on `(fraise_name, environment,
timestamp, event_type)`.

Notifications for a single deploy fire in a single dispatcher pass —
ordering across destinations within the same event is not guaranteed.
Across events, fraisier never overlaps deploys for the same `(fraise,
environment)` (the deployment lock prevents it), so events for the same
fraise+env arrive in chronological order.

## See also

- [webhook-reference.md](webhook-reference.md) — *inbound* webhooks (GitHub/GitLab/Gitea/Bitbucket → fraisier)
- [notifications.md](notifications.md) — full notification destination catalog (Slack, Discord, Teams, Email, generic webhook)
- [deployment-guide.md#post-migration-verification](deployment-guide.md#post-migration-verification) — when and why each `event_type` fires
