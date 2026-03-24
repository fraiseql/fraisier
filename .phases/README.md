# Fraisier v0.1.0 — First Published Release

## Goal

Transform fraisier from an unpublished prototype (983 tests) into a
publishable v0.1.0 deployment orchestrator that teams can start using.

## Guiding Principles

- Fix safety gaps before adding features
- Every new feature ships with tests and docs
- No phase ships with ruff or ty warnings

## Phase Overview

| Phase | Title | Focus | Status |
|-------|-------|-------|--------|
| 1 | Harden Core | Fix exception handling, lock timeout, timeout-rollback gap | [ ] |
| 2 | Notification System | Notifier protocol + issue creation + Slack/Discord | [ ] |
| 3 | Docker Compose Validation | Service name validation, docker cp path traversal | [ ] |
| 4 | Rollback Everywhere | ETL, Scheduled, Docker Compose rollback | [ ] |
| 5 | Pre-flight Validation | SSH, DB, git connectivity in `validate` | [ ] |
| 6 | Provider Refactoring | Split god objects, extract command builders | [ ] |
| 7 | Documentation & v0.1.0 | Docs accuracy, webhook quickstart, version bump | [ ] |
| 8 | Finalize | Archaeology removal, security audit, release | [ ] |

## Current Status

Starting Phase 1.
