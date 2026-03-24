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

## Rate Limiting

The webhook endpoint enforces rate limiting:
- 10 requests per minute per IP (configurable via `FRAISIER_WEBHOOK_RATE_LIMIT`)
- Maximum 256 tracked IPs (LRU eviction)

## What Fraisier Does NOT Protect Against

- **Host compromise**: If an attacker has shell access to the deployment server, fraisier cannot protect against them.
- **Network MitM**: Fraisier does not manage TLS. Use a reverse proxy (nginx, Caddy) with TLS termination.
- **Config file tampering**: If an attacker can write to `fraises.yaml`, command validation reduces but does not eliminate risk. Protect the config file with filesystem permissions.
- **Secrets in config**: Fraisier does not encrypt secrets at rest. Use environment variables for sensitive values (`${VAR}` syntax in YAML).
