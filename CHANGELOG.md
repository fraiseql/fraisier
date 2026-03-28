# Changelog

## v0.1.6 (2026-03-28)

Deployment fix. 1411 tests, zero lint warnings.

### Deployment (#10)

- **fix:** `chdir` to `app_path` before running confiture migrations so relative paths resolve correctly when running as a systemd service

## v0.1.5 (2026-03-28)

Git deployment fix. 1409 tests, zero lint warnings.

### Git Operations (#9)

- **fix:** `git_repo` from fraises.yaml now used as bare repo path (was always constructing from `repos_base`)
- **fix:** `reset --soft` uses `--git-dir`/`--work-tree` flags instead of `git -C worktree` (fixes bare repo + worktree pattern where worktree has no `.git` directory)
- **fix:** `_git_rollback` same fix for `reset --soft`

## v0.1.4 (2026-03-28)

Quality review and monorepo support (#6).
1406 tests, zero lint warnings.

### Error Handling & Robustness

- **fix:** wire `_notify()` into APIDeployer success and failure paths (was only called on rollback failure)
- **fix:** `UnboundLocalError` when health check endpoints list is empty
- **fix:** TCP health check socket leak when `connect_ex` raises
- **fix:** check `PyThreadState_SetAsyncExc` return value (log warning on 0, undo on >1)
- **fix:** Docker Compose rollback now passes `IMAGE_TAG` env to `compose up`
- **fix:** `daemon-reload` runs before `enable`/`start` in scheduled deployer
- **fix:** file lock handle leak on unexpected `OSError`
- **fix:** `REASSIGN OWNED BY` failure now reported in `RestoreResult`

### API Design & Consistency

- **feat:** `reset_config()` for clean config singleton replacement
- **fix:** normalize commit SHA to full length across all git providers (Gitea/Bitbucket were truncating)
- **fix:** `status-all --type` filter checks each fraise individually (was only checking first)
- **fix:** CLI commands that need config show helpful error instead of `AttributeError`
- **refactor:** type deployer `runner` parameter as `CommandRunner | None`
- **refactor:** pool metrics use `psycopg_pool` public `get_stats()` API
- **refactor:** eliminate all `config._config` private access from webhook.py
- **fix:** `pip install` → `uv add` in error messages

### Webhook Header Normalization (#7)

- **fix:** use lowercase header normalization instead of `.title()` — fixes GitHub event detection silently failing (`X-Github-Event` vs `X-GitHub-Event`)

### Config Resolution (#8)

- **fix:** `FRAISIER_CONFIG` env var now respected when resolving config path (priority: `--config` flag > env var > standard locations)

### Monorepo Branch Mapping (#6)

- **feat:** `branch_mapping` accepts list-of-dicts syntax for one branch → multiple fraises
- **feat:** `get_fraises_for_branch()` returns all mapped fraises for a branch
- **feat:** webhook dispatch fires one deployment per mapped fraise (locked ones skipped independently)
- **feat:** config validation rejects missing keys and duplicate fraise+environment pairs
- **deprecate:** `get_fraise_for_branch()` (returns first match only)

## v0.1.3 (2026-03-28)

Per-environment systemd and nginx configuration (#4).
1317 tests, zero lint warnings.

### Infrastructure Scaffolding

- **feat(scaffold):** per-environment `service:` block in fraises.yaml — configurable `user`, `group`, `port`, `workers`, `exec`, `memory_max`, `memory_high`, `cpu_quota`, `environment_file`, `credentials` (LoadCredential), `environment` (arbitrary env vars), and `security` directives
- **feat(scaffold):** per-environment `nginx:` block — `server_name`, custom `ssl_cert`/`ssl_key`, per-env `cors_origins`, and structured `restricted_paths` with `allow`/`deny` rules
- **feat(scaffold):** configurable systemd security hardening — override individual directives (e.g., `protect_home: read-only`) while keeping defaults for the rest
- **feat(scaffold):** per-environment nginx config files (`nginx/{fraise}_{env}.conf`) generated alongside shared `gateway.conf`
- **feat(scaffold):** port resolution priority: `service.port` > `health_check.url` > default 8000
- **feat(scaffold):** backward compatible — legacy flat fields (`worker_count`, `memory_max`, `exec_command`) still work alongside nested `service:` key

## v0.1.1 (2026-03-28)

Bug-fix release addressing scaffold generation issues (#1, #2).
1265 tests, zero lint warnings.

### Infrastructure Scaffolding

- **fix(scaffold):** systemd `WorkingDirectory` now reads `app_path` from fraises.yaml instead of hardcoding `/opt/<name>` (#1)
- **fix(scaffold):** systemd `ExecStart` port extracted from `health_check.url` instead of hardcoded 8000 (#1)
- **fix(scaffold):** new `exec_command` field on fraises overrides the default uvicorn command for non-Python services (#1)
- **fix(scaffold):** nginx no longer generates duplicate `location /` blocks for multi-fraise setups (#2)
- **fix(scaffold):** nginx upstream blocks use per-fraise ports from `health_check.url` (#2)
- **fix(scaffold):** `server_name` field on fraises generates separate `server {}` blocks with per-domain SSL (#2)
- **fix(scaffold):** `location` field on fraises allows custom URL prefixes; auto-prefixes with `/<name>/` when multiple fraises share one server block (#2)

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
