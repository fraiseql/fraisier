# Phase 3: Security Tightening

## Objective

Close the remaining security gaps identified by the audit: unsafe environment
variable parsing and inconsistent health check command validation.

## Success Criteria

- [ ] All `int(os.getenv(...))` calls wrapped with try/except
- [ ] `ExecHealthChecker` command validated with `validate_shell_command()`
- [ ] Dynamic SQL column construction has docstring documenting safety invariant
- [ ] All fixes have regression tests

## TDD Cycles

### Cycle 1: Safe environment variable integer parsing

**Context:** `webhook_rate_limit.py:8` and `db/factory.py:64-68` use bare
`int(os.getenv(...))` without try/except. Malformed env var crashes the
webhook service (DoS vector).

- **RED**: Write tests:
  - Test: `FRAISIER_WEBHOOK_RATE_LIMIT=abc` falls back to default (10)
  - Test: `FRAISIER_WEBHOOK_RATE_LIMIT=-1` falls back to default (10)
  - Test: `FRAISIER_WEBHOOK_RATE_LIMIT=0` falls back to default (10)
  - Test: `FRAISIER_DB_POOL_MIN=abc` falls back to default (1)
  - Test: `FRAISIER_DB_POOL_MAX=abc` falls back to default (10)
  - Test: valid integer values are accepted
- **GREEN**: Create helper in a shared location (e.g. `fraisier/_env.py`):
  ```python
  def get_int_env(key: str, default: int, *, min_value: int = 0) -> int:
      """Read integer from env var with fallback. Rejects non-positive values."""
      raw = os.getenv(key)
      if raw is None:
          return default
      try:
          value = int(raw)
      except ValueError:
          logger.warning("Invalid integer for %s=%r, using default %d", key, raw, default)
          return default
      if value < min_value:
          logger.warning("%s=%d below minimum %d, using default %d", key, value, min_value, default)
          return default
      return value
  ```
  Replace bare `int(os.getenv(...))` in `webhook_rate_limit.py` and `db/factory.py`.
- **REFACTOR**: Search for any other bare `int(os.getenv(...))` calls in codebase
  and replace them too.
- **CLEANUP**: ruff check, verify webhook starts with malformed env vars.

### Cycle 2: Health check exec command validation

**Context:** `health_check.py:224` uses `shlex.split(exec_cmd)` on a
user-provided command from YAML config. While controlled by the operator,
it's inconsistent with the thorough validation applied elsewhere.

- **RED**: Write tests:
  - Test: exec command with shell metacharacters (`;`, `|`, `&&`) is rejected
  - Test: exec command with clean arguments is accepted
  - Test: empty exec command is rejected
- **GREEN**: Add `validate_shell_command(exec_cmd)` call in
  `ExecHealthChecker.__init__()` before storing the command.
- **REFACTOR**: Consider also validating the command at config load time
  (in `_validate_fraises()`) for fail-fast behavior.
- **CLEANUP**: ruff check, verify health check tests pass.

### Cycle 3: Document SQL construction safety invariant

**Context:** `db/postgres_adapter.py:178-186` uses f-strings for table/column
names in SQL. Verified safe: column names come from `dict.keys()` (app code),
values use parameterized `$N` placeholders. No injection vector exists today.

- **GREEN**: Add docstring to `insert()` documenting the safety invariant:
  "Table and column names come from application code — never pass
  user-controlled strings as keys."
- **CLEANUP**: Verify docstring is accurate, ruff check.

## Dependencies

- None (can run in parallel with Phase 1 and 2)

## Status

[x] Complete
