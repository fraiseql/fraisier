# Security

Fraisier's security model and hardening measures.

## Webhook Secret

The webhook server **requires** a secret to verify incoming requests. Without it, the server refuses to start.

### Requirements

- Set via `FRAISIER_WEBHOOK_SECRET` environment variable
- Minimum 32 characters
- Used for HMAC signature verification (GitHub, Gitea, Bitbucket) or token comparison (GitLab)

### Generating a Secret

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Configuration

```bash
export FRAISIER_WEBHOOK_SECRET="your-secret-here-at-least-32-characters"
fraisier-webhook
```

## Input Validation

### Shell Commands

Commands from `fraises.yaml` (e.g., `restore_command`, health check `command`) are validated before execution:

- **Rejected**: Shell metacharacters (`;`, `|`, `&`, `` ` ``, `$()`)
- **Parsed**: Using `shlex.split()` into a list of arguments
- **Executed**: Via `subprocess.run(list, ...)` with `shell=False`
- **Optional**: Binary allowlist (e.g., only `pg_restore` and `psql`)

This prevents command injection even if an attacker gains write access to the config file.

### Service Names

Systemd service names are validated against `^[a-zA-Z0-9_@.\-]+$` to prevent injection in `systemctl` commands.

### File Paths

- Paths are validated against `^[a-zA-Z0-9_./ -]+$`
- Path traversal (`..`) is detected and rejected
- When `base_dir` is specified, resolved paths must stay within it
- **Strict mode**: Rejects symlinks entirely (for backup paths)

### Docker CP Paths

- Must contain `:` separator
- Container path must be absolute (start with `/`)
- Path traversal (`..`) rejected

### Database Identifiers

PostgreSQL identifiers (schema names, table names) are validated against `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$`.

## Log Redaction

Sensitive values are automatically redacted in structured JSON logs. Any dict key containing these substrings has its value replaced with `***REDACTED***`:

- `password`, `secret`, `token`, `key`, `auth`, `credential`

Safe keys that would otherwise match (like `primary_key`, `foreign_key`, `sort_key`, `cache_key`) are explicitly excluded.

### Token providers (`smoke_tests.token_provider`)

Token providers acquire short-lived bearer credentials at deploy time. They share
the same trust envelope as `post_migrate`'s `psql -f` — the resolved value is
sensitive and the subprocess (for `exec`) runs as the deploy user. The
implementation guards against accidental leakage:

- **Resolved tokens never appear in logs** at any level (DEBUG included).
  Verified by `tests/test_token_providers.py::TestExecProvider::test_resolved_token_never_appears_in_logs`
  and the analogous OAuth2 cases.
- **`exec` subprocess argv** logs only `argv[0]` at INFO. Full argv (which may
  contain `--client-id` and similar non-token but operationally interesting
  args) is DEBUG-only.
- **`exec` subprocess is invoked with a list**, never `shell=True`.
- **`exec` subprocess `stderr` is not included in the raised
  `DeploymentError` message** — a wrapper with `set -x` enabled (or a
  helper that echoes its output to stderr) would otherwise have the token
  surface in the deploy journal via the outer `logger.exception(...)`.
  The stderr tail is emitted at DEBUG only; re-run with
  `FRAISIER_LOG_LEVEL=DEBUG` when triaging.
- **`format` placeholder validation.** A `format` string without
  `{token}` would silently drop the resolved value; a typo placeholder
  (`{access_token}`) would `KeyError` mid-deploy. Both shapes are
  rejected at config-load time.
- **Unknown keys in `token_provider:`** are rejected at config-load
  time. Operators who set the deferred `cwd` or `env_passthrough`
  options, or who typo a field name, learn at parse time rather than
  observing a silent no-op at deploy time.
- **OAuth2 `client_secret` and `refresh_token`** are redacted in the form-body
  log line at DEBUG. The token endpoint's error response body is never echoed in
  the raised `DeploymentError` — some IdPs include the client_secret in error
  envelopes.
- **Rotated OAuth2 refresh tokens** returned in the response are discarded.
  Fraisier does not write to your secrets store; rotation is the operator's
  responsibility.

## Rate Limiting

The webhook endpoint enforces rate limiting:
- 10 requests per minute per IP (configurable via `FRAISIER_WEBHOOK_RATE_LIMIT`)
- Maximum 256 tracked IPs (LRU eviction)

## User Separation

Fraisier supports separating the **deploy user** (runs deployments) from the **app user** (runs the application process). This follows the principle of least privilege: if the application is compromised, the attacker cannot drop databases, restart services, or modify deployed code.

### Two-user model

| User | Role | Privileges |
|------|------|-----------|
| `deploy_user` | Runs `fraisier deploy`, webhook, backups | CREATEDB (for rebuild strategy), sudoers for systemctl, git worktree write |
| `service.user` | Runs the application process | Connect to own DB, read app files |

### Configuration

```yaml
scaffold:
  deploy_user: myapp_deploy   # global deploy user

fraises:
  my_api:
    type: api
    environments:
      production:
        deploy_user: prod-deployer  # per-env override (optional)
        service:
          user: myapp               # app runs as myapp
```

When `service.user` differs from `deploy_user`, `fraisier setup` will:
1. Create both system accounts
2. Set `app_path` ownership to the app user
3. Add the deploy user to the app user's group for write access during deployment
4. Install sudoers for the deploy user's systemctl access

### When single-user is acceptable

On **development servers**, running both roles as the same user is acceptable (data is disposable, no external exposure). On **production**, use separate users. Production typically uses the `migrate` strategy which doesn't need CREATEDB.

### Database access

For strategies that need privileged database operations (rebuild, restore_migrate), configure `admin_url` to connect as a PostgreSQL superuser:

```yaml
database:
  strategy: rebuild
  admin_url: postgresql://postgres@/postgres?host=/var/run/postgresql
```

This avoids granting the deploy user OS-level sudo access to the postgres account.

## What Fraisier Does NOT Protect Against

- **Host compromise**: If an attacker has shell access to the deployment server, fraisier cannot protect against them.
- **Network MitM**: Fraisier does not manage TLS. Use a reverse proxy (nginx, Caddy) with TLS termination.
- **Config file tampering**: If an attacker can write to `fraises.yaml`, command validation reduces but does not eliminate risk. Protect the config file with filesystem permissions.
- **Secrets in config**: Fraisier does not encrypt secrets at rest. Source secrets from environment variables instead of embedding them in `fraises.yaml`:
  - **`!envvar VAR_NAME`** (deferred, recommended): a custom YAML tag that parses into a placeholder; the `os.environ['VAR_NAME']` lookup is deferred to consumption time and re-reads on every access. Missing variables raise `ConfigurationError` *when the value is actually consumed*, naming the full YAML key path of the placeholder. Subcommands that do not enter a section (`--help`, `--version`, `ship --help`, etc.) do not require its env tags. Run `fraisier validate --resolve-envvars` to force-resolve every reference in one pass for a pre-deploy CI gate. Works anywhere in `fraises.yaml`, including `git.<provider>.webhook_secret`, `smoke_tests.headers`, and `database.database_url`.
  - **`${VAR_NAME}`** (runtime): scoped to the `notifications:` and `hooks:` blocks only. Expanded by the dispatcher at fire time. Do not use elsewhere — most consumers read the YAML value literally and will not expand it.
