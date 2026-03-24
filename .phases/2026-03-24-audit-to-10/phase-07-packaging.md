# Phase 07: Packaging & Distribution

## Objective
Make fraisier installable via `pip install fraisier` with correct dependency declarations and no local path hacks.

## Rationale
The README says `pip install fraisier` but the package isn't on PyPI and `pyproject.toml` has `uv.sources` pointing to `../confiture`. The `all-databases` extra promises MySQL/SQLite support that doesn't exist. `asyncssh` is imported but not declared. Users cannot install fraisier without cloning both repos side by side.

## Success Criteria
- [ ] `fraiseql-confiture` is published to PyPI (or at minimum, installable from a git URL)
- [ ] `fraisier` is published to PyPI
- [ ] `uv.sources` local path removed (or gated behind a `[tool.uv.sources]` dev override)
- [ ] `all-databases` and `graphql` extras removed (dead features from Phase 01)
- [ ] `asyncssh` declared as optional dependency in a `bare-metal` extra
- [ ] `pip install fraisier` works in a clean venv
- [ ] `pip install fraisier[bare-metal]` installs asyncssh
- [ ] Version is 0.2.0

## TDD Cycles

### Cycle 1: Clean up pyproject.toml dependencies
- **RED**: Create a test that builds the wheel (`uv build`) and installs it in an isolated venv. Assert it installs without errors. Assert `fraisier --version` works.
- **GREEN**:
  - Remove `[tool.uv.sources]` local path (move to a `pyproject.override.toml` or document as dev-only).
  - Remove `all-databases` extra (dead code removed in Phase 01).
  - Remove `graphql` extra (dead code removed in Phase 01).
  - Add `bare-metal` extra: `asyncssh>=2.14.0,<3.0`.
  - Verify `fraiseql-confiture>=0.8.11,<1.0` resolves from PyPI (if not published, use git URL as interim).
- **REFACTOR**: Review all remaining dependencies — are they all actually used?
  - `httpx` is in both `dependencies` and `dev` — remove from `dev`.
  - `jinja2` — used by scaffold. Keep.
  - `fastapi` + `uvicorn` — used by webhook server. Consider making these optional (`webhook` extra) since not all users need the webhook server.
- **CLEANUP**: Lint, commit.

### Cycle 2: Fix description and metadata
- **RED**: Read the built package metadata. Assert description doesn't say "Any language, any database, any deployment target".
- **GREEN**: Update `pyproject.toml:28` description to: "Deployment orchestrator for the FraiseQL ecosystem — migration-aware rollback for PostgreSQL applications using confiture."
  - Update classifiers to include `Topic :: System :: Installation/Setup`.
  - Verify `project.urls` point to real URLs (or remove if not live yet).
- **REFACTOR**: Add `project.license-files` if LICENSE exists.
- **CLEANUP**: Lint, commit.

### Cycle 3: Publish workflow
- **RED**: Write a GitHub Actions workflow that builds and publishes to PyPI on tag push.
- **GREEN**: Create `.github/workflows/publish.yml`:
  - Trigger on `v*` tags
  - Build with `uv build`
  - Publish with `uv publish` (or `twine`)
  - Test install from PyPI in a clean job
- **REFACTOR**: Add a `test` job that runs the full test suite before publish.
- **CLEANUP**: Lint, commit.

## Dependencies
- Requires: Phase 01 (dead extras removed), Phase 05 (tests passing)
- Blocks: Phase 08 (docs reference installable package)

## Status
[ ] Not Started
