# Changelog

## v0.1.0 (2026-03-24)

First published release. Atomic deploy + migrate with surgical rollback
for PostgreSQL applications using confiture for migrations.

### New in v0.1.0

#### Notifications
- Pluggable notification system with `Notifier` protocol and `DeployEvent` dataclass
- Git issue creation on GitHub, GitLab, Gitea, Bitbucket with dedup and auto-close
- Slack and Discord webhook notifiers
- Generic webhook notifier (POST JSON to any URL)
- `NotificationDispatcher` wired into deployment lifecycle (fire-and-forget)
- Notification config validated at load time

#### Safety & Reliability
- Replaced broad `except Exception` catches with specific types
- Configurable `lock_timeout` per fraise environment (default 300s)
- API deployer attempts rollback on SIGALRM timeout
- Service name validation in all Docker Compose provider methods
- Docker cp path traversal rejection
- Webhook port and rate-limit bounds checking

#### Rollback
- Shared `_git_rollback()` mixin for all git-based deployers
- Scheduled deployer rollback restores git SHA and restarts timer

#### Pre-flight Validation
- `fraisier validate` checks git repo reachability via `git ls-remote`
- SSH connectivity and DB readiness checks (independently skippable)

### Core Features

#### Core Deploy Loop

- Bare metal provider: SSH + systemd deployment with TCP/HTTP health checks
- Docker Compose provider: container orchestration with health monitoring
- File-based mutex (`fcntl.flock`) prevents concurrent deploys
- Atomic status file writes (tmp + rename + fsync)

#### Database Migration Safety

- Confiture Python API integration for PostgreSQL migrations
- Three strategies: `rebuild` (dev), `restore_migrate` (staging), `migrate` (prod)
- Preflight checks verify migration reversibility before deploy
- Surgical rollback via `confiture migrate down --steps=N` on failure
- `--no-rollback` flag for irreversible migrations

#### Webhook & CI

- Webhook handler with HMAC signature verification (GitHub, GitLab, Bitbucket, Gitea)
- Automatic GitHub Issue creation on deploy failure
- Authenticated status details endpoint
- Version-gated deployments (reject outdated pushes)

#### Infrastructure Scaffolding

- `fraisier scaffold` generates systemd units, nginx config, sudoers, shell scripts
- Per-fraise service templates with security hardening
- Confiture YAML and GitHub Actions workflow generation

#### Developer Experience

- `fraisier init` creates starter fraises.yaml
- `fraisier ship` for one-command version bump + commit + push
- `fraisier health` with JSON output for monitoring integration
- Structured error hints with actionable fix suggestions

#### Security

- Input validation on all boundaries (service names, DB identifiers, file paths)
- Path traversal prevention in `validate_file_path()`
- Parameterized SQL queries; identifiers validated via regex
- SSH commands use list-based subprocess (no shell injection)
- Webhook secrets loaded from environment, never hardcoded
- Systemd security hardening on all scaffolded service units
