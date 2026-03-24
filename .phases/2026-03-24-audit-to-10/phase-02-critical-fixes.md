# Phase 02: Critical Fixes

## Objective
Fix the three most dangerous production bugs: partial migration rollback, Docker Compose rollback no-op, and `restore_migrate` strategy crash.

## Rationale
These bugs undermine fraisier's core value proposition (migration-aware rollback). A user who trusts the tool and deploys a broken migration will end up with a partially migrated database and old code — the worst possible outcome. The Docker Compose rollback silently does nothing. The `restore_migrate` strategy crashes at runtime despite being a documented feature.

## Success Criteria
- [ ] `_migrations_applied` tracks incrementally (per-migration callback or confiture API)
- [ ] Partial migration failure triggers rollback of the N migrations that succeeded
- [ ] Docker Compose rollback re-deploys the previous image tag
- [ ] `restore_migrate` strategy works end-to-end with `restore_command` from config
- [ ] Each fix has a dedicated regression test that reproduces the original bug

## TDD Cycles

### Cycle 1: Fix partial migration rollback tracking
- **RED**: Write a test where `migrate_up` applies 2 of 3 migrations, then raises `MigrationError` on the 3rd. Assert that rollback calls `migrate_down --steps=2` (not 0).
- **GREEN**: Modify `APIDeployer._run_strategy()` (`api.py:195-230`) to track migrations incrementally. Options:
  - (a) If confiture's `Migrator.up()` returns a count of applied migrations before raising, use that.
  - (b) Query confiture for the current migration version before and after, compute the delta.
  - (c) Wrap the strategy call to catch `MigrationError` and extract the applied count from the error context.
  Option (b) is safest — it works regardless of how confiture reports errors.
- **REFACTOR**: Extract migration counting into a helper method `_count_applied_migrations()` so it's reusable by the rollback path.
- **CLEANUP**: Lint, commit.

### Cycle 2: Fix Docker Compose rollback
- **RED**: Write a test where `DockerComposeDeployer.rollback()` is called after a failed deploy. Assert the runner receives a command containing the previous image tag (e.g., `IMAGE_TAG=abc123 docker compose up -d`).
- **GREEN**: Modify `DockerComposeDeployer`:
  - Store `_previous_image_tag` before deploy (read from running containers via `docker compose ps --format json` or from an env file).
  - On rollback, pass the previous tag as an environment variable to the compose command.
  - If no previous tag is available, log a clear error rather than silently re-upping.
- **REFACTOR**: Consider storing the previous tag in the status file so rollback works even after process restart.
- **CLEANUP**: Lint, commit.

### Cycle 3: Fix `restore_migrate` strategy wiring
- **RED**: Write a test that configures a fraise with `strategy: restore_migrate` and `restore_command: pg_restore ...`, then calls `APIDeployer.execute()`. Assert it doesn't crash and calls the restore command.
- **GREEN**: Modify `APIDeployer._run_strategy()` at `api.py:199` to read `restore_command` from `self.db_config` and pass it to `get_strategy()`. Add config validation: if `strategy: restore_migrate` but no `restore_command`, fail at config load time (not deploy time).
- **REFACTOR**: Add the validation to `config.py` validation logic so `fraisier validate` catches this.
- **CLEANUP**: Lint, commit.

### Cycle 4: Integration test for full rollback scenario
- **RED**: Write a test that simulates the full deploy → health-check-fail → rollback cycle for each deployer type. For API deployer: assert git rolled back, migrations rolled back, service restarted, status recorded as ROLLED_BACK.
- **GREEN**: Fix any issues discovered during integration testing.
- **REFACTOR**: Ensure rollback result includes accurate counts and error messages.
- **CLEANUP**: Lint, commit.

## Dependencies
- Requires: Phase 01 (clean architecture to work against)
- Blocks: Phase 04, 05

## Status
[ ] Not Started
