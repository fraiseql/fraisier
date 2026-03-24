# Phase 05: Real Tests

## Objective
Replace mock-heavy "E2E" tests with integration tests that verify real behavior, and cover the untested public functions.

## Rationale
The test suite has 1138 passing tests but `test_e2e_deployments.py` patches 5+ methods per test, verifying mock call order rather than system behavior. A 10/10 test quality score requires tests that catch real bugs, not tests that confirm mocks were called. As the FraiseQL ecosystem's deployment tool, the confiture migration integration path is the most critical thing to test well.

## Success Criteria
- [ ] `test_e2e_deployments.py` replaced with tests that run against real git repos (temp dirs) and real subprocess calls
- [ ] At least 90% of public functions have test coverage
- [ ] No test patches more than 3 things (if it needs more, the test is at the wrong level)
- [ ] Integration tests for: deploy → rollback, webhook → deploy, scaffold → usable output
- [ ] Confiture integration tests: migrate up → verify → migrate down → verify
- [ ] All tests still run in < 30 seconds (no network dependencies in CI)

## TDD Cycles

### Cycle 1: Build integration test infrastructure
- **RED**: Write a test fixture `real_git_repo` that creates a temp directory with a real git repo (bare + worktree), a simple Python app with a health endpoint, and confiture migration files. Assert the fixture produces a working git repo.
- **GREEN**: Implement the fixture in `tests/integration/conftest.py`. Use `subprocess.run` to create real git repos. Create confiture migration files (up.sql/down.sql) that run against a test SQLite or in-memory database.
- **REFACTOR**: Make the fixture parametrizable for different scenarios (with/without migrations, with/without health check).
- **CLEANUP**: Commit.

### Cycle 2: Rewrite E2E deploy tests
- **RED**: Write an integration test: create a real git repo, configure a fraise pointing at it, call `APIDeployer.execute()` with `LocalRunner`. Assert the worktree has the new SHA, the status file exists, the DB record was created.
- **GREEN**: Implement. Use the real `LocalRunner` with real subprocess calls. Mock only the `systemctl restart` (since we don't have a real service) and the health check HTTP call (use a local test server or mock at the network level only).
- **REFACTOR**: Add variants: deploy with migration, deploy with health check failure triggering rollback, deploy with `--dry-run`.
- **CLEANUP**: Delete `tests/test_e2e_deployments.py`. Commit.

### Cycle 3: Rewrite webhook integration tests
- **RED**: Write an integration test using FastAPI `TestClient`. POST a real GitHub-shaped webhook payload with a valid HMAC signature. Assert the webhook triggers a deploy (mock only the deployer's external calls, not the webhook → deployer wiring).
- **GREEN**: Implement. Use `TestClient` from `httpx` (already a dependency). Verify the full chain: payload parsing → signature verification → fraise config lookup → deployer instantiation → deploy execution.
- **REFACTOR**: Test all four git providers (GitHub, GitLab, Gitea, Bitbucket) with their respective payload shapes and signature schemes.
- **CLEANUP**: Commit.

### Cycle 4: Cover untested public functions — CLI and internal APIs
- **RED**: Write tests for: `backup_cmd`, `db_check`, `status_all`, `provider_info`, `provider_test`, `metrics_endpoint` using Click's `CliRunner`. Write tests for `health_check.check_and_record_metrics`, webhook endpoints.
- **GREEN**: Implement. Focus on testing the happy path and one error path per command.
- **REFACTOR**: Extract common CLI test patterns into helpers.
- **CLEANUP**: Commit.

### Cycle 5: Confiture integration tests
- **RED**: Write integration tests that exercise the full strategy → confiture path: create real migration files, run `MigrateStrategy.execute()`, verify the database was migrated, run `MigrateStrategy.rollback()`, verify the migration was reversed.
- **GREEN**: Implement using confiture's test utilities or a real SQLite database. Test all three strategies (migrate, rebuild, restore_migrate).
- **REFACTOR**: Add edge case tests: empty migration dir, irreversible migration with `allow_irreversible=False`, partially failing migration set.
- **CLEANUP**: Run full suite, verify <30s. Commit.

## Dependencies
- Requires: Phase 02 (critical fixes), Phase 03 (security), Phase 04 (robustness) — tests need the fixes in place
- Blocks: Phase 06, 07

## Status
[ ] Not Started
