# ADR-001: Declarative Path Ownership Model

**Date:** 2026-04-06
**Status:** Accepted
**Version:** v0.5.0

---

## Context

Between v0.4.19 and v0.4.28, five separate bugs were filed and fixed that all had the
same root cause: no single place in the codebase defines who owns what path, with what
permissions, and which systemd units need write access to it.

The fixes landed in isolation:

| Issue | Path affected | Fix |
|-------|--------------|-----|
| #117  | `.venv` | Added `chown -R install_user .venv` at runtime (non-root — always fails) |
| #112  | bare git repo | Added `chown` loop to `install.sh.j2` |
| bootstrap fix | `/opt/fraisier`, `/var/lib/fraisier` | Added `mkdir`/`chown` loop to `install.sh.j2` |
| #116 / 99fce56 | git repo + app path | Added `ReadWritePaths` to `deploy-service.j2` |
| a0cd107 | `fraisier.db` | Added `FRAISIER_DB_PATH` env var, added `ReadWritePaths=/opt/fraisier` |

Each fix is correct in isolation. Together they reveal a systemic gap: ownership
semantics are scattered across `install.sh.j2`, `deploy-service.j2`,
`fraisier-webhook.service.j2`, `mixins.py`, and `database.py`. Adding a new path
requires updating all of these independently, and the failure mode is always a
production outage discovered after deployment.

A secondary pattern: configuration resolution has accumulated escape hatches.
`FRAISIER_CONFIG` overrides the config file path. `FRAISIER_DB_PATH` overrides the
database path. Both were added as reactive fixes. The resolution order (env var →
YAML field → hardcoded default) is implicit and inconsistently applied.

Issue #121 (the `chown` in `_install_dependencies` failing for non-root users) is the
immediate trigger for this ADR, but it is a symptom of the same gap.

---

## Decision

### 1. Declarative path ownership manifest

Introduce a `PathManifest` — a typed, validated data structure that is the single
source of truth for every filesystem path that fraisier manages. It lives in
`fraisier/manifest.py` and is populated from `fraisier.yaml` config at load time.

Each entry declares:

```python
@dataclass
class ManagedPath:
    path: Path
    owner: str           # username
    group: str           # group name
    mode: int            # octal (e.g. 0o750)
    read_write_units: list[str]   # systemd units that need ReadWritePaths
    create_if_missing: bool = True
```

The manifest is the authoritative answer to:
- What `ReadWritePaths` each service unit needs (no more per-fix additions)
- What `chown`/`chmod` commands `install.sh` emits (no more per-path loops)
- What `validate-remote` checks (derived, not hardcoded)
- What `repair-remote` fixes (derived from the same manifest entry)

### 2. Fix #121 tactically before the refactor

Remove the `chown` call from `_install_dependencies` entirely. Replace with:
delete `.venv` if it is owned by a user other than `install_user`, then let
`uv sync` recreate it. This works because `deploy_user` controls `app_path`
and can remove its contents regardless of subdirectory ownership.

This fix ships in a patch release before the v0.5.0 work begins.

### 3. Unified configuration resolution

Introduce a `ConfigResolver` that centralises all environment variable overrides.
All `os.getenv()` calls outside `ConfigResolver` are removed. The resolution order
is documented and tested. Configuration consumers receive resolved values, never
raw env vars.

### 4. Validate and repair from the manifest

`validate-remote` and `repair-remote` derive their checks and fixes from the
`PathManifest`. The current hardcoded check/fix pairs are replaced by a single
loop over manifest entries. New managed paths get validation and repair for free.

---

## Consequences

### Positive

- Any new managed path requires one declaration in the manifest; systemd units,
  install scripts, validator, and repairer all update automatically.
- The class of "permission bug discovered in production" becomes structurally
  impossible for declared paths.
- `validate-remote` becomes a complete pre-flight check, not an approximation.
- `repair-remote` becomes a complete remediation tool, not a patch.
- Configuration resolution is auditable: one file, one place, one test.

### Negative

- v0.5.0 is a significant internal refactor. No user-visible YAML schema changes
  are planned, but the scaffold output will change (generated `install.sh` and
  service units will differ). Users regenerating scaffold must reinstall
  generated files.
- `PathManifest` must be kept in sync with actual runtime behaviour. If a new
  code path writes to an undeclared path, the system reverts to the old failure mode.
  The validator should eventually check for this.

### Neutral

- `mixins.py:_install_dependencies` loses its ownership-management responsibility.
  It becomes purely: run the install command as the right user.
- The `FRAISIER_CONFIG` and `FRAISIER_DB_PATH` env vars are preserved for
  backwards compatibility but are now managed by `ConfigResolver`, not inline `getenv`.

---

## Alternatives considered

### sudo chown (the approach proposed in #121)

Adding `sudo /bin/chown` with a matching sudoers wildcard was rejected because:
- It adds root-level access for what is essentially a one-time ownership migration.
- The sudoers glob is a broader attack surface than needed.
- It treats the symptom (wrong owner on `.venv`) not the cause (no manifest).

### Per-call ownership checks in each deployer

Rejected. This is exactly the pattern that produced five isolated fixes.

### External provisioning tool (Ansible, etc.)

Out of scope. Fraisier's design goal is to be self-contained. The manifest approach
achieves the same correctness guarantee without introducing an external dependency.
