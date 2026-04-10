# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.12] - 2026-04-10

### Fixed

- **Issue #156: bootstrap restarts the webhook service after a fraisier self-upgrade** — `fraisier bootstrap` now runs a `systemctl restart fraisier-{project}-webhook.service` step immediately after upgrading fraisier, so the new binary takes effect without manual intervention. If the service is not yet running (fresh install), the step is a no-op.

---

## [0.7.11] - 2026-04-10

### Fixed

- **Issue #153: deploy daemon no longer errors on config sync when `/opt/fraisier/` is absent** — `_sync_fraises_yaml` now runs `mkdir -p` on the destination directory before `cp`, so the first deployment on a freshly bootstrapped server succeeds silently instead of logging a recurring "Config sync failed" warning.

---

## [0.7.5] - 2026-04-10

### Fixed

- **`fraisier logs` works correctly from background processes and scripts** — Replaced `os.execvp` with `subprocess.Popen` (inherited stdin/stdout/stderr). TTY behaviour is identical for interactive use (colours, terminal size, Ctrl-C), but the Python process now stays alive so background runners and scripts can track the PID properly.
- **`fraisier trigger-deploy --follow` was silently broken** — The `--follow` path imported `_resolve_deploy_unit_pattern` by its old name after the #154 refactor, causing a silent `ImportError` and never exec-ing into journalctl. Fixed by reusing the `socket_stem` already computed just above the call site.

---

## [0.7.4] - 2026-04-10

### Added

- **Issue #154: `fraisier logs` fetches logs from remote servers** — `fraisier logs <fraise> <env>` now detects whether the target environment is remote (has an `ssh:` block in its config) and SSHs to the target server to run `journalctl` there instead of querying the local machine. `os.execvp` is used so the TTY is inherited and `--follow` / Ctrl-C work correctly over SSH. A new `--service [app|deploy]` option (default: `deploy`) lets operators switch between the deploy-daemon template instances and the main app service. Unit pattern generation now uses the same naming logic as the scaffold (`deploy_socket_name` / `app_service_name`), so patterns always match installed units. `app_service_name()` is extracted to `fraisier.naming` as the single source of truth; the scaffold renderer delegates to it. The `ssh:` block is now validated by the config loader (`host` required, typed fields enforced).

---

## [0.7.3] - 2026-04-10

### Fixed

- **Issue #151: `fraisier sync` auto-resolves file deletions from the source branch** — When the source branch has deleted a file that still exists in the target branch, the sync now detects the deletion (via `git cat-file -e origin/{source}:{file}`) and accepts it with `git rm`, instead of halting with an unresolved conflict error. A log line is printed for each auto-resolved deletion. Conflicts where both sides modified the same file are still surfaced to the user.

---

## [0.7.2] - 2026-04-09

### Fixed

- **Issue #149: stale deploy socket/service files not cleaned up on re-scaffold** — `ScaffoldRenderer` now removes `fraisier-*.socket` and `fraisier-*@.service` files from the `systemd/` output directory that are no longer part of the current render set (e.g. after a fraise rename or the 0.7.1 socket naming change).
- **Issue #150: install.sh migrates pre-0.7.1 generic deploy socket units** — `install.sh` now detects and removes stale `fraisier-{env}.socket` / `fraisier-{env}@.service` / `fraisier-{env}.service` units left by older scaffold versions before installing the new fraise-specific units, preventing duplicate or conflicting unit names on the server.

---

## [0.7.1] - 2026-04-09

### Fixed

- **Issue: deploy socket name collision with multiple fraises** — `deploy_socket_name()` now includes the fraise name in the default unit name: `fraisier-{fraise_name}-{env_key}.socket`. Previously, multiple fraises sharing the same env key (e.g. `production`) generated the same socket unit name, causing last-write-wins overwrites. Resolution order: `systemd_deploy_socket` override → `fraisier-{env.name}.socket` (if `env.name` set) → `fraisier-{fraise}-{env}.socket` (new default) → `fraisier-{env}.socket` (legacy fallback).

---

## [0.7.0] - 2026-04-09

### Added

