# Phase 08: Documentation Overhaul

## Objective
Make every document accurate, complete, and honest. No aspirational features described as shipped. No missing commands. No outdated competitor claims. Add FraiseQL ecosystem integration documentation.

## Rationale
The audit found: `rollback` and `ship` missing from CLI reference, `--if-changed` and `--no-rollback` flags undocumented, API reference missing endpoints, README claiming `pip install` works (it doesn't yet), and outdated Kamal comparisons. Documentation that lies erodes trust faster than missing documentation. Additionally, fraisier's unique position as the FraiseQL ecosystem's deployment tool is not documented at all — a new user has no idea how fraisier relates to confiture.

## Success Criteria
- [ ] Every CLI command and flag documented in `docs/cli-reference.md`
- [ ] Every webhook API endpoint documented in `docs/api-reference.md`
- [ ] `docs/confiture-integration.md` explains the fraisier ↔ confiture relationship, strategies, and migration lifecycle
- [ ] README accurately positions fraisier as FraiseQL's deployment tool (not a generic deployer)
- [ ] README installation instructions work
- [ ] `docs/architecture.md` command references are correct
- [ ] No document describes unimplemented features as available
- [ ] A doc verification test checks that all CLI commands appear in cli-reference.md

## TDD Cycles

### Cycle 1: CLI reference completeness
- **RED**: Write a test that parses `fraisier --help` (and all subcommand helps) and asserts every command and flag appears in `docs/cli-reference.md`.
- **GREEN**: Add missing sections to `docs/cli-reference.md`:
  - `fraisier rollback <fraise> <env>` with `--to-version` and `--force` flags
  - `fraisier ship patch|minor|major` with `--no-deploy`, `--message` flags
  - `fraisier migrations analyze|status|pending` (new from Phases 06/09)
  - `--if-changed` and `--no-rollback` flags on `fraisier deploy`
  - Any other missing commands/flags discovered by the test
- **REFACTOR**: Ensure consistent format across all command entries.
- **CLEANUP**: Lint, commit.

### Cycle 2: Confiture integration guide
- **RED**: Assert `docs/confiture-integration.md` exists and covers: strategy overview, migration lifecycle, configuration, troubleshooting.
- **GREEN**: Write `docs/confiture-integration.md` covering:
  - How fraisier uses confiture (strategies: migrate, rebuild, restore_migrate)
  - Configuration: `database.confiture_config`, `database.migrations_dir`, `database.strategy`
  - The deploy flow: preflight → migrate up → restart → health check → rollback (with migrate down)
  - Migration analysis and safety features (from Phase 06)
  - Common scenarios: first deploy, adding a migration, rolling back a bad migration
  - Troubleshooting: partial migration, schema drift, irreversible migrations
- **REFACTOR**: Cross-link from README and architecture docs.
- **CLEANUP**: Lint, commit.

### Cycle 3: README accuracy and positioning
- **RED**: Review every factual claim in README against current code state.
- **GREEN**: Fix:
  - Position fraisier as "the deployment orchestrator for the FraiseQL ecosystem" (not "any language, any database, any deployment target")
  - Installation: use actual installation method
  - Features list: mark anything not yet shipped as "planned" or remove
  - Remove or update competitor comparison — fraisier competes on confiture integration depth, not generic deployment breadth
  - Add a "Current Limitations" section: PostgreSQL only, single-server, requires confiture
- **REFACTOR**: Add a "FraiseQL Ecosystem" section showing how fraisier fits alongside confiture and other FraiseQL tools.
- **CLEANUP**: Lint, commit.

### Cycle 4: API reference and stale doc fixes
- **RED**: Write a test that introspects the FastAPI app routes and asserts every route appears in `docs/api-reference.md`. Grep all docs for command references and verify each one.
- **GREEN**: Add missing endpoints to API reference. Fix stale references:
  - `docs/architecture.md:57` — `fraisier config validate` → `fraisier validate`
  - `docs/deployment-guide.md:439` — clarify metrics status
  - Any other stale references
- **REFACTOR**: Add request/response examples for each API endpoint.
- **CLEANUP**: Lint, commit.

## Dependencies
- Requires: Phase 07 (packaging — so install instructions are accurate)
- Blocks: Phase 09 (polish)

## Status
[ ] Not Started
