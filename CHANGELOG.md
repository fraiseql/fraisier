# Changelog

## v0.2.3 (2026-03-30)

Feature release: admin_url for privileged DB operations. 1507 tests, zero lint warnings.

### Admin URL (#27)

- **feat:** `admin_url` config field for `rebuild` and `restore_migrate` strategies — uses a PostgreSQL superuser connection for DROP/CREATE DATABASE instead of `sudo -u postgres`, fixing deployments where the service user has no sudo access
- **feat:** `RestoreMigrateStrategy` now passes `connection_url` to all dbops calls (terminate_backends, drop_db, create_db) — previously always fell back to sudo
- **feat:** config validation for `admin_url` (same rules as `database_url`)

## v0.2.2 (2026-03-30)

Bug-fix release: rebuild strategy path doubling. 1507 tests, zero lint warnings.

### Path Resolution (#26)

- **fix:** `RebuildStrategy` used config file's parent as `project_dir`, causing `SchemaBuilder` to produce doubled paths like `app/db/environments/db/environments/dev.yaml` — deployer now passes `app_path` as explicit `project_dir`; standalone callers fall back to config file's parent (original behavior)

## v0.2.1 (2026-03-30)

Bug-fix release: deploy CLI path resolution. 1507 tests, zero lint warnings.

### Path Resolution (#26)

- **fix:** resolve relative `confiture_config` and `migrations_dir` against `app_path` explicitly instead of relying solely on `os.chdir()` — prevents silent misresolution when `app_path` is missing or the chdir is skipped
- **fix:** deployment now fails loudly with a clear `DeploymentError` when `app_path` is configured but the directory does not exist (was silently using wrong CWD)
- **fix:** database rollback also resolves paths against `app_path` (same fix as forward migrations)
- **fix:** config default locations reordered — CWD is now checked before `/opt/fraisier/` so local configs take precedence over system-wide installs

## v0.2.0 (2026-03-30)

Major release: restore_migrate strategy, server setup, ship pipeline,
rebuild hardening, and real PostgreSQL integration tests.
1521 tests, zero lint warnings.

### Restore-Migrate Strategy (#17)

- **feat:** `restore_migrate` strategy for staging — full backup restore lifecycle (find latest backup → validate age → pg_restore → optional ownership fix → migrate up → validate table count)
- **feat:** instant template-based rollback (`CREATE DATABASE … TEMPLATE`) as alternative to migrate down
- **feat:** configurable `min_tables` post-restore validation

### Server Setup (#15, #16)

- **feat:** `fraisier setup` command for server-side provisioning (create users, install systemd units, configure nginx, set up sudoers)
- **feat:** multi-server deployments with per-server filtering (`--server`)

### Ship Pipeline (#22, #23)

- **feat:** `fraisier ship` owns the full pre-commit pipeline with phased checks (fix → validate+test)
- **fix:** handle pre-commit hooks that modify files (re-stage after fix phase)

### Rebuild Strategy (#24, #25)

- **fix:** drop and recreate the entire database instead of just the schema — fixes "must be owner of schema public" errors when the app user doesn't own the public schema (#24)
- **feat:** `required_roles` configuration — provisions missing PostgreSQL roles (`CREATE ROLE … NOLOGIN`) and grants them to the database owner before schema apply, preventing silent failures from `CREATE SCHEMA … AUTHORIZATION <role>` referencing nonexistent roles (#25)
- **fix:** `psql -f` now uses `-v ON_ERROR_STOP=1` so schema apply aborts on the first SQL error instead of silently producing a half-built database (#25)
- **fix:** `SchemaBuilder` resolves configs relative to the confiture config file, not the working directory

### Database Operations

- **feat:** all `dbops.operations` functions accept an optional `connection_url` parameter to bypass `sudo -u postgres` — enables direct connections to containerised or remote PostgreSQL instances
- **feat:** `database_url` forwarded through the full strategy and deployer stack
- **feat:** confiture view_helpers option forwarded during migrate up (#20)
- **fix:** `terminate_backends` and `check_db_exists` no longer use psql variable binding (`:'var'`) which broke in psql 18

### Scaffold & Config (#14, #18)

- **feat:** per-environment webhook secrets (#14)
- **feat:** systemd and nginx names prefixed with project name (#18)

### Testing Infrastructure

- **feat:** integration tests using `testcontainers[postgres]` — 17 tests run against a real PostgreSQL 16 container
- **refactor:** removed 22 mock-heavy tests (525 lines) replaced by higher-confidence integration tests

### Dependencies

- **deps:** bump confiture to >=0.8.14
- **deps:** add `testcontainers[postgres]>=4.0.0` to dev dependencies

## v0.1.8 (2026-03-29)

Rebuild strategy performance. 1413 tests, zero lint warnings.

### Database Strategies (#13)

- **feat:** `RebuildStrategy` now uses `confiture build` (SchemaBuilder) + `psql -f` for bulk SQL apply — ~30s vs 10+ minutes for large schemas (~1284 SQL files)
- **feat:** rebuild now includes seed data (`schema_only=False`) making development databases immediately usable
- **fix:** drop and recreate `public` schema before applying to ensure clean state

## v0.1.7 (2026-03-29)

Deployment fix. 1413 tests, zero lint warnings.

### Deployment (#12)

- **fix:** stop service before `rebuild` strategy to release DB connections — prevents PostgreSQL "cache lookup failed for function" errors from stale OIDs
- **feat:** add `SystemdServiceManager.stop()` method

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
