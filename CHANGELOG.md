# Changelog

## v0.3.11 (2026-04-01)

Feature release: config synchronization, automatic scaffold regeneration, install step improvements, enhanced error reporting, health check clarity, pre-deployment validation, deployment status command, component test commands, multi-server scaffold filtering. Fixes #52, #53, #54, #55, #56, #57, #58, #60, #62. 1811+ tests, zero lint warnings.

### Scaffold: Multi-Server Webhook Path Filtering (#62)

- **fix:** webhook service now filters `ReadWritePaths` to only environments running on the current server, preventing startup failures when paths from other servers don't exist locally
- **feat:** new `fraisier scaffold --server <hostname>` option filters generated configurations to a specific server (matches `fraisier setup --server`)
- **feat:** preserves backward compatibility — running `scaffold` without `--server` generates all paths (existing behavior)
- **test:** 3 new tests covering server-filtered paths, unfiltered fallback, and non-existent server cases

### Component Test Commands (#60)

- **feat:** five new CLI commands for testing deployment components in isolation without full deployment risk
- **feat:** `fraisier test-wrapper <fraise> <env> <wrapper-type> [args...]` — validates and executes wrapper scripts (systemctl, pg) with timing and exit code reporting
- **feat:** `fraisier test-install <fraise> <env>` — runs the configured install step (e.g., `uv sync`) with detailed error context on failure
- **feat:** `fraisier test-health <fraise> <env>` — tests health check endpoint, shows URL/timeout/retries configured, reports HTTP status and response time
- **feat:** `fraisier test-git <fraise> <env>` — validates git operations: clone URL, bare repo exists, app path exists, remote connectivity (fetch --dry-run), current/latest versions
- **feat:** `fraisier test-database <fraise> <env>` — tests database connectivity: attempts `SELECT 1` query, shows connection time, masks password in display URL
- **feat:** all commands use rich table output with color-coded status (✓/✗/·) for consistency with validation commands
- **feat:** early validation checks prevent expensive operations; detailed error messages with recovery suggestions
- **test:** 24 new unit tests covering all 5 commands, error conditions, helper functions, mocking deployers/subprocess/filesystem

### Deployment Status Command (#58)

- **feat:** new CLI command `fraisier status [fraise] [environment]` shows deployment status and health across all fraises/environments — no args shows global table view, both args show single-fraise details
- **feat:** global table displays: Fraise | Environment | Deployed SHA | Deployed At | Latest SHA | Status | Health
- **feat:** status column shows: "deployed ✓" (green) when versions match, "out-of-date" (yellow) when mismatch, "unknown" (dim) when data unavailable
- **feat:** health column shows: "healthy ✓" (green) when health check passes, "unhealthy" (red) when failing, "not configured" (dim) when no health check defined
- **feat:** rich formatted output with color-coded status indicators; table columns expand for readability
- **feat:** pulls deployed version from DB (`tb_fraise_state.current_version` + `last_deployed_at`), latest version from deployer, health status from health_check() call
- **feat:** new git helper `get_commit_timestamp(git_dir, sha)` returns ISO timestamp for commits (used for audit/reporting)
- **test:** 6 new tests covering global view, deployed/out-of-date status, configured/unconfigured health, error handling

### Pre-Deployment Validation Command (#57)

- **feat:** new CLI command `fraisier validate-deployment <fraise> <environment>` validates infrastructure readiness before deployments — checks git repo, app_path, database config, systemd service, wrapper scripts, sudoers, health endpoint, and install dependencies
- **feat:** 9 targeted checks with severity levels — errors (blocking) and warnings (advisory) — each with actionable remediation hints
- **feat:** rich formatted output with ✓/✗/⚠ indicators and summary status (READY/NOT READY); supports `--json` for scripting
- **feat:** exit code 1 only on error-severity failures — warnings don't block (sudoers, health check timeouts are informational)
- **test:** 26 new tests covering all 9 checks with real filesystem operations, mocking only network/subprocess/environment

### Deploy: Wrapper Script Validation (#54)

- **feat:** `APIDeployer._validate_wrapper_scripts()` pre-flight check validates that `FRAISIER_SYSTEMCTL_WRAPPER` and `FRAISIER_PG_WRAPPER` scripts exist and are executable before deployment
- **feat:** clear error messages with remediation hints when wrappers are missing or not executable
- **feat:** validation runs at start of `execute()` before any deployment work (git pull, migrations, restart) — fails fast with actionable guidance

### Deploy: Automatic Config Synchronization

