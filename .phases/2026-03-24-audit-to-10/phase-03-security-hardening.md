# Phase 03: Security Hardening

## Objective
Close all input validation gaps, eliminate command injection vectors, and enforce defense-in-depth at every boundary.

## Rationale
The audit found path traversal via `fraise_name`, a `shell=True` code path in health checks, unvalidated `excluded_tables` passed to `pg_dump`, raw command strings on some code paths, and plaintext webhook secrets in YAML config. While many of these require operator-level access to exploit, a 10/10 security score demands zero known vectors.

Note: Cycle 4 from the original plan (asyncssh/exec_in_container escaping) is removed because Phase 01 deletes `providers/bare_metal.py` and `providers/docker_compose/provider.py` entirely.

## Success Criteria
- [ ] `fraise_name` validated everywhere (no path separators, no `..`, alphanumeric + hyphen + underscore only)
- [ ] `ExecHealthChecker` `shell=True` path removed entirely
- [ ] `excluded_tables` validated via `validate_pg_identifier` before passing to `pg_dump`
- [ ] SQL identifiers in `restore.py` properly double-quoted
- [ ] `webhook_secret` in YAML config emits a deprecation warning pointing to env var
- [ ] All fixes have regression tests

## TDD Cycles

### Cycle 1: Add `validate_fraise_name()` and enforce everywhere
- **RED**: Write tests asserting `validate_fraise_name("../../etc/cron.d/evil")` raises `ValueError`. Test valid names pass: `my-app`, `api_v2`, `frontend`.
- **GREEN**: Add `validate_fraise_name()` to `dbops/_validation.py` — regex `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$` (no path separators). Call it in:
  - `locking.py:43` (before constructing lock path)
  - `deployers/mixins.py:222` (`_write_incident`)
  - `status.py` (before constructing status file path)
  - `config.py` (at config load time for each fraise name)
  - `webhook.py:306` (before using fraise_name from webhook payload)
- **REFACTOR**: Centralize the call — validate once at config load and once at webhook entry, so downstream code can trust it.
- **CLEANUP**: Lint, commit.

### Cycle 2: Remove `shell=True` from `ExecHealthChecker`
- **RED**: Write a test that creates an `ExecHealthChecker` and confirms it always uses `shell=False`. Write a test that verifies the command is properly split via `shlex.split`.
- **GREEN**: Remove the `shell` parameter from `ExecHealthChecker.__init__()`. Always use `subprocess.run(shlex.split(self.command), shell=False, ...)`. Update the test at `tests/test_security.py:154` that explicitly tested `shell=True` — it should now test that passing `shell=True` raises `TypeError` or is simply not accepted.
- **REFACTOR**: If any caller relied on shell features (pipes, globbing), rewrite the command to not need them.
- **CLEANUP**: Lint, commit.

### Cycle 3: Validate `excluded_tables` and quote SQL identifiers
- **RED**: Write a test asserting `excluded_tables=["--help"]` raises `ValueError`. Write a test asserting `REASSIGN OWNED BY CURRENT_USER TO "my_user"` uses double-quoted identifier.
- **GREEN**:
  - In `backup.py:62`: validate each table via `validate_pg_identifier()` before `cmd.extend(["-T", table])`.
  - In `restore.py:55`: double-quote `db_owner` in the SQL: `f'REASSIGN OWNED BY CURRENT_USER TO "{db_owner}"'`.
  - Alternatively, use psql `-v` variable binding: `psql -v owner=db_owner -c 'REASSIGN OWNED BY CURRENT_USER TO :"owner"'`.
- **REFACTOR**: Audit all other SQL string constructions in `dbops/` for the same pattern.
- **CLEANUP**: Lint, commit.

### Cycle 4: Deprecate plaintext webhook secret in YAML
- **RED**: Write a test asserting that loading a config with `webhook_secret` in YAML emits a `DeprecationWarning` and logs a warning pointing to `FRAISIER_WEBHOOK_SECRET` env var.
- **GREEN**: In `webhook.py:478`, add `warnings.warn("webhook_secret in YAML config is deprecated. Use FRAISIER_WEBHOOK_SECRET env var.", DeprecationWarning, stacklevel=2)` and `logger.warning(...)`.
- **REFACTOR**: Consider removing YAML secret support entirely in v0.3.0 (document the timeline).
- **CLEANUP**: Lint, commit.

## Dependencies
- Requires: Phase 01 (providers deleted, so asyncssh/docker exec findings are moot)
- Blocks: Phase 05 (security tests needed)

## Status
[ ] Not Started
