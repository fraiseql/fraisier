# Phase 04: Production Robustness

## Objective
Make deploy operations atomic and recoverable: transactional git checkout, deployment-level timeouts for all deployers, and multi-server lock coordination.

## Rationale
SSH drops mid-checkout leave worktrees inconsistent. Non-API deployers have no deployment-level timeout. The DB-backed lock store exists but is never called. SIGALRM can interrupt database connections unsafely. These issues mean a real-world deploy on unreliable infrastructure can leave systems in unrecoverable states.

## Success Criteria
- [ ] Git checkout is atomic: either fully applied or fully rolled back
- [ ] All deployers have deployment-level timeouts (not just API)
- [ ] SIGALRM replaced with a safer timeout mechanism
- [ ] Dead config values removed (`environments`, `poll_interval_seconds`, `status_file`, `webhook_secret_env`)
- [ ] SQLite DB path configurable via fraises.yaml
- [ ] `deployment_id` in webhook error handler is no longer dead

## TDD Cycles

### Cycle 1: Atomic git checkout
- **RED**: Write a test that simulates `git checkout -f` failing mid-operation (mock subprocess to raise after fetch succeeds but before reset). Assert the worktree is restored to its previous state.
- **GREEN**: Modify `git/operations.py:fetch_and_checkout` to use a two-phase approach:
  1. Fetch to bare repo (safe — doesn't touch worktree)
  2. Create a temporary worktree or use `git stash` to save state
  3. Checkout + reset
  4. On failure: restore from stash or previous HEAD
  Alternative (simpler): use `git worktree add` to a temp directory, then atomic `mv` to swap. This is truly atomic on the same filesystem.
- **REFACTOR**: Extract the atomic-swap logic into a reusable `atomic_checkout()` function.
- **CLEANUP**: Lint, commit.

### Cycle 2: Safe deployment timeouts for all deployers
- **RED**: Write a test for `ETLDeployer` and `ScheduledDeployer` where a command hangs. Assert the deploy times out and returns a failure status (not hangs forever).
- **GREEN**: Replace `SIGALRM` in `api.py:23-37` with a thread-based timeout (e.g., `concurrent.futures.ThreadPoolExecutor` with `future.result(timeout=N)` or `threading.Timer`). Apply the same pattern to all deployers via `BaseDeployer`.
- **REFACTOR**: Add `timeout` as a config option in the fraise env config (already parsed as `env_config.get("timeout")` but only used by API deployer). Wire it into all deployers.
- **CLEANUP**: Lint, commit.

### Cycle 3: Remove dead config values
- **RED**: Write a test asserting that `config.environments` is removed (attribute access raises `AttributeError`). Write tests that `poll_interval_seconds`, `status_file`, and `webhook_secret_env` are not in `DeploymentConfig`.
- **GREEN**: Remove `environments` property from `config.py`. Remove `poll_interval_seconds`, `status_file`, `webhook_secret_env` from `DeploymentConfig` at `config.py:74-78`. Remove any references.
- **REFACTOR**: Add `db_path` to deployment config as a replacement for the hardcoded path in `database.py:22`.
- **CLEANUP**: Lint, commit.

### Cycle 4: Fix webhook `deployment_id` dead code
- **RED**: Write a test for `webhook._run_deployment` where the deploy fails. Assert the DB records the failure correctly (currently `deployment_id` is always None so `db.complete_deployment` is a no-op).
- **GREEN**: Either:
  - (a) Remove the dead `deployment_id` tracking from the webhook (deployers handle their own DB recording), or
  - (b) Have the webhook create the DB record before starting the deployer and pass the ID through.
  Option (a) is cleaner — the deployer already records everything.
- **REFACTOR**: Clean up the except block at `webhook.py:255` to not reference `deployment_id`.
- **CLEANUP**: Lint, commit.

### Cycle 5: Fix silent status write failures
- **RED**: Write a test where `_write_status` raises `OSError`. Assert the deployer is aware the status write failed (either re-raise or return a flag).
- **GREEN**: In `mixins.py:154`, re-raise `OSError` as `DeploymentError` (or at minimum, set a flag on the deployer that callers can check). The deployment should not silently proceed with no status tracking.
- **REFACTOR**: Consider making status writes optional (log warning) for non-critical paths, but mandatory for deploy/rollback completion.
- **CLEANUP**: Lint, commit.

## Dependencies
- Requires: Phase 01 (clean architecture), Phase 02 (rollback fixes)
- Blocks: Phase 05 (robustness tests needed)

## Status
[ ] Not Started
