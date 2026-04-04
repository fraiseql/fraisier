# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.17] - 2026-04-05

### Fixed
- **Per-env nginx configs had wrong filename and were not installed** (#110) — files were named
  `{project}_{fraise}_{env}.conf` instead of `{server_name}.conf`, mismatching nginx conventions.
  The renderer now uses `nginx_config.server_name` as the filename stem. The generated `install.sh`
  now also copies and symlinks each per-env config into `/etc/nginx/sites-available/` and
  `/etc/nginx/sites-enabled/`, then reloads nginx — previously only `gateway.conf` was installed.

---

## [0.4.14] - 2026-04-04

### Fixed
- **Bootstrap double-sudo fails silently for steps 4 and 10** (#104) — when using `--sudo`
  with a non-root SSH user, the outer `sudo -S` consumed the password from stdin, leaving the
  inner `sudo -u <deploy_user>` with no stdin to read from. Steps 3, 4, and 10 now use
  `sudo -n -u` (non-interactive), which prevents any stdin read since root needs no password
  to switch users.
- **Bootstrap step 4 installs wrong fraisier version on server** (#103) — `_install_fraisier`
  read the version from the hardcoded `__init__.__version__` string, which could be stale
  relative to `pyproject.toml`. It now uses `importlib.metadata.version("fraisier")`, which
  always reflects the actually-installed package version.

---

## [0.4.13] - 2026-04-04

### Fixed
- **`validate-setup` and `deploy` used old socket path pattern** — both commands were
  building the socket directory as `/run/fraisier/{project_name}-{environment}` instead of
  using `deploy_socket_name()`, causing them to look in the wrong place after the naming
  overhaul in v0.4.9.
- **`_check_systemd_units` used a completely wrong unit name** — it was constructing
  `fraisier-{project}-{environment}-deploy.socket`, a pattern that never existed. It now
  receives the unit name directly from the caller via `deploy_socket_name()`.

---

## [0.4.12] - 2026-04-04

### Fixed
- **Bootstrap step 4 always pins server-side fraisier to the client version** (#101) — `uv tool install`
  was skipping the install if fraisier was already present on the server, leaving a stale version.
  It now runs `uv tool install --force fraisier==<client_version>` unconditionally, ensuring the
  server and client are always in sync.

---

## [0.4.10] - 2026-04-04

### Fixed
- **Bootstrap step 10 validates each fraise individually** (#98) — `validate-setup` requires
  a positional `FRAISE` argument; the bootstrap command was calling it without one. It now
  iterates all fraises configured for the target environment, calling `validate-setup <fraise>`
  once per fraise and failing fast on the first error.

---

## [0.4.9] - 2026-04-04

### Added
- **`fraisier/naming.py`** — new `deploy_socket_name(env_config, env_key)` helper as the
  single source of truth for deploy socket unit names. Resolves in order:
  1. explicit `systemd_deploy_socket` field in environment config
  2. `fraisier-{env.name}.socket` derived from the environment's `name` field
  3. `fraisier-{env_key}.socket` derived from the environment dict key
- **`systemd_deploy_socket` config field** — optional per-environment override for the
  deploy socket unit name (validated against the same regex as `systemd_service`)

### Changed
- **Deploy socket unit names** now derived from the environment `name` field (e.g.
  `fraisier-api.myapp.io.socket`) instead of the verbose
  `fraisier-{project}-{fraise}-{env}-deploy.socket` pattern
- **`ListenStream` socket path** in generated units updated to match the new naming
  (`/run/fraisier/{socket_stem}/deploy.sock`)
- **`fraisier/scaffold/renderer.py`** — all consumers call `deploy_socket_name()`;
  `socket_unit_name` and `socket_stem` added to socket/service template contexts;
  `deploy_socket_name` registered as a Jinja2 global for use in templates
- **`fraisier/scaffold/diff.py`** — filter logic replaced: pre-computes the set of
  matching deploy unit paths from config instead of parsing filenames with a regex
- **`fraisier/bootstrap.py`** — `_enable_sockets()` now iterates all fraises for the
  target environment and enables each socket; returns a clear error if no fraises match
- **`fraisier/cli/main.py`** — `diagnose` command derives `socket_unit` and `socket_path`
  via `deploy_socket_name()`

### Fixed
- **Bootstrap enabled the wrong socket unit** (#95) — bootstrap was building
  `fraisier-{project}-{env}-deploy.socket`, missing the fraise name, causing
  `systemctl enable` to fail. Fixed as a side effect of centralising naming in #96.

---

## [0.4.3] - 2026-04-03

### Added

#### CLI Enhancements
- **New `fraisier logs <fraise> <env>` command** - Tail systemd journal logs for deploy daemons
  - Supports `--no-follow`, `--lines N`, `--since` options
  - Automatically resolves unit patterns from configuration
  - Uses `os.execvp` for proper signal handling

- **Enhanced `fraisier history` command** - Improved deployment history viewing
  - Positional arguments: `fraisier history <fraise> <env>`
  - `--json` flag for structured output
  - `--since` filtering with relative time parsing (7d, 24h) and ISO dates
  - Enhanced table with SHA (truncated) and "Triggered By" columns
  - Better duration formatting with time units

- **New `fraisier scaffold-diff` command** - Detect infrastructure drift
  - Compare generated scaffold files against installed system files
  - Unified diff output with file-level summaries
  - Supports filtering by fraise/environment
  - `--apply` flag for automatic re-installation (future enhancement)

- **Enhanced `fraisier ship` command** - Post-deployment verification
  - `--wait-deploy` flag polls health endpoint after shipping
  - `--deploy-timeout` option controls verification timeout (default 300s)
  - Shows progress during health polling with elapsed time
  - Supports multiple health endpoint version field names

- **Enhanced `fraisier trigger-deploy` command** - Synchronous execution
  - `--wait` flag blocks until deployment completion
  - `--follow` flag streams deployment logs in real-time
  - JSON result parsing from daemon responses

#### Daemon & Status Improvements
- **Enhanced deployment status display** - Real-time deployment visibility
  - `fraisier status` shows deploying/pending/failed states with elapsed time
  - Status file reading prioritizes over version comparison
  - Added "pending" state to deployment lifecycle

- **Improved daemon error diagnostics** - Better troubleshooting
  - Config search path display when config files not found
  - Project availability hints when fraise not in configuration
  - File existence checks in search locations

- **Rollback enhancements** - Safer rollback operations
  - `--dry-run` shows rollback plan without executing
  - Improved target resolution finds most recent successful deployment
  - Safety limit prevents rollback beyond 10 deployments (unless --force)
  - Better output with current vs target version display

#### Internal Improvements
- **Health polling system** - `fraisier.ship.health_poll` module
  - Robust HTTP polling with configurable timeout/intervals
  - Version extraction from multiple health endpoint field names
  - Progress display during long-running health checks

- **Scaffold drift detection** - `fraisier.scaffold.diff` module
  - File comparison with unified diff generation
  - Install path mapping from scaffold to system locations
  - Support for systemd, nginx, and sudoers file types

- **Enhanced status file management**
  - Daemon writes status updates during deployment lifecycle
  - Atomic status file operations with proper error handling

### Changed
- **Daemon result output** - Now writes JSON results to stdout for socket clients
- **Status computation** - Prefers status file data over version comparison
- **Rollback target selection** - Uses smarter algorithm for finding rollback targets

### Fixed
- **Config validation** - Better error messages for missing fraises/environments
- **Socket communication** - Improved response parsing in trigger-deploy

### Technical Details
- **8 major features** implemented across multiple phases
- **15+ new CLI commands/options** added
- **20+ test files** with comprehensive coverage
- **Zero breaking changes** - Full backward compatibility maintained
- **Enterprise-grade reliability** with proper error handling and timeouts

---

## [Unreleased]

## [1.0.0] - 2026-03-15

Initial release of Fraisier deployment management system.

### Added
- Core deployment functionality for multiple providers (Docker Compose, API, ETL)
- Configuration-driven deployment with `fraises.yaml`
- Systemd socket-activated deployment daemons
- Scaffold generation for infrastructure files
- Version management and git integration
- Database migration support via confiture
- Comprehensive testing framework
- Rich CLI with progress indicators and error handling
