# Changelog

## [0.4.2] - 2026-04-02

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
