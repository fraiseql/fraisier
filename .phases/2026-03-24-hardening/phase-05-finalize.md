# Phase 5: Finalize

## Objective

Clean all development artifacts, verify quality gates, prepare for release.

## Success Criteria

- [ ] `git grep -iE 'todo|fixme|hack|xxx|temp'` (outside .phases) returns nothing
- [ ] `uv run ruff check` clean (zero warnings)
- [ ] `uv run ruff format --check` clean
- [ ] `uv run pytest` 100% pass (no skips, no xfail)
- [ ] No commented-out code (ERA rule in ruff)
- [ ] No unused imports
- [ ] No dead functions/classes
- [ ] CHANGELOG.md updated with hardening changes
- [ ] All docs still accurate after refactoring
- [ ] `.phases/` directory not shipped (add to .gitignore if not already)

## TDD Cycles

### Cycle 1: Quality control review

Review the entire codebase as a senior software engineer would.

- **RED**: Run full lint + type check + test suite:
  ```bash
  uv run ruff check --fix
  uv run ruff format
  uv run pytest -x -q
  ```
  Fix any failures.
- **GREEN**: Address each warning/error. For ruff findings:
  - Fix the code (preferred)
  - Add targeted `# noqa: XXXX` with justification comment (last resort)
- **REFACTOR**: Review changes from Phases 1-4 for:
  - API consistency (do extracted classes follow naming conventions?)
  - Error handling completeness
  - Import cleanliness
- **CLEANUP**: One final `ruff check && ruff format --check && pytest`

### Cycle 2: Archaeology removal

Remove all development markers and artifacts.

- **RED**: Run `git grep -iE 'todo|fixme|hack|phase|xxx|temp'` and list results.
- **GREEN**: For each match:
  - If it's a real TODO: either fix it now or delete the comment
  - If it's a phase reference: remove it
  - If it's debug code: remove it
  - If it's a legitimate use (e.g., `tempfile`): leave it
- **REFACTOR**: Check for any commented-out code blocks that survived.
- **CLEANUP**: Re-run grep to confirm clean.

### Cycle 3: Release preparation

- **RED**: Verify all user-facing changes are documented:
  - New config options (`lock_backend`, etc.)
  - Behavioral changes (retry count from config, not hardcoded)
  - Deprecation warnings for dead config keys
- **GREEN**: Update CHANGELOG.md with a "Hardening" section listing
  all changes grouped by category (breaking, features, fixes, internal).
- **REFACTOR**: Review README.md for accuracy after refactoring.
  Ensure installation instructions and examples still work.
- **CLEANUP**: Final commit, tag if appropriate.

## Dependencies

- All prior phases (1, 2, 3, 4)

## Status

[x] Complete (2026-03-25)
