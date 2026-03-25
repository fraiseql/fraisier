# Changelog

## v0.2.0 (2026-03-25)

Bulletproof deploy + migrate + rollback pipeline with security hardening,
production safety, and comprehensive test coverage (1256 tests).

### Critical Fixes
- **Git rollback on migration failure**: When migration fails after git checkout,
  code is now automatically rolled back to the previous SHA and service restarted
- **ROLLBACK_FAILED status**: When rollback itself fails, operators receive a
  critical notification with both the original and rollback errors
- **Timeout rollback failures** now correctly report `ROLLBACK_FAILED` status
  instead of generic `FAILED`
- Exact migration rollback count tracked in incident files

### Production Safety
- **Database-backed deployment lock** with SQLite WAL — replaces local-only
  file locking for multi-server environments
- **Thread-based deployment timeout** replaces `SIGALRM` — works correctly
  in multi-threaded contexts and on non-Unix platforms
- Health check retries wired from config (`health.retries`), no longer hardcoded
- Deprecation warnings for `deployment.poll_interval_seconds` and
  `deployment.webhook_secret_env` (superseded by `health.*` and env vars)

### Security Hardening
- Webhook server **refuses to start** without `FRAISIER_WEBHOOK_SECRET` (minimum 32 chars)
- Shell commands from config (`restore_command`, health check `command`) are validated
  for metacharacters before execution — prevents command injection
- **Safe integer parsing** (`get_int_env`) for all environment variables —
  rejects non-numeric and out-of-range values with fallback to defaults
- Webhook port and rate-limit validated at server startup
- Log redaction expanded: any key containing `password`, `secret`, `token`, `key`,
  `auth`, or `credential` is redacted (safe keys like `primary_key` excluded)
- `validate_file_path()` gains `strict` mode that rejects symlinks
- Docker CP paths now require absolute container paths

### Refactoring
- `SystemdServiceManager` extracted from deployers — single responsibility
- `HealthCheckManager` consolidates retry logic from duplicated implementations
- Webhook helpers extracted, long handler functions reduced to orchestrators
- Rollback helpers extracted from `APIDeployer`, strategy resolution deduplicated
- Silent `except Exception` catches replaced with specific types and logged context
- Duplicate `_rollback_git` removed from `APIDeployer` — uses `GitDeployMixin`

### Cleanup
- Removed unused `DeploymentLock` (database-backed distributed locking) and
  `DeploymentLockedError` — file-based `fcntl.flock` is the documented scope
- `_restore_previous_state()` extracted as shared method in `APIDeployer`

### Documentation
- `docs/failure-modes.md`: Decision tree for every failure scenario
- `docs/security.md`: Threat model, validation rules, log redaction
- SQL construction safety invariant documented in `insert()`
- README: "When NOT to use Fraisier" and honest comparison table

### Test Improvements
- **Integration test fixtures**: `git_deploy_env` creates real bare repo +
  worktree for deploy/rollback testing without mocks
- **Database isolation**: autouse fixture ensures per-test DB state
- **Deploy→rollback integration tests** with real git operations
- **Webhook→deployment chain** integration tests
- Coverage for all previously untested public functions
- Integration test harness for confiture migrations (requires PostgreSQL)
- Scaffold artifact validation (no unexpanded templates)
- `pytest.mark.integration` marker for external-service tests

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
