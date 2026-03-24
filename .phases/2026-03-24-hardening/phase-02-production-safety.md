# Phase 2: Production Safety

## Objective

Address the four critical/high production risks identified by the audit:
distributed locking, SSH subprocess timeout, atomic migration rollback,
and config values that are parsed but ignored.

## Success Criteria

- [ ] Deployment lock works across machines (database-backed)
- [ ] SSH subprocess timeout kills remote processes on expiry
- [ ] Migration rollback tracks applied count and rolls back exactly N steps
- [ ] `health_check.retries` config field is actually wired to the retry logic
- [ ] `health_check.timeout` config field is wired to the health checker
- [ ] Dead config values removed or wired up
- [ ] All new behavior has regression tests

## TDD Cycles

### Cycle 1: Database-backed deployment lock

**Context:** `locking.py` uses `fcntl.flock()` — local machine only. Two CI
pipelines on different machines can race. This is the #1 production risk.

**Design:** Add a `DatabaseDeploymentLock` that uses SQLite (already used by
fraisier's internal DB layer) with a lock table. Keep `file_deployment_lock`
as fallback for single-machine deployments. Selection based on config:
`lock_backend: file | database`. SQLite with WAL mode supports concurrent
readers across machines if the DB is on shared storage (NFS/CIFS).

- **RED**: Write tests:
  - Test: `DatabaseDeploymentLock.acquire(fraise)` inserts lock row, returns True
  - Test: second `acquire(fraise)` while first held returns False
  - Test: `release(fraise)` removes lock row, next acquire succeeds
  - Test: lock with TTL expires after timeout (stale lock recovery)
  - Test: lock includes holder identity (hostname + PID)
- **GREEN**: Implement `DatabaseDeploymentLock` in `locking.py`:
  ```python
  class DatabaseDeploymentLock:
      def __init__(self, db_path: Path, ttl: int = 600):
          self.db_path = db_path
          self.ttl = ttl

      def acquire(self, fraise: str) -> bool:
          """INSERT lock row with timestamp. Return False if row exists
          and timestamp < ttl seconds ago. Reclaim if stale."""

      def release(self, fraise: str) -> None:
          """DELETE lock row."""
  ```
  Use SQLite with WAL mode (already used by fraisier's DB layer).
  Lock table: `deployment_locks(fraise TEXT PRIMARY KEY, holder TEXT, acquired_at REAL)`.
- **REFACTOR**: Add `lock_backend` to `DeploymentConfig` in `config.py`.
  `file_deployment_lock` for `file` (default, backward-compatible),
  `DatabaseDeploymentLock` for `database`.
  Update CLI `deploy` command to select lock backend from config.
- **CLEANUP**: Add migration for lock table. Document in `fraises.yaml` schema.

### Cycle 2: SSH subprocess timeout wrapper

**Context:** `deployers/api.py:155-182` uses SIGALRM for deployment timeout.
If SSH drops mid-confiture, SIGALRM fires but the remote confiture subprocess
is orphaned. The DB may be partially migrated with no cleanup.

**Design:** Replace SIGALRM with a thread-based timeout that also kills
child processes. Use `subprocess.Popen` with `process.kill()` on timeout
instead of `subprocess.run()` with SIGALRM.

- **RED**: Write tests:
  - Test: deployment that exceeds timeout is terminated (process killed)
  - Test: child processes of timed-out deployment are also killed (process group)
  - Test: timeout cleanup sets deployment status to FAILED with timeout reason
  - Test: non-timeout deployment completes normally (no interference)
- **GREEN**: Create `fraisier/timeout.py`:
  ```python
  @contextmanager
  def deployment_timeout(seconds: int, on_timeout: Callable):
      """Thread-based timeout that calls on_timeout() when time expires.
      Does NOT use SIGALRM. Safe for multi-threaded code."""
      timer = threading.Timer(seconds, on_timeout)
      timer.start()
      try:
          yield
      finally:
          timer.cancel()
  ```
  Update `runners.py` to use `subprocess.Popen` with process group kill:
  ```python
  proc = subprocess.Popen(cmd, start_new_session=True, ...)
  # On timeout: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
  ```
- **REFACTOR**: Remove `_arm_timeout()` / `_disarm_timeout()` from
  `APIDeployer`. Replace with `deployment_timeout()` context manager in
  `execute()`. Apply same timeout to all deployers via `BaseDeployer`.
- **CLEANUP**: Remove SIGALRM import. Verify timeout tests pass on CI.

### Cycle 3: Atomic migration rollback (track applied count)

**Context:** `deployers/api.py:385-430` — if migration fails halfway
(3 of 5 migrations applied), rollback must undo exactly 3. Currently,
the rollback calls `migrate_down` without knowing how many to undo.
If `migrate_down` fails (FK constraint), the DB is left in a broken state.

**Design:** Track migrations incrementally during `execute()`. On failure,
pass the exact count of applied migrations to `rollback()`.

- **RED**: Write tests:
  - Test: deploy applies 3 of 5 migrations, then fails — rollback undoes exactly 3
  - Test: deploy applies 0 migrations (all fail) — rollback skips DB rollback
  - Test: rollback of N migrations where migration M fails — incident file records
    partial state (M rolled back, N-M still applied)
- **GREEN**: Modify `MigrateStrategy.execute()` to return
  `StrategyResult(migrations_applied=N)` with the actual count.
  Modify `MigrateStrategy.rollback(count=N)` to run exactly N down migrations.
  Pass this count through `APIDeployer.rollback()`.
- **REFACTOR**: Add `migrations_applied` field to `DeploymentResult` so the
  incident file and DB record include the exact migration state.
- **CLEANUP**: Update incident file format to include migration counts.
  Verify rollback tests pass.

### Cycle 4: Wire up ignored config values

**Context:** Audit found config values that are parsed but never used:
1. `health_check.retries` — parsed in config but hardcoded to 10 in `deployers/api.py:356`
2. `health_check.timeout` — similar issue
3. Dead config keys: `poll_interval_seconds`, `webhook_secret_env`

- **RED**: Write tests:
  - Test: setting `retries: 3` in config results in exactly 3 health check retries
  - Test: setting `timeout: 15` in config uses 15s health check timeout
  - Test: config with `poll_interval_seconds` emits deprecation warning
  - Test: config with `webhook_secret_env` emits deprecation warning
- **GREEN**:
  - In `APIDeployer.execute()`, read `health_check.retries` and `health_check.timeout`
    from config and pass to `HealthCheckManager`.
  - Remove hardcoded `max_retries=10` default. Use config value or sensible default (5).
  - Add deprecation warnings for dead keys in `_validate_fraises()`.
- **REFACTOR**: Consider adding `HealthCheckConfig` dataclass to centralize
  health check settings (url, retries, timeout, backoff_factor).
- **CLEANUP**: Remove dead config parsing code for truly unused keys.
  Document deprecated keys.

## Dependencies

- None (can run in parallel with Phase 1 and 3)

## Status

[ ] Not Started
