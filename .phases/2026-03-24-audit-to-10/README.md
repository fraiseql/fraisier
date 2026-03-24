# Fraisier: Audit Remediation — Road to 10/10

**Created:** 2026-03-24
**Baseline scores:** Code Quality 5, Test Quality 6, Production Readiness 4, Security 7, Feature Completeness 6, Documentation 5, Competitive Position 4

**Target:** 10/10 on every axis — as the FraiseQL ecosystem's deployment tool.

**Design principle:** Fraisier is not a generic deployment tool competing with Kamal or Capistrano. It is the **specialized deployment orchestrator for FraiseQL applications using confiture migrations on PostgreSQL**. Every feature should deepen the confiture/PostgreSQL integration rather than chase generic deployment breadth.

## Phase Overview

| Phase | Title | Focus | Estimated Cycles |
|-------|-------|-------|-----------------|
| 01 | Surgery | Remove dead code, collapse dual abstractions | 6 |
| 02 | Critical Fixes | Partial-migration rollback, Docker Compose rollback, restore_migrate | 4 |
| 03 | Security Hardening | Path traversal, shell injection, input validation (asyncssh cycle removed — providers deleted in 01) | 4 |
| 04 | Production Robustness | Atomic git checkout, safe timeouts, dead config cleanup | 5 |
| 05 | Real Tests | Replace mock-heavy "E2E" with integration tests, cover gaps | 5 |
| 06 | Migration-Safe Deploys | Locking DDL detection, migration dry-run, two-phase deploy | 4 |
| 07 | Packaging & Distribution | PyPI publish, dependency cleanup, extras rationalization | 3 |
| 08 | Documentation Overhaul | CLI reference, confiture integration guide, README accuracy | 4 |
| 09 | Confiture Integration & Polish | Deep migration CLI, schema verification, error UX | 5 |
| 10 | Finalize | Archaeology removal, final verification, release v0.2.0 | 3 |

## Dependency Graph

```
Phase 01 (Surgery)
  └─> Phase 02 (Critical Fixes)
       ├─> Phase 03 (Security)
       └─> Phase 04 (Robustness)
            └─> Phase 05 (Real Tests)
                 ├─> Phase 06 (Migration-Safe Deploys)
                 └─> Phase 07 (Packaging)
                      └─> Phase 08 (Docs)
                           └─> Phase 09 (Confiture Integration & Polish)
                                └─> Phase 10 (Finalize)
```

Phases 03 and 04 can run in parallel after Phase 02.
Phases 06 and 07 can run in parallel after Phase 05.

## Current Status

- [ ] Phase 01: Not Started
- [ ] Phase 02: Not Started
- [ ] Phase 03: Not Started
- [ ] Phase 04: Not Started
- [ ] Phase 05: Not Started
- [ ] Phase 06: Not Started
- [ ] Phase 07: Not Started
- [ ] Phase 08: Not Started
- [ ] Phase 09: Not Started
- [ ] Phase 10: Not Started
