# Phase 09: Confiture Integration & Polish

## Objective
Make fraisier the best possible interface to confiture migrations, with deep CLI integration, schema verification, and polished error UX.

## Rationale
Fraisier is the FraiseQL ecosystem's deployment tool. Its competitive edge isn't generic deployment features — it's the deepest, most trustworthy confiture integration available. Users should be able to manage their entire migration lifecycle through fraisier, from checking pending migrations to verifying production schema state. The remaining polish items (error UX, config API, thread safety) round out the developer experience.

## Success Criteria
- [ ] `fraisier migrations status <fraise> <env>` shows current migration version and pending count
- [ ] `fraisier migrations pending <fraise> <env>` lists pending migrations with details
- [ ] `fraisier status` output includes migration version alongside deploy version
- [ ] Error messages include actionable remediation hints
- [ ] `webhook.py` no longer accesses `config._config` (private attribute)
- [ ] Webhook rate limiter is thread-safe
- [ ] `fraisier validate` catches all config issues before deploy (including confiture config reachability)

## TDD Cycles

### Cycle 1: `fraisier migrations status` and `fraisier migrations pending`
- **RED**: Write tests using Click's `CliRunner`:
  - `fraisier migrations status my_api production` outputs current version, pending count, last migration timestamp
  - `fraisier migrations pending my_api production` lists each pending migration with name and whether it's reversible
- **GREEN**: Create `cli/migrations.py` (extending the `analyze` command from Phase 06). Add `status` and `pending` subcommands. These connect to confiture's API to query migration state for the configured database.
- **REFACTOR**: Add the migration version to `fraisier status` output so operators see both deploy SHA and DB migration version in one view.
- **CLEANUP**: Lint, commit.

### Cycle 2: Fix config API access in webhook
- **RED**: Write a test asserting `webhook.py` does not access `config._config` (grep or ast-based check).
- **GREEN**: Add public methods to `FraisierConfig` for every config value the webhook needs:
  - `config.get_lock_dir()` instead of `config._config.get("deployment", {}).get("lock_dir")`
  - `config.get_git_provider_config(fraise_name)` instead of reaching into `_config`
  - etc.
- **REFACTOR**: Apply the same pattern to any other module accessing `_config` directly.
- **CLEANUP**: Lint, commit.

### Cycle 3: Thread-safe rate limiter
- **RED**: Write a concurrent test (using `threading`) that hammers the rate limiter from multiple threads. Assert no `KeyError`, `RuntimeError`, or data corruption.
- **GREEN**: Add a `threading.Lock` to `webhook_rate_limit.py` around the `OrderedDict` mutations. Or replace with a `collections.Counter` + lock.
- **REFACTOR**: Consider whether the rate limiter should be per-process (current) or shared (Redis-backed). For v0.2.0, per-process with thread safety is sufficient.
- **CLEANUP**: Lint, commit.

### Cycle 4: Improve error messages with remediation hints
- **RED**: Test the error output for common failure scenarios: wrong fraise name, missing config, SSH auth failure, migration failure, health check timeout. Assert each error includes a remediation hint.
- **GREEN**: Add `hint` fields to `DeploymentError` and its subclasses. Format errors as:
  ```
  Error: Health check failed after 30s (http://localhost:8000/health returned 503)
  Hint: Check that the service is running: systemctl status my-app
  ```
  Add migration-specific hints:
  ```
  Error: Migration 003_add_email failed: column "email" already exists
  Hint: The migration may have been partially applied. Check schema state:
        fraisier migrations status my_api production
  ```
- **REFACTOR**: Ensure CLI catches errors and displays them with `rich` formatting rather than raw tracebacks.
- **CLEANUP**: Lint, commit.

### Cycle 5: Enhanced `fraisier validate` with confiture checks
- **RED**: Write a test where `fraisier validate` with a properly configured fraise also checks that the confiture config file exists, the migrations directory exists, and (optionally) the database is reachable.
- **GREEN**: Extend `cli/ops.py:validate` to:
  1. Validate fraises.yaml structure (existing)
  2. For each fraise with `database` config: check `confiture_config` path exists
  3. For each fraise with `database` config: check `migrations_dir` path exists
  4. Optionally (`--check-db`): attempt to connect to the database and verify confiture schema table exists
- **REFACTOR**: Add `--strict` flag that treats warnings as errors (useful in CI).
- **CLEANUP**: Lint, commit.

## Dependencies
- Requires: Phase 06 (migration analysis foundation), Phase 08 (docs)
- Blocks: Phase 10 (finalize)

## Status
[ ] Not Started
