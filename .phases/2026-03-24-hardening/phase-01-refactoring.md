# Phase 1: Refactoring & Code Quality

## Objective

Break apart god objects, shorten long functions, deduplicate shared patterns,
and fix error propagation gaps — without changing external behavior.

## Success Criteria

- [ ] `APIDeployer.rollback()` is under 50 lines, split into focused methods
- [ ] `process_webhook_event()` and `generic_webhook()` each under 50 lines
- [ ] Service restart logic exists in one place, used by all callers
- [ ] Health check retry logic exists in one place
- [ ] Config loading in `webhook.py` happens once per request, not 9 times
- [ ] Status write failures are logged with context (not silently swallowed)
- [ ] DB record failures log at WARNING with deployment_id for correlation
- [ ] All tests pass, ruff check clean, zero behavior changes

## TDD Cycles

### Cycle 1: Refactor APIDeployer.rollback()

**Context:** `deployers/api.py:375-491` — 117 lines handling DB rollback,
git rollback, service restart, health check, incident writing. Too many
responsibilities in one method.

- **RED**: Write tests that assert rollback behavior is preserved after refactoring:
  - Test: rollback with successful DB migrate-down + git checkout
  - Test: rollback with failed DB migrate-down writes incident file
  - Test: rollback with no previous SHA returns ROLLBACK_FAILED
  - (These may already exist in `test_api_rollback.py` — verify coverage first,
    add missing cases)
- **GREEN**: Extract `rollback()` into:
  - `_rollback_database(strategy, target_count) -> StrategyResult`
  - `_rollback_git(target_sha) -> bool`
  - `_finalize_rollback(db_result, git_ok, old_sha, new_sha) -> DeploymentResult`
  - Keep `rollback()` as thin orchestrator calling these three
- **REFACTOR**: Deduplicate strategy resolution between `execute()` (line ~199)
  and `rollback()` (line ~389) — extract `_resolve_strategy()` method
- **CLEANUP**: ruff check, remove dead code, verify all tests pass

### Cycle 2: Extract SystemdServiceManager

**Context:** Service restart logic duplicated across `deployers/api.py:232-241`,
`providers/bare_metal.py:383-401`, and similar patterns in other deployers.

- **RED**: Write tests for a new `SystemdServiceManager` class:
  - Test: `restart(service_name)` calls `systemctl restart` via runner
  - Test: `restart(invalid_name)` raises ValueError (validation)
  - Test: `status(service_name)` returns parsed systemctl output
- **GREEN**: Create `fraisier/systemd.py` with `SystemdServiceManager`:
  ```python
  class SystemdServiceManager:
      def __init__(self, runner: Runner): ...
      def restart(self, service_name: str, timeout: int = 60) -> None: ...
      def status(self, service_name: str) -> str: ...
  ```
- **REFACTOR**: Replace direct systemctl calls in `APIDeployer._restart_service()`,
  `BareMetalProvider.restart_service()` with `SystemdServiceManager` calls
- **CLEANUP**: Delete now-dead service restart code from deployers/providers

### Cycle 3: Consolidate health check retry logic

**Context:** `health_check.py:286` has `check_with_retries()` with exponential
backoff. `providers/base.py:104-167` has `check_health()` with its own retry
logic. Same concept, different implementations.

- **RED**: Write tests asserting unified retry behavior:
  - Test: retries with exponential backoff (initial_delay, backoff_factor, max_delay)
  - Test: stops after max_retries
  - Test: returns first success immediately
- **GREEN**: Make `HealthCheckManager.check_with_retries()` the single source of
  truth. Have provider `check_health()` delegate to it.
- **REFACTOR**: Remove retry logic from `providers/base.py`. Update all callers.
- **CLEANUP**: Verify no duplicate retry implementations remain

### Cycle 4: Refactor webhook.py long functions

**Context:** `process_webhook_event()` (103 lines, `webhook.py:265`) handles
auth, routing, and dispatch. `generic_webhook()` (91 lines, `webhook.py:371`)
handles provider detection + signature verification + event processing.
Config loaded 9 times across the file.

- **RED**: Existing webhook tests should cover behavior — verify coverage,
  add tests for edge cases (unknown provider, malformed payload, missing header)
- **GREEN**: Split into focused functions:
  - `_detect_git_provider(headers) -> str` (provider auto-detection)
  - `_verify_signature(provider, body, headers) -> None` (signature check)
  - `_normalize_event(provider, payload, headers) -> WebhookEvent` (event parsing)
  - `_dispatch_deployment(event, config) -> DeploymentResult` (deployment trigger)
  - Load config once at request start, pass as argument
- **REFACTOR**: Reduce `generic_webhook()` to thin orchestrator calling the four
  functions above. Same for `process_webhook_event()`.
- **CLEANUP**: Remove duplicate config loading. ruff check.

### Cycle 5: Fix error propagation gaps

**Context:** Three places where errors are silently swallowed:
1. `deployers/mixins.py:152-155` — status write OSError logged as warning
2. `deployers/mixins.py:175-177` — DB record failure returns None silently
3. `webhook.py:255` — generic Exception catch loses specifics

- **RED**: Write tests asserting:
  - Status write failure logs at WARNING with fraise name and path
  - DB record failure logs at WARNING with deployment_id
  - Webhook exception handler preserves exception class name in error response
- **GREEN**: Improve logging in all three locations. Add structured context
  (fraise_name, deployment_id, file path) to warning messages. In webhook,
  catch specific exceptions (DeploymentError, ConfigurationError, OSError)
  before the generic fallback.
- **REFACTOR**: Consider whether status write failure should retry once
  (transient disk issue) before giving up.
- **CLEANUP**: Verify no new exceptions are swallowed

## Dependencies

- None (pure refactoring, no new features)

## Status

[ ] Not Started
