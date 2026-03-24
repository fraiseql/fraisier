# Changelog

## v0.3.0 (2026-03-23)

Initial public release. Atomic deploy + migrate with surgical rollback
for PostgreSQL applications using confiture for migrations.

### Core Deploy Loop

- Bare metal provider: SSH + systemd deployment with TCP/HTTP health checks
- Docker Compose provider: container orchestration with health monitoring
- File-based mutex (`fcntl.flock`) prevents concurrent deploys
- Atomic status file writes (tmp + rename + fsync)

### Database Migration Safety

- Confiture Python API integration for PostgreSQL migrations
- Three strategies: `rebuild` (dev), `restore_migrate` (staging), `migrate` (prod)
- Preflight checks verify migration reversibility before deploy
- Surgical rollback via `confiture migrate down --steps=N` on failure
- `--no-rollback` flag for irreversible migrations

### Webhook & CI

- Webhook handler with HMAC signature verification (GitHub, GitLab, Bitbucket, Gitea)
- Automatic GitHub Issue creation on deploy failure
- Authenticated status details endpoint
- Version-gated deployments (reject outdated pushes)

### Infrastructure Scaffolding

- `fraisier scaffold` generates systemd units, nginx config, sudoers, shell scripts
- Per-fraise service templates with security hardening
- Confiture YAML and GitHub Actions workflow generation

### Developer Experience

- `fraisier init` creates starter fraises.yaml
- `fraisier ship` for one-command version bump + commit + push
- `fraisier health` with JSON output for monitoring integration
- Structured error hints with actionable fix suggestions

### Security

- Input validation on all boundaries (service names, DB identifiers, file paths)
- Path traversal prevention in `validate_file_path()`
- Parameterized SQL queries; identifiers validated via regex
- SSH commands use list-based subprocess (no shell injection)
- Webhook secrets loaded from environment, never hardcoded
- Systemd security hardening on all scaffolded service units