- **feat:** `_sync_fraises_yaml()` method pulls `fraises.yaml` from checked-out git repo to `/opt/<project>/fraises.yaml` during deployments
- **feat:** `_detect_config_changes()` uses hash-based ConfigWatcher to detect when deployed config differs from previous version
- **feat:** `_regenerate_scaffold()` calls `fraisier scaffold` on server when config changes, regenerating systemd units, nginx configs, sudoers rules
- **feat:** `_install_scaffold()` calls `fraisier scaffold-install` to deploy updated scaffold files to system locations
- **feat:** `_rollback_config()` restores previous `fraises.yaml` from git and regenerates scaffold on failed deployments
- **refactor:** APIDeployer.execute() now includes config sync/regeneration before git pull, skipping if config not present

### Install Step: No Unnecessary Sudo When User Matches Deploy User (#52)

- **fix:** `_install_dependencies()` now skips `sudo -u` prefix when `install.user` equals `deploy_user` — avoids errors when already running as the correct user
- **fix:** `webhook._run_deployment()` injects `deploy_user` into deploy_config so deployers can compare against `install.user`
- **feat:** install command runs directly when users match, uses `sudo -u` only when they differ

### Install Step: Fraise-Level Inheritance (#53)

- **feat:** `install` configuration can now be defined at the fraise level and inherited by all environments — reduces boilerplate for multi-environment deployments where install steps are identical
- **feat:** environment-level `install` takes full precedence, allowing per-env overrides when needed (e.g., different flags for production)
- **refactor:** `get_fraise_environment()` now merges fraise-level `install` with environment config

### Install Step: Enhanced Error Messages (#55)

- **feat:** `_install_dependencies()` now captures and reports comprehensive error context when install commands fail — shows exact command, exit code, stdout, stderr, and working directory
- **feat:** structured `DeploymentError` context includes `suggested_command` (e.g., `cd /var/www/api && uv sync --frozen`) for operators to reproduce failures manually
- **feat:** error messages are human-readable and actionable: operators can quickly diagnose why install failed without accessing logs
- **test:** 6 new tests covering failure scenarios, context capture, and edge cases (no-op when unconfigured, sudo prefix handling)

### Health Check: Clarify Recoverable vs Fatal Failures (#56)

- **feat:** `HealthCheckResult` now includes `transient: bool | None` field to categorize failures — service warming up (transient) vs configuration issue (fatal)
- **feat:** `HTTPHealthChecker` distinguishes error types: `URLError` and HTTP 5xx → `transient=True`, HTTP 4xx → `transient=False`
- **feat:** `check_with_retries()` shows attempt count as `X/Y` format (e.g., "attempt 2/5") instead of bare attempt number
- **feat:** transient failures log at INFO level (expected during startup), fatal failures log at WARNING level (requires intervention)
- **feat:** success after retry shows startup time: "(service took 3.2s to become ready)" — helps operators understand warm-up duration
- **feat:** final exhaustion message includes context-specific recovery hint based on error category
- **test:** 7 new tests covering error categorization, log level routing, startup time tracking, and attempt formatting

### Scaffold: Install Step Sudoers Entry with Wildcard

- **fix:** sudoers scaffold now includes `ALL` directive for install commands to support any command configuration, not just hardcoded `uv sync`

### Rollback & Edge Cases

- **feat:** deployments now support rollback to previous version when failure occurs
- **feat:** rollback includes both git worktree checkout and config restoration
- **feat:** incident files written to `/var/lib/fraisier/incidents/` on failed rollbacks for manual recovery
- **feat:** comprehensive logging of deployment lifecycle (pending, deploying, success, failed, rolled_back)
- **test:** 100+ integration tests covering rollback, edge cases, and error scenarios across all deployer types

## v0.3.10 (2026-04-01)

Feature release: systemctl wrapper for sudoers, SSH systemctl fixes. Fixes #46.

### Sudoers: systemctl Wrapper to Eliminate Wildcards (#41, #46)

