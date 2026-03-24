# Fraisier Hardening: Post-Audit Remediation

Created 2026-03-24, based on comprehensive five-area audit.

## Baseline Scores

| Area              | Score | Key Issue                                                        |
|-------------------|-------|------------------------------------------------------------------|
| Code Quality      | 7     | Two near-god-objects, duplicated patterns, 117-line rollback     |
| Test Quality      | 6     | Mock-heavy critical paths, shared DB state, under-tested rollback|
| Production Rdns   | 7     | Local-only locking, SSH drop vulnerability, non-atomic rollback  |
| Security          | 9     | Excellent — minor env parsing gap                                |
| Feature Complete  | 9     | 33 commands shipped, all documented features implemented         |
| Documentation     | 9     | Accurate, comprehensive, honest about limitations                |
| Competitive Pos   | 8     | Clear niche, genuine differentiator                              |

## Target

Raise Code Quality, Test Quality, and Production Readiness to 9+.
Security, Features, Docs, and Competitive Position are already strong — maintain them.

## Phases

| # | Title                       | Focus                                                      | Depends On |
|---|-----------------------------|------------------------------------------------------------|------------|
| 1 | Refactoring & Code Quality  | God objects, long functions, error propagation              | —          |
| 2 | Production Safety           | Distributed lock, SSH timeout, atomic rollback, config gaps | —          |
| 3 | Security Tightening         | Env var parsing, health check validation                   | —          |
| 4 | Test Hardening              | Real rollback tests, DB isolation, mock reduction           | 1, 2       |
| 5 | Finalize                    | Archaeology removal, lint clean, verification               | 1–4        |

Phases 1, 2, and 3 can run in parallel (independent concerns).
Phase 4 depends on 1 and 2 (tests verify refactored code and new safety features).
Phase 5 depends on all prior phases.

## Design Principles

- Fraisier is the FraiseQL ecosystem's deployment tool for confiture migrations on PostgreSQL.
- Every change must have a regression test.
- No speculative features — only fix what the audit identified.
- Minimal blast radius per phase — each phase is independently shippable.
