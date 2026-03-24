# Phase 06: Migration-Safe Deploys

## Objective
Make fraisier the safest way to deploy confiture migrations in production — with locking DDL detection, migration dry-run, and two-phase deploy verification.

## Rationale
Fraisier's core value proposition is **migration-aware deployment with automatic rollback**. But currently the migration step is a black box: `migrate_up()` runs and either succeeds or fails. There's no way to preview what will happen, no detection of dangerous DDL (e.g., `ALTER TABLE ... ADD COLUMN ... NOT NULL` without a default on a large table), and no verification step between migration and service restart.

For the FraiseQL ecosystem, **migration safety IS zero-downtime**. A locking `ALTER TABLE` on a production PostgreSQL database causes more downtime than a `systemctl restart`. Generic TCP-level zero-downtime (socket activation, blue-green) is nginx/HAProxy's job — fraisier's job is ensuring the database migration doesn't blow up.

## Success Criteria
- [ ] `fraisier deploy --dry-run` shows exactly which migrations will run and their SQL content
- [ ] Locking DDL patterns detected and warned about before execution (configurable: warn or block)
- [ ] Two-phase deploy mode: migrate → verify schema → restart (with rollback between phases if verify fails)
- [ ] `fraisier migrations analyze <fraise> <env>` CLI command that reports pending migrations + risk assessment
- [ ] Each feature has tests

## TDD Cycles

### Cycle 1: Migration dry-run
- **RED**: Write a test where `APIDeployer.execute(dry_run=True)` with pending migrations returns a `DeploymentResult` whose `details["pending_migrations"]` lists the migration names and their SQL content, without actually applying anything.
- **GREEN**: Add a `dry_run` parameter to `MigrateStrategy.execute()`. When true, use confiture's API to list pending migrations and read their SQL files, but don't call `migrate_up()`. Wire this through `APIDeployer.execute()` and the CLI `--dry-run` flag (which currently only skips the deploy — make it show migration info too).
- **REFACTOR**: Format the dry-run output nicely with `rich` — show migration name, direction, SQL preview (first 20 lines), and whether it's reversible.
- **CLEANUP**: Lint, commit.

### Cycle 2: Locking DDL detection
- **RED**: Write a test that analyzes a migration containing `ALTER TABLE users ADD COLUMN email TEXT NOT NULL` and flags it as potentially locking. Write a test that `ALTER TABLE users ADD COLUMN email TEXT` (nullable, no default) is safe.
- **GREEN**: Create `fraisier/migration_analysis.py` with a `analyze_migration_sql(sql: str) -> list[MigrationWarning]` function. Detect known PostgreSQL locking patterns:
  - `ALTER TABLE ... ADD COLUMN ... NOT NULL` without `DEFAULT` (requires full table rewrite on PG < 11, still locks on large tables)
  - `ALTER TABLE ... ALTER COLUMN ... TYPE` (full table rewrite)
  - `CREATE INDEX` without `CONCURRENTLY` on existing tables
  - `LOCK TABLE` explicit locks
  Wire this into the deploy flow: before `migrate_up()`, analyze pending migration SQL and log warnings. Add a config option `database.block_unsafe_migrations: true` that makes these warnings into errors.
- **REFACTOR**: Make the pattern list extensible via config (users can add custom patterns). Store analysis results in the `DeploymentResult.details`.
- **CLEANUP**: Lint, commit.

### Cycle 3: Two-phase deploy with verification
- **RED**: Write a test where deploy runs migrations, then a verification step checks that the schema hash matches the expected value. If verification fails, migrations are rolled back *before* the service is restarted (so the old code never sees the new schema).
- **GREEN**: Add a `verify_after_migrate` option to the deploy config. When enabled, after `migrate_up()` succeeds:
  1. Query the database schema hash (via confiture if it supports this, or via `pg_catalog` introspection)
  2. Compare against the expected hash (from the migration's metadata or a committed `.schema_hash` file)
  3. If mismatch: roll back migrations and abort (before restart)
  4. If match: proceed to restart
  This catches the case where migrations ran but produced unexpected schema state (e.g., a migration was edited after being committed).
- **REFACTOR**: Make schema verification available as a standalone command: `fraisier verify-schema <fraise> <env>`.
- **CLEANUP**: Lint, commit.

### Cycle 4: `fraisier migrations analyze` CLI command
- **RED**: Write a test using Click's `CliRunner` that runs `fraisier migrations analyze my_api production` and asserts the output includes pending migration count, risk assessment, and SQL preview.
- **GREEN**: Create the CLI command in `cli/migrations.py`. It:
  1. Loads config for the fraise/environment
  2. Connects to confiture to list pending migrations
  3. Reads and analyzes each migration's SQL
  4. Outputs a rich-formatted report: safe migrations in green, risky ones in yellow/red with explanation
- **REFACTOR**: Add `--json` output format for CI integration. Add `--fail-on-unsafe` flag for use in CI pipelines.
- **CLEANUP**: Lint, commit.

## Dependencies
- Requires: Phase 05 (integration test infrastructure, confiture test fixtures)
- Blocks: Phase 09 (confiture integration builds on this)

## Status
[ ] Not Started
