# Changelog

## v0.1.0 (2026-03-25)

First published release. Atomic deploy + migrate with surgical rollback
for PostgreSQL applications using confiture for migrations.
1260 tests, zero lint warnings.

### Core Deploy Loop

- Bare metal provider: SSH + systemd deployment with TCP/HTTP health checks
- Docker Compose provider: container orchestration with health monitoring
- Database-backed deployment lock with SQLite WAL
- Thread-based deployment timeout (works in multi-threaded contexts)
- Atomic status file writes (tmp + rename + fsync)

### Database Migration Safety

- Confiture Python API integration for PostgreSQL migrations
- Three strategies: `rebuild` (dev), `restore_migrate` (staging), `migrate` (prod)
- Preflight checks verify migration reversibility before deploy
- Surgical rollback via `confiture migrate down --steps=N` on failure
- `--no-rollback` flag for irreversible migrations
- Exact migration rollback count tracked in incident files

### Rollback

- Automatic git rollback on migration failure — restores previous SHA and restarts service
- `ROLLBACK_FAILED` status when rollback itself fails, with critical notification
- Shared `_git_rollback()` mixin for all git-based deployers
- Scheduled deployer rollback restores git SHA and restarts timer

### Webhook & CI

- Webhook handler with HMAC signature verification (GitHub, GitLab, Bitbucket, Gitea)
- Webhook server refuses to start without `FRAISIER_WEBHOOK_SECRET` (minimum 32 chars)
- In-memory rate limiting (10/min per IP)
- Automatic GitHub Issue creation on deploy failure
- Authenticated status details endpoint
- Version-gated deployments (reject outdated pushes)

### Notifications

- Pluggable notification system with `Notifier` protocol and `DeployEvent` dataclass
- Git issue creation on GitHub, GitLab, Gitea, Bitbucket with dedup and auto-close
- Slack and Discord webhook notifiers
- Generic webhook notifier (POST JSON to any URL)
- `NotificationDispatcher` wired into deployment lifecycle (fire-and-forget)

### Infrastructure Scaffolding

- `fraisier scaffold` generates systemd units, nginx config, sudoers, shell scripts
- Per-fraise service templates with security hardening
- Confiture YAML and GitHub Actions workflow generation

### Developer Experience

- `fraisier init` creates starter fraises.yaml
- `fraisier ship` for one-command version bump + commit + push
- `fraisier health` with JSON output for monitoring integration
- `fraisier validate` checks git repo, SSH, and DB readiness
- Health check retries wired from config (`health.retries`)
- Structured error hints with actionable fix suggestions

### Security

- Input validation on all boundaries (service names, DB identifiers, file paths)
- Path traversal prevention in `validate_file_path()` with strict symlink rejection
- Shell commands from config validated for metacharacters before execution
- Safe integer parsing (`get_int_env`) for all environment variables
- Parameterized SQL queries; identifiers validated via regex
- SSH commands use list-based subprocess (no shell injection)
- Docker CP paths require absolute container paths
- Log redaction for keys containing `password`, `secret`, `token`, `key`, `auth`, `credential`
- Webhook secrets loaded from environment, never hardcoded
- Systemd security hardening on all scaffolded service units

### Documentation

- `docs/failure-modes.md`: Decision tree for every failure scenario
- `docs/security.md`: Threat model, validation rules, log redaction
- README: "When NOT to use Fraisier" and honest comparison table
