# Phase 01: Surgery

## Objective
Remove ~2500 lines of dead code and collapse the dual providers/deployers abstraction into a single coherent layer.

## Rationale
The codebase has two parallel hierarchies (`providers/` and `deployers/`) that serve overlapping purposes but are not connected. The `providers/` layer is never called from the real deploy path. The `db/` subpackage contains a multi-database adapter layer (SQLite, PostgreSQL, MySQL) that contradicts the FraiseQL/PostgreSQL-only focus — the actual migration path goes through `fraisier.dbops.confiture` → `confiture` directly. This dead weight confuses contributors, inflates test counts with meaningless coverage, and creates the false impression of features that don't exist.

## Success Criteria
- [ ] `providers/` deleted — no parallel abstraction
- [ ] `db/adapter.py`, `db/postgres_adapter.py`, `db/observability.py`, `db/migrations.py`, `db/factory.py` removed (keep only `db/state.py`, `db/history.py`, `db/lock_store.py`, `db/webhook_store.py` — fraisier's own SQLite state)
- [ ] `ProviderRegistry`, `ProviderConfig`, provider emit hooks removed
- [ ] `all-databases` and `graphql` optional deps removed from pyproject.toml
- [ ] `graphql` keyword removed from pyproject.toml
- [ ] Deployer factory extracted into single shared function (no duplication between CLI and webhook)
- [ ] All tests pass
- [ ] `ruff check` clean

## TDD Cycles

### Cycle 1: Delete dead `db/` adapter layer
- **RED**: Write a test that imports `fraisier.db` and asserts the public API contains only the state-management components (`db/state.py`, `db/history.py`, `db/lock_store.py`, `db/webhook_store.py`). Verify it fails because the multi-DB adapter classes still exist.
- **GREEN**: Delete `db/adapter.py`, `db/postgres_adapter.py`, `db/observability.py`, `db/migrations.py`, `db/factory.py`. Update `db/__init__.py` to export only what's real. Remove `tests/test_observability.py` and any tests for deleted code.
- **REFACTOR**: Check whether `database.py` (the SQLite state layer) imported anything from the deleted modules. If so, inline or rewrite.
- **CLEANUP**: Remove `all-databases` and `graphql` optional deps from pyproject.toml. Remove `graphql` from keywords. Run `ruff check --fix`, commit.

### Cycle 2: Delete `providers/` entirely
- **RED**: Verify that no deployer, strategy, or webhook code imports from `fraisier.providers`. The only imports should be from `cli/providers.py` (provider-test/provider-info commands) and tests.
- **GREEN**: Delete `providers/base.py`, `providers/bare_metal.py`, `providers/docker_compose/provider.py`, `providers/docker_compose/__init__.py`, `providers/__init__.py`.
- **REFACTOR**: The `cli/providers.py` commands (`fraisier providers`, `fraisier provider-test`, `fraisier provider-info`) need to either be deleted or rewritten. Since they provide useful SSH connectivity and Docker Compose validation diagnostics, rewrite them as simple functions in `validation.py` and keep the CLI commands pointing there.
- **CLEANUP**: Delete `tests/test_providers.py` and `tests/test_provider_integration.py`. Commit.

### Cycle 3: Rewrite diagnostic CLI commands
- **RED**: Write tests for diagnostic functions: `test_ssh_connectivity(host, user, port)` and `test_docker_compose_config(compose_file)`.
- **GREEN**: Implement these as simple functions in `validation.py`. Wire `cli/providers.py` (rename to `cli/diagnostics.py`) to call them.
- **REFACTOR**: Rename the CLI commands: `fraisier provider-test` → `fraisier check-ssh` / `fraisier check-compose`. Keep the old names as hidden aliases during v0.2.x.
- **CLEANUP**: Commit.

### Cycle 4: Extract shared deployer factory
- **RED**: Write a test asserting `get_deployer(fraise_config, env_config, ...)` returns the correct deployer class for each type (`api`, `etl`, `scheduled`, `docker_compose`, `backup`). Test that unknown types raise `ValueError`.
- **GREEN**: Extract the deployer factory from `cli/_helpers.py:74-117` into `deployers/__init__.py` or `deployers/factory.py`.
- **REFACTOR**: Update `cli/_helpers.py` and `webhook.py:206` to both call the shared factory. Ensure the webhook path now supports `scheduled` and `backup` types (fixing the behavioral divergence).
- **CLEANUP**: Lint, commit.

### Cycle 5: Remove speculative abstractions
- **RED**: Verify `ProviderRegistry`, `ProviderConfig`, and any remaining provider-era types are no longer imported anywhere.
- **GREEN**: Delete any remaining references.
- **REFACTOR**: Simplify `deployers/base.py` if any methods are now unnecessary.
- **CLEANUP**: Lint, commit.

### Cycle 6: Verify and measure
- **RED**: Run full test suite — must pass. Run `ruff check` — must be clean.
- **GREEN**: Fix any regressions.
- **REFACTOR**: Count lines removed. Target: net -2000 lines minimum.
- **CLEANUP**: Final commit for phase.

## Dependencies
- Requires: Nothing (first phase)
- Blocks: Phase 02, 03, 04, 05

## Status
[ ] Not Started
