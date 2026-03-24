# Phase 7: Documentation & v0.1.0

## Objective
Align docs with reality, add missing guides, bump to v0.1.0.

## Success Criteria
- [ ] Webhook setup in README quickstart
- [ ] Notification system documented (new `docs/notifications.md`)
- [ ] `architecture.md` TODO markers replaced with shipped/not-shipped labels
- [ ] Strategy name inconsistency resolved (standardize on `migrate`)
- [ ] `deployment-guide.md` updated with notification config examples
- [ ] CLI reference updated for new flags (lock_timeout, --skip-* on validate)
- [ ] Version bumped to 0.1.0 in pyproject.toml
- [ ] Changelog written

## Cycles

### Cycle 1: Fix existing doc inaccuracies
- Fix strategy name inconsistency in deployment-guide.md
- Label unshipped features in architecture.md as `[PLANNED]`
- Add webhook setup to README quickstart

### Cycle 2: Notification docs
- Write `docs/notifications.md` with config reference and examples
- Add notification section to deployment-guide.md
- Document issue dedup and auto-close behavior

### Cycle 3: CLI reference updates
- Document `lock_timeout` config field
- Document `--skip-ssh`, `--skip-db` flags on validate
- Document notification-related config

### Cycle 4: Version bump + changelog
- Bump pyproject.toml to 0.1.0
- Update Development Status classifier to `3 - Alpha`
- Write CHANGELOG.md for v0.1.0

## Dependencies
- Phases 1-6 (documents features built in prior phases)

## Status
[ ] Not Started
