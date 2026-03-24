# Phase 10: Finalize

## Objective
Transform the codebase into a production-ready, evergreen repository with zero development artifacts, passing lints, and a clean release.

## Rationale
Per the Eternal Sunshine Principle: a repository should look like it was written in one perfect session. After nine phases of fixes, there will be TODO markers, phase references, temporary workarounds, and rough edges. This phase removes all of them.

## Success Criteria
- [ ] `git grep -iE 'todo|fixme|hack|phase|xxx|temp' -- ':!.phases'` returns nothing
- [ ] `uv run ruff check` returns zero warnings
- [ ] `uv run ruff format --check` returns zero changes needed
- [ ] `uv run pytest` passes 100% (zero skips unless genuinely platform-conditional)
- [ ] No commented-out code anywhere
- [ ] No unused imports
- [ ] No dead functions or classes
- [ ] `.phases/` removed from the shipping branch (kept in a tag or separate branch for history)
- [ ] CHANGELOG.md updated for v0.2.0
- [ ] Version bumped to 0.2.0
- [ ] README is accurate and complete
- [ ] All docs are current

## TDD Cycles

### Cycle 1: Quality control review
- **RED**: Run `ruff check`, `ruff format --check`, `pytest`. Collect all warnings and failures.
- **GREEN**: Fix every warning and failure. Common issues:
  - Unused imports from deleted modules
  - Formatting drift from manual edits
  - Test failures from changed APIs
- **REFACTOR**: Review each module as a senior engineer would:
  - Is the API intuitive and consistent?
  - Are error paths comprehensive?
  - Are edge cases handled?
  - Is complexity justified?
- **CLEANUP**: Commit.

### Cycle 2: Archaeology removal
- **RED**: Run `git grep -iE 'todo|fixme|hack|phase|xxx|temp|wip' -- ':!.phases' -- ':!CHANGELOG.md'`. Every match is a failure.
- **GREEN**: For each match:
  - If it's a real TODO: either fix it now or delete it (this is the last phase — no more deferring)
  - If it's a phase reference: remove
  - If it's commented-out code: delete
  - If it's a debug print/log: remove
- **REFACTOR**: Read through every file changed in phases 01-09. Look for:
  - Temporary workarounds that should be permanent solutions
  - Inconsistent naming between old and new code
  - Tests that test old behavior instead of current behavior
- **CLEANUP**: Commit.

### Cycle 3: Release
- **RED**: Final full verification:
  - `uv run ruff check` — clean
  - `uv run ruff format --check` — clean
  - `uv run pytest -x` — all pass
  - `uv build` — succeeds
  - Install wheel in clean venv — `fraisier --version` shows 0.2.0
  - `fraisier --help` — all commands listed
  - `fraisier validate` on example config — passes
- **GREEN**: Fix any remaining issues.
- **REFACTOR**:
  - Update CHANGELOG.md with all changes since v0.1.0
  - Bump version to 0.2.0 in `pyproject.toml` and `__init__.py`
  - Remove `.phases/2026-03-24-audit-to-10/` from the main branch (archive as a git tag: `git tag phases/2026-03-24-audit-to-10`)
  - Write release notes
- **CLEANUP**: Final commit. Tag `v0.2.0`.

## Dependencies
- Requires: All previous phases complete
- Blocks: Nothing (this is the end)

## Status
[ ] Not Started
