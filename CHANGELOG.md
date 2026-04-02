# Changelog

## [0.4.2] - 2026-04-03

### Added

- **`fraisier bootstrap` command** — provision a virgin server end-to-end via SSH.
  Connects as root (or `--ssh-user`) and runs 10 ordered, idempotent steps:
  create deploy user, add to `www-data`, install `uv` and `fraisier` for the
  deploy user, create directories, upload `fraises.yaml`, upload scaffold files,
  run `install.sh --standalone`, enable the deploy socket, and validate setup.
  Supports `--dry-run`, `--yes`, `--verbose`, `--server` override, and
  `--ssh-key`.

- **`SSHRunner.upload()`** — upload a single file to a remote host via `scp`,
  reusing the shared SSH connection options.

- **`SSHRunner.upload_tree()`** — upload a directory tree to a remote host by
  piping a `tar` archive over SSH (no `rsync` dependency required).

- **`install.sh --standalone` mode** — the generated install script now accepts
  `--standalone` and `--scaffold-dir <path>`, allowing it to run from a
  temporary upload directory without requiring the project to be cloned at
  `PROJECT_DIR` first.

### Changed

- `SSHRunner._build_ssh_prefix()` now delegates shared option building to a new
  `_build_ssh_options()` helper, so SSH and SCP commands stay in sync.

### Fixed

- **Scaffold socket/service units** — resolved four bugs in the generated
  socket-activated deploy daemon units (closes #72):
  - Service unit renamed to `fraisier-{project}-{fraise}-{env}-deploy@.service`
    (template unit required by `Accept=yes`; systemd spawns one instance per
    connection). Fraise name is now included in the filename to avoid collisions
    when multiple fraises share an environment name.
  - `ProtectHome` removed from the deploy service unit; fraisier is installed as
    a `uv` tool entirely within `~/.local`, which `ProtectHome` (any value) makes
    inaccessible.
  - `Environment=FRAISIER_CONFIG` added to the deploy service unit so the daemon
    can locate `fraises.yaml` in a clean systemd environment. The path defaults to
    `/opt/fraisier/fraises.yaml` and is configurable via `scaffold.config_path`.
  - `StandardOutputFormat=json` removed from the deploy service unit; the key is
    unavailable on systemd 252–253 (Debian 12 / Ubuntu 22.04).

- **`scaffold.systemd_service` per-environment override** — set
  `systemd_service: api.printoptim.dev.service` on any environment to use a
  custom unit filename instead of the generated `{project}_{fraise}_{env}`
  pattern. The override propagates to `install.sh` and `systemctl-wrapper.sh`.
  Validated at config load time. (`service.service_name` is also supported as a
  nested alternative under the `service:` key.)
