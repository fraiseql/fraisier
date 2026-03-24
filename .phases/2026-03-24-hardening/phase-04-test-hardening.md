# Phase 4: Test Hardening

## Objective

Replace mock-heavy tests on critical paths with integration tests that verify
real behavior. Fix test database isolation. Achieve high confidence that
rollback, the most critical feature, actually works.

## Success Criteria

- [ ] Rollback path tested with real git repo + real confiture migrations
- [ ] No test patches more than 3 things on critical paths
- [ ] Database tests use isolated state (no cross-test contamination)
- [ ] At least one integration test for each: deploy→rollback, webhook→deploy
- [ ] All tests run in < 30 seconds

## TDD Cycles

### Cycle 1: Fix test database isolation

**Context:** `tests/test_database.py` creates/modifies shared SQLite database.
No teardown between tests. Risk of ordering dependencies and flaky tests.

- **RED**: Demonstrate the isolation problem:
  - Test A writes to DB, Test B reads — should not see A's data
  - If tests currently share state, this test will fail
- **GREEN**: Add autouse fixture in `conftest.py`:
  ```python
  @pytest.fixture(autouse=True)
  def _isolated_db(tmp_path, monkeypatch):
      """Each test gets a fresh SQLite database."""
      db_path = tmp_path / "test_fraisier.db"
      monkeypatch.setenv("FRAISIER_DB_PATH", str(db_path))
      # Reset any cached DB connections
      yield
  ```
- **REFACTOR**: Update `test_database.py` to use the fixture. Remove any
  manual DB path setup that's now redundant.
- **CLEANUP**: Verify no test depends on another test's DB state.

### Cycle 2: Integration test infrastructure

**Context:** Need real git repo + confiture migration fixtures to test
deploy→rollback without mocking.

- **RED**: Write fixture that creates a temporary git bare repo with:
  - Two commits (v1 and v2)
  - A confiture migration directory with one reversible migration
  - A worktree checkout
- **GREEN**: Create `tests/fixtures/` with:
  ```python
  @pytest.fixture
  def git_deploy_env(tmp_path):
      """Real git bare repo + worktree + confiture migration."""
      bare = tmp_path / "app.git"
      worktree = tmp_path / "app"
      migrations = tmp_path / "migrations"
      # git init --bare, create commits, setup worktree
      # Create confiture.toml + one migration (create table / drop table)
      return DeployEnv(bare=bare, worktree=worktree, migrations=migrations,
                       sha_v1=..., sha_v2=...)
  ```
- **REFACTOR**: Make fixture configurable (number of commits, number of
  migrations) for different test scenarios.
- **CLEANUP**: Verify fixture creates valid git state.

### Cycle 3: Real rollback integration tests

**Context:** `test_rollback_everywhere.py` has 2.2 mocks/test for the most
critical code path. Need real tests that exercise git checkout + confiture
migrate down.

- **RED**: Write integration tests (marked `@pytest.mark.integration`):
  - Test: deploy v2 → health check fails → rollback restores v1 code + v1 schema
  - Test: deploy v2 → migration 2/3 fails → rollback undoes migrations 1-2
  - Test: deploy v2 → rollback succeeds → status file shows ROLLED_BACK
  - Test: deploy v2 → rollback fails (migration down error) → status ROLLBACK_FAILED
    + incident file created
- **GREEN**: Use `git_deploy_env` fixture. Mock only:
  1. `systemctl restart` (can't restart real service in test)
  2. HTTP health check endpoint (no real service to check)
  Everything else (git, confiture, file system) runs for real.
- **REFACTOR**: Ensure tests are deterministic — no timing dependencies,
  no network calls, no shared state.
- **CLEANUP**: Consider whether any existing mock-heavy rollback tests can
  be deleted now that integration tests cover the same scenarios.

### Cycle 4: Webhook integration tests

**Context:** Webhook → deploy chain is tested but could use a full-chain
integration test with FastAPI TestClient.

- **RED**: Write integration test:
  - Test: POST /webhook with valid GitHub signature → triggers deployment
    → returns 200 with deployment result
  - Test: POST /webhook with invalid signature → returns 401
  - Test: POST /webhook for unconfigured branch → returns 200 with "skipped"
- **GREEN**: Use FastAPI `TestClient` with real config (from `sample_config`
  fixture). Mock only systemctl + health check (same as Cycle 3).
- **REFACTOR**: Ensure webhook tests clean up background tasks.
- **CLEANUP**: Verify all webhook test edge cases are covered.

### Cycle 5: Cover untested public functions

**Context:** Audit identified `WebhookEventStore` and some deployer edge
cases as under-tested.

- **RED**: Identify all public functions with zero test coverage:
  - Run `uv run pytest --co -q` and compare with source
  - Focus on public methods in: deployers, config, database, webhook
- **GREEN**: Write focused unit tests for each uncovered function.
  Prioritize by risk: deployer methods > config methods > utility methods.
- **REFACTOR**: If a public function is truly unreachable, consider making
  it private or removing it.
- **CLEANUP**: Final test count and coverage check.

## Dependencies

- Phase 1 (refactored rollback code is what we're testing)
- Phase 2 (new safety features need test coverage)

## Status

[ ] Not Started