- **feat:** `fraisier scaffold` now generates a systemctl wrapper script at `/usr/local/libexec/fraisier/systemctl-<project>` that validates service names and actions against the project's allowlist before executing
- **feat:** sudoers now uses a single wrapper rule per deploy user instead of 5 per-service rules, eliminating wildcards while maintaining security
- **feat:** wrapper supports all systemctl actions: start, stop, restart, status, is-active, enable, disable, daemon-reload, show
- **feat:** systemd service templates inject `FRAISIER_SYSTEMCTL_WRAPPER` env var for local runner execution
- **fix:** BareMetalProvider now uses `/usr/bin/systemctl` explicitly in all SSH commands, matching sudoers configuration (#46)
- **fix:** SystemdServiceManager checks `FRAISIER_SYSTEMCTL_WRAPPER` env var, uses wrapper when available (fallback: direct sudo systemctl)

## v0.3.9 (2026-03-31)

Bug-fix release: ship command push fix. 1663 tests, zero lint warnings.

### Ship: Fix push on new branches (#45)

- **fix:** `fraisier ship` now uses `git push -u origin HEAD` when the current branch has no upstream, instead of failing with "no upstream branch"
- **refactor:** extracted `_git_push()` helper shared by both pipeline and legacy ship paths

## v0.3.8 (2026-03-31)

Feature release: dependency install step in deploy pipeline. 1661 tests, zero lint warnings.

### Deploy: Dependency Install Step (#44)

- **feat:** deploy pipeline now supports an `install` step between git checkout and database migrations — ensures the venv is in sync with `uv.lock` before the service restarts
- **feat:** `install.command` config (list of strings) runs the install command in `app_path` after git checkout
- **feat:** `install.user` config runs the command as the app user via `sudo -u`, keeping venv ownership correct
- **feat:** install step wired into all git-based deployers (API, ETL, Scheduled)
- **feat:** sudoers scaffold generates a NOPASSWD rule for the install command when `install.user` is configured
- **fix:** deployments that change dependencies in `uv.lock` no longer fail at health check

## v0.3.5 (2026-03-31)

Feature release: three-phase rebuild, CLI improvements, better error reporting. 1593 tests, zero lint warnings.

### Three-Phase Schema Apply (#39)

- **feat:** `RebuildStrategy` now supports confiture's three-phase `build_split()` — superuser pre-schema (roles, extensions), app schema (tables, views, data), and superuser post-schema (grants on tables, role settings)
- **feat:** post-schema phase is skipped automatically when `superuser_post_files == 0`
- **refactor:** extracted shared `admin_app_conn` computation for reuse across pre and post phases
- **deps:** requires `fraiseql-confiture>=0.8.20` for three-phase `build_split()` support

### CLI: --version flag (#36)

- **feat:** `fraisier --version` now prints the installed version (e.g. `fraisier, version 0.3.5`)

### CLI: --verbose flag (#37)

- **feat:** `fraisier --verbose` / `fraisier -v` sets root logger to DEBUG for detailed diagnostic output from all subcommands

### Better psql error reporting (#38)

- **fix:** `_apply_sql` now logs stderr at ERROR level before raising, so psql failures show the actual database error instead of a generic exit code message
- **fix:** `CalledProcessError` now includes both stdout and stderr from failed psql commands

## v0.3.2 (2026-03-30)

Feature release: two-phase rebuild strategy. 1538 tests, zero lint warnings.

### Split Schema Apply (#32)

- **feat:** `RebuildStrategy` now applies schema in two phases — superuser SQL (roles, extensions) via `admin_url`, then app SQL (schemas, tables, views, data) via `database_url` — using confiture's new `build_split()` API
- **feat:** superuser phase is skipped automatically when no `superuser_dirs` are configured in the confiture environment
- **feat:** `admin_url` is rewritten to target the app database (not `postgres`) so that `CREATE EXTENSION` and `GRANT` statements land in the right place
- **deps:** requires `fraiseql-confiture>=0.8.17` for `build_split()` support

## v0.3.0 (2026-03-30)

Feature release: deploy user / app user separation. 1521 tests, zero lint warnings.

### User Separation (#28)

- **feat:** per-environment `deploy_user` override in `fraises.yaml` — different environments can use different deploy users
- **feat:** `FraisierConfig.get_deploy_user(fraise, env)` resolves effective deploy user (env-level > scaffold.deploy_user)
- **feat:** `fraisier setup` creates both deploy and app system accounts with idempotency checks
- **feat:** `fraisier setup` configures file permissions when `service.user` differs from `deploy_user` — sets app_path ownership to app user, adds deploy user to app group for write access
- **feat:** sudoers template resolves per-environment deploy_user for systemctl permissions
- **feat:** install.sh template creates app users when `service.user` is configured
- **feat:** validation checks existence of all deploy users and app users (per-env and global)
- **feat:** `triggered_by_user` field now populated with OS username in deployment records
- **feat:** `/var/lib/fraisier/status/` added to managed directories in setup
- **docs:** "User Separation" section added to `docs/security.md`

## v0.2.4 (2026-03-30)

Bug-fix release: setup installs sudoers. 1507 tests, zero lint warnings.

### Server Setup (#27)

- **feat:** `fraisier setup` now installs the sudoers fragment to `/etc/sudoers.d/` — deploy users get passwordless `systemctl stop/start/restart/status/is-active/daemon-reload` without manual configuration
- **fix:** sudoers template now includes `is-active` permission (used by health checks)

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