- **`fraisier sync` CLI command (#140)** — Replaces the `sync.sh` scaffold template. `fraisier sync [pair]` fetches the source branch, detects drift, creates a squash sync branch, auto-resolves fraisier-owned files on merge conflict, and opens a PR with auto-merge enabled. Supports `--list`, `--check` (drift detection only), `--dry-run`, and `--yes` flags.

### Fixed

- **Issue #148: nginx configs always generated for all servers** — `fraisier scaffold` (with or without `--server`) now generates per-environment nginx configs for every server, not just the local one. `scripts/generated/nginx/` is always a complete, committable artifact; each server's `install.sh` uses `_env_active()` to install only its own configs.
- **Issue #147: CORS wildcard regex anchored and dot-escaped** — Wildcard patterns (e.g. `https://*.example.com`) now produce correctly anchored nginx regexes: dots in the domain are escaped before `*` is expanded, and `^`/`$` anchors are added.
- **Issue #145: legacy rate_limit.conf removed before nginx reload** — `install.sh` now deletes `/etc/nginx/conf.d/rate_limit.conf` before reloading nginx when `gateway.conf` owns `limit_req_zone`, preventing duplicate-directive errors on servers upgraded from older fraisier versions.
- **Issue #144: install.sh SCAFFOLD_DIR always derived from script location** — The `SCAFFOLD_DIR` default no longer depends on the `STANDALONE` flag; it is always inferred from the script's own path, fixing silent no-ops when run outside the standard directory.

### Removed

- **`sync.sh.j2` scaffold template** — Replaced by the `fraisier sync` CLI command. Projects that referenced a generated `sync.sh` should switch to `fraisier sync`.

---

## [0.6.0] - 2026-04-08

### Breaking Changes

- **CORS origins configuration format changed** — `cors_origins` now requires structured objects. Plain strings are no longer accepted. Update `["https://*.example.com", "https://api.example.com"]` to `[{"pattern": "https://*.example.com", "type": "wildcard"}, {"pattern": "https://api.example.com", "type": "literal"}]`.

### Added

- **Structured CORS origins with explicit types** — `cors_origins` entries now support:
  - `"type": "literal"` — Escapes dots for nginx regex (e.g., `https://api.example.com` → `~https://api\.example\.com`)
  - `"type": "wildcard"` — Converts `*` to subdomain regex (e.g., `*.example.com` → `~https://[a-zA-Z0-9-]+\.example\.com`)
  - `"type": "regex"` — Raw nginx regex without processing (e.g., `^https://.*\.custom\.com$`)
- **Conditional WatchdogSec support** — `service.watchdog_sec` field enables `WatchdogSec` in systemd service units for servers that support `sd_notify` (e.g., gunicorn). Defaults to disabled for uvicorn compatibility.
- **Server type configuration** — `service.server_type` field (`"uvicorn"` or `"gunicorn"`) for future server-specific optimizations.

- **Socket-based install helper (#124)** — New `fraisier-install-helper` daemon (socket + service systemd units) runs as `install_user`, accepts install commands over a Unix socket owned by `deploy_user`. Eliminates the `sudo -u` dependency at deploy time. `fraisier-webhook` emits `FRAISIER_INSTALL_SOCKET_*` env vars; `install.sh` installs and enables the units automatically.
- **Scaffold sync promotion scripts (#139)** — New `sync.sh` scaffold template generates a git-based promotion script per `scaffold.sync` pair defined in `fraises.yaml`. The script fetches, detects drift, creates a sync branch, pre-merges the target, auto-resolves fraisier-owned files, and opens a PR via `gh`.
- **Stale service file warning at deploy time (#142)** — `fraisier deploy` now warns when the live systemd service file differs from what fraisier would generate, prompting a `fraisier scaffold` run before the next deploy.

### Fixed

- **Issue #138: nginx CORS map supports wildcard subdomain patterns** — Wildcard patterns like `https://*.example.com` now generate proper nginx regex patterns.
- **Issue #137: WatchdogSec properly omitted for non-sd_notify servers** — WatchdogSec is now conditionally included only when configured, preventing uvicorn services from being killed every 60 seconds.
- **Issue #127: gateway.conf spurious SSL catch-all block suppressed** — `server_name` is now aggregated across per-environment nginx blocks when not set at the fraise level, so `has_server_names` is correctly `True` and the `server_name _` SSL catch-all is omitted.

---

## [0.5.28] - 2026-04-07

### Added

- **`fraisier health --width` option** — Table now expands to terminal width by default (`os.get_terminal_size()`), eliminating column truncation. Pass `--width N` to override. Falls back to 120 when terminal size is unavailable (e.g. CI/piped output).

## [0.5.27] - 2026-04-07

### Added

- **Custom per-environment health endpoints with headers and field mappings (#136)** — `fraisier health` now uses the full `health_check.url` from each environment config as-is (no path stripping). Optional `health_check.headers` dict allows authenticated endpoints. Optional per-environment `version_field` and `migration_field` override the global defaults with dot-notation path support (e.g. `versions.app`). Falls back to global `health.version_field` / `health.migration_field` when not set per-environment.

### Fixed

- **pytest-asyncio missing from dev dependency group** — `pytest-asyncio` was declared in `[project.optional-dependencies]` but not in `[dependency-groups]`, causing async tests to fail silently after `uv sync`.

## [0.5.25] - 2026-04-07

### Fixed

- **fraisier health only shows last environment when multiple environments share the same fraise name (#130)** — The services dict used `fraise_name` as key, causing overwrites for each environment iteration. Changed to use composite key `f"{fraise_name}-{env_name}"` so all environments appear in the health table.

- **fraisier health URL column strips scheme for remote services (#131)** — The URL display logic incorrectly split on `:` and took the last part, dropping `https://` for remote URLs. Now displays the full base URL.

- **fraisier health table lacks Environment column (#132)** — The table merged fraise name and environment into a single Service column. Added a dedicated Environment column for clarity, with Service showing only the fraise name.

- **fraisier health does not display migration number** — Added support for parsing and displaying migration information from health endpoint responses. Added `include_migration` config option and Migration column in health table.

## [0.5.24] - 2026-04-07

### Added

- **`fraisier db restore` command (#129)** — Expose the staging restore workflow
  (pg_restore → rollback template → migrate → validate) as a user-facing command. The
  nightly `restore-staging-from-production-backup.service` systemd unit previously called
  a broken `trigger-deploy` path and did nothing useful; it now calls
  `fraisier db restore api staging` and works correctly. Supports `--from-backup` to restore
  from a specific dump file (useful for testing), `--dry-run` to preview the plan, and
  `--no-service-restart` to skip service management if needed.

- **Scaffold templates for staging restore** — Generated `restore-staging.service` and
  `restore-staging.timer` systemd units for nightly restores. Auto-discovers fraises with
  `restore_migrate` strategy and generates correct `fraisier db restore` commands.
  When running `fraisier scaffold-install --apply` on existing servers, the broken service
  unit is automatically replaced with the corrected one.
  Conditionally rendered only when `restore_migrate` is configured to avoid invalid
  systemd units in configs without staging restore.

- **CLI consistency** — All fraise database commands now use positional `ENVIRONMENT`
  arguments instead of flags (e.g., `fraisier db restore api staging` instead of
  `fraisier db restore api -e staging`), matching the pattern of `trigger-deploy`, `logs`,
  and other top-level commands.

### Fixed

- **Server-scoped scaffold collectors** — `pg_allowed_databases`, `allowed_services`, and
  `has_database` now use server-filtered fraises (`local_fraises`) instead of the unfiltered
  global list. Previously, rendering with `--server` included databases and services from
  all servers, causing cross-server leakage in generated pg-wrapper and systemctl-wrapper
  allowlists.

- **ReadWritePaths use configured `app_path`** — `restore-staging.service` and
  `poll-deploy.service` templates now derive `ReadWritePaths` from the actual `app_path`
  in environment config instead of hardcoded `/opt/<fraise_name>`. restore-staging further
  restricts paths to environments with `restore_migrate` strategy.

- **Socket-based systemctl for restore and poll-deploy** — `restore-staging.service` and
  `poll-deploy.service` now use `FRAISIER_SYSTEMCTL_SOCKET` (the socket-activated helper
  running as root) instead of the legacy `FRAISIER_SYSTEMCTL_WRAPPER` which lacked
  privilege escalation inside systemd's security namespace.

- **CWD-independent `db restore` migrations** — Relative `confiture_config` paths (default:
  `confiture.yaml`) are now resolved against `app_path` before being passed to confiture.
  Previously, migrations failed when the service's working directory was not the app
  directory, because confiture could not find `db/environments/<env>.yaml` relative to CWD.

- **Deduplicate ReadWritePaths in service templates** — `poll-deploy.service` and
  `restore-staging.service` now emit each `app_path` once even when multiple fraises
  share the same path on a server.

- **Deduplicate sudoers pg-wrapper rules** — The sudoers fragment now emits each
  `(user, pg-wrapper)` rule once regardless of how many environments use admin
  strategies. Also scoped to `local_fraises` to avoid cross-server leakage.

---

## [0.5.17] - 2026-04-06

### Fixed

- **Multi-server `install.sh` silent overwrites (#125)** — Running `fraisier scaffold --server A`
  then `--server B` silently overwrote `install.sh` with B's config, corrupting deployments on
  server A. The problem occurred because `install.sh` was rendered per-server at scaffold time,
  and successive runs overwrote the same output file. Fixed by adding runtime server detection:
  `install.sh` now detects the current machine's hostname at startup and conditionally installs
  only the units relevant to that machine. Requires new `servers:` section in `fraises.yaml`
  mapping logical server hostnames to machine hostnames (e.g., `prod.example.com` → 
  `[backend-prod-01, backend-prod-02]`). Single `install.sh` now works across the entire cluster
  with zero ambiguity.

---

## [0.5.16] - 2026-04-06

### Fixed

- **`poll-deploy.service.j2` deploys wrong fraise on wrong environment** — The service
  hardcoded `{{ fraise_names | first }} production`, so only the first fraise was polled and
  the environment was always `production` regardless of which server the unit ran on. The
  service now loops over `local_fraises` (server-filtered) and generates one `ExecStart`
  line per (fraise, environment) pair. The binary path was also wrong (`/usr/local/bin/fraisier`
  instead of the deploy user's uv-tool path) and is now consistent with all other templates.

- **`ScaffoldConfig` missing `socket_user`/`socket_group` fields** — The deploy-socket
  template referenced `scaffold.socket_user` and `scaffold.socket_group` via Jinja
  `|default('www-data')` filters, making them silently unconfigurable. Both fields are now
  declared in `ScaffoldConfig` with `"www-data"` as the default.

- **Bootstrap uploads scaffold without checking for placeholder files** — When a template
  file is missing from the package, `ScaffoldRenderer` writes a `# Placeholder:` stub. The
  bootstrap step now inspects each rendered file and fails early with a clear error rather
  than uploading broken stubs that produce confusing failures on the remote server.

- **Config module refactor lint cleanup** — The `config/` package re-export module had
  stale unused imports (`_config`, `_config_lock` without `noqa`), an unsorted `__all__`,
  and test files had `noqa` directives that were no longer needed after adding `F401` to the
  test ignore list. All cleaned up.

---

## [0.5.15] - 2026-04-06

### Fixed

- **Watchdog kills uvicorn-based services every 60 seconds** — `service.j2` unconditionally
  added `WatchdogSec=60` but uvicorn does not implement `sd_notify` keepalive pings, so
  systemd would kill every instance exactly 60 seconds after startup. Removed `WatchdogSec`
  from the template (the fraisier-webhook template already omitted it with an explanatory
  comment for the same reason).

---

## [0.5.14] - 2026-04-06

### Fixed

- **Bootstrap installs stale fraisier version due to uv index metadata cache** — `uv tool install
  --force` reinstalls but uses cached PyPI index metadata, so newly published versions are invisible
  until the cache expires. Added `--refresh-package fraisier` to the bootstrap install command so
  the index is always refreshed when bootstrapping, ensuring the server receives the exact client
  version.

---

## [0.5.13] - 2026-04-06

### Fixed

- **`uv tool install --force` fails with "Permission denied" on reinstall** — the systemctl
  helper service runs as root and causes Python to create root-owned `__pycache__/`
  directories inside the deploy user's fraisier tool installation. When uv later tries to
  force-reinstall fraisier it cannot remove those root-owned directories. Added
  `Environment=PYTHONDONTWRITEBYTECODE=1` to the helper service so Python never writes
  bytecode cache files, keeping the tool directory entirely owned by the deploy user.

---

## [0.5.12] - 2026-04-06

### Fixed

- **`/run/fraisier/` still root-owned: `systemctl-helper.socket` had `RuntimeDirectory=fraisier`** —
  the socket unit (which runs as root via `SocketUser=root`) also declared `RuntimeDirectory=fraisier`
  with `RuntimeDirectoryMode=0755`. On every socket start/restart, systemd recreated `/run/fraisier/`
  owned by root, overwriting the `printoptim_deploy`-owned directory managed by the webhook service.
  Removed `RuntimeDirectory` and `RuntimeDirectoryMode` from the socket unit; `/run/fraisier/` is
  now managed exclusively by the webhook service.

---

## [0.5.11] - 2026-04-06

### Fixed

- **`/run/fraisier/` still wiped during v0.5.7 → v0.5.10 upgrade** — the previous fix
  stopped the helper service *before* daemon-reload, which meant systemd used the OLD
  unit (which had `RuntimeDirectory=fraisier`) for the stop phase and still wiped the
  directory. The correct approach is to copy the new unit files, run `daemon-reload`
  first (so systemd loads the new unit without `RuntimeDirectory`), and *then* restart
  the service — the stop phase now uses the new unit and `/run/fraisier/` is preserved.

---

## [0.5.10] - 2026-04-06

### Fixed

- **`/run/fraisier/` wiped during upgrade from v0.5.7 → v0.5.9** — `install.sh` now
  explicitly stops the old helper service and socket *before* copying the new unit files
  and running `daemon-reload`. This prevents the old helper service (which had
  `RuntimeDirectory=fraisier`) from wiping `/run/fraisier/` at daemon-reload time. After
  the new files are in place, deploy sockets are restarted to ensure their socket files
  exist, then the helper socket is started.

---

## [0.5.9] - 2026-04-06

### Fixed

- **Every deployment wiped `/run/fraisier/` and destroyed all socket files** — the
  `systemctl-helper.service` template included `RuntimeDirectory=fraisier` (and
  `RuntimeDirectoryMode=0755`). Since the helper runs as root, systemd recreated
  `/run/fraisier/` as a root-owned directory on every activation, wiping the deploy
  socket files and the helper socket file itself. Removed `RuntimeDirectory` from the
  helper service; `/run/fraisier/` is now managed exclusively by the webhook service
  (which uses `RuntimeDirectoryPreserve=restart`).

---

## [0.5.8] - 2026-04-06

### Fixed

- **systemctl helper socket file missing after webhook restart** — `install.sh` installed the
  helper service and socket unit files but never restarted the socket, so the socket file in
  `/run/fraisier/` was never (re)created. Added `systemctl restart fraisier-{project}-systemctl-helper.socket`
  after the install step, mirroring the pattern used for deploy socket units.

---

## [0.5.7] - 2026-04-06

### Fixed

- **Duplicate `ReadWritePaths` in generated deploy service units** — the `deploy-service.j2`
  template had hardcoded `ReadWritePaths=/run/fraisier` and `ReadWritePaths={{ scaffold.config_dir }}`
  entries below the manifest loop. After the v0.5.6 fix those same paths are now emitted by the
  manifest loop, producing duplicates. The hardcoded lines have been removed; the manifest is now
  the sole source of all `ReadWritePaths` entries.

---

## [0.5.6] - 2026-04-06

### Fixed

- **Deploy service missing `ReadWritePaths` for `/opt/fraisier`, `/var/lib/fraisier`, `/run/fraisier`** —
  `build_manifest()` was collecting deploy socket stems *after* building the global paths, so the deploy
  service unit stems were absent from `read_write_units` on the three shared fraisier directories.
  Stems are now collected first and included in `shared_rw_units` used by all global paths.
- **`GIT_SSH_COMMAND` env assignment ignored by systemd** — the unquoted value
  `Environment=GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new` caused systemd to log
  `Invalid environment assignment, ignoring: -o` (spaces split the value into separate tokens).
  Fixed to `Environment="GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new"`.

---

## [0.5.5] - 2026-04-06

### Fixed

- **Deploy socket files not recreated after first-time webhook install** — `install.sh` now
  restarts all local deploy socket units after the webhook service restart, ensuring their
  socket files are always present in `/run/fraisier/` for `validate-setup` to find.

---

## [0.5.4] - 2026-04-06

### Fixed

- **Deploy socket files wiped on webhook restart** — the webhook service's `RuntimeDirectory=fraisier`
  caused systemd to remove and recreate `/run/fraisier/` on service restart, destroying socket
  files created by co-located deploy socket units. Added `RuntimeDirectoryPreserve=restart` so
  `/run/fraisier/` and its contents survive webhook restarts (still cleaned on full stop/reboot).

---

## [0.5.3] - 2026-04-06

### Fixed

- **Webhook service unit not installed by `install.sh`** — `install.sh` installed the
  systemctl helper, sudoers, app units, and deploy socket units, but never the
  `fraisier-{project}-webhook.service` file itself. Upgrades to webhook configuration
  (e.g. `FRAISIER_SYSTEMCTL_SOCKET`, `NoNewPrivileges`, `ReadWritePaths`) were silently
  not applied on re-bootstrap. Now copies the webhook service file and restarts it.
- **`fraisier-{project}-webhook.service` added to `get_install_mapping()`** so
  `scaffold-diff` correctly detects divergence between generated and installed webhook units.

---

## [0.5.2] - 2026-04-06

### Fixed

- **sudoers install rule not generated when `install.user` is defined at fraise level** —
  `_collect_deduplicated_sudoers_rules` only checked `env_config` for the `install` key,
  missing the common pattern where `install` is declared once at the fraise level and
  inherited by all environments. Now falls back to fraise-level install config.

---

## [0.5.1] - 2026-04-06

### Breaking Changes

- Scaffold output changed: regenerate and reinstall all systemd service units after upgrading.
  Generated webhook and deploy-daemon services now use `FRAISIER_SYSTEMCTL_SOCKET` instead
  of `FRAISIER_SYSTEMCTL_WRAPPER`. The generated sudoers fragment no longer contains
  a systemctl-wrapper NOPASSWD entry.

### Added

- **`fraisier-systemctl-helper`** — root-privileged Unix socket helper for service management.
  Replaces the sudo/wrapper mechanism entirely. Runs as root under systemd socket activation,
  validates commands against an allowlist baked into `ExecStart` args, calls `systemctl`
  directly. `NoNewPrivileges=true` on all services including the helper itself.
- New scaffold templates: `systemctl-helper.service.j2`, `systemctl-helper.socket.j2`.
  Generated as `fraisier-{project}-systemctl-helper.{service,socket}`, installed to
  `/etc/systemd/system/` by `install.sh`, socket enabled on first install.
- `FRAISIER_SYSTEMCTL_SOCKET` environment variable in webhook and deploy-daemon service units —
  points to `/run/fraisier/systemctl-{project}.sock`.
- `resolver.py`: new `systemctl_socket` property.

### Fixed

- **#123: `NoNewPrivileges=true` blocked sudo in webhook service** — sudo is no longer used
  for service management; all services keep `NoNewPrivileges=true`.
- **#122: wrapper called without sudo** — the wrapper mechanism is superseded; the new helper
  requires no sudo at all.

### Removed

- systemctl NOPASSWD block from generated `sudoers` fragment (no longer needed).

---

## [0.5.0] - 2026-04-06

### Breaking Changes

- Scaffold output changed: regenerate and reinstall `install.sh` and all systemd service
  units after upgrading. Generated files are now derived from the path ownership manifest
  rather than per-concern template logic.

### Added

- **PathManifest** — declarative, typed registry of every filesystem path fraisier manages,
  with ownership, permissions, and systemd unit bindings. Replaces scattered path logic.
- **ConfigResolver** — single source of truth for environment variable overrides;
  replaces scattered `os.getenv` calls across the codebase.
- `validate-remote` path checks are now manifest-driven; new managed paths get validation
  and repair automatically without handwritten checkers.

### Fixed

- **#121: `.venv` owned by the wrong user** — now deleted and recreated by the install step
  instead of failing with a non-root `chown` error.
- **ReadWritePaths in generated service units** — now complete and derived from the manifest;
  no more per-bug additions.

### Removed

- Runtime `chown` call in `_install_dependencies` (replaced by manifest-driven delete-and-recreate).
- Hardcoded path-ownership checks in `remote_validator.py` (replaced by manifest loop).
- Scattered `os.getenv` calls outside `resolver.py` and `_env.py`.

---

## [0.4.18] - 2026-04-05

### Fixed
- **Cryptic permission error when `deploy-daemon` runs as wrong user** (#67) — instead of a
  bare `PermissionError` on the lock file, fraisier now checks the current user against the
  configured `deploy_user` before acquiring the lock and emits a clear error with a suggested
  `sudo -u <deploy_user>` command.

---

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
