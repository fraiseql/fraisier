# Fraisier CLI Reference

Complete reference for all `fraisier` commands.

```bash
fraisier [GLOBAL_OPTIONS] COMMAND [COMMAND_OPTIONS]
```

## Global Options

| Option | Description |
|--------|-------------|
| `-c`, `--config PATH` | Path to `fraises.yaml` configuration file |
| `--verbose`, `-v` | Enable debug logging |
| `--help` | Show help and exit |

---

## Core Commands

### fraisier init

Initialise a new `fraises.yaml` in the current directory from a template.

```bash
fraisier init [--output DIR] [--template TEMPLATE] [--force]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--output DIR` | Output directory (default: current directory) |
| `--template TEMPLATE` | Starter template: `generic`, `django`, `rails`, `node` (default: `generic`) |
| `--force` | Overwrite existing `fraises.yaml` |

**Examples:**

```bash
fraisier init
fraisier init --template django
fraisier init --output config/ --template node
```

---

### fraisier list

List all registered fraises and their environments.

```bash
fraisier list [--flat]
```

**Options:**

- `--flat` -- Show a flat table instead of the default tree view.

**Examples:**

```bash
# Tree view (default)
fraisier list

# Flat table view
fraisier list --flat
```

---

### fraisier deploy

**REMOVED** -- Deploy a fraise to an environment.

> **Note:** This command has been removed. Use `fraisier trigger-deploy` for all deployments.

```bash
fraisier deploy FRAISE ENVIRONMENT [OPTIONS]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise to deploy.
- `ENVIRONMENT` (required) -- Target environment.

**Options:**

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would happen without deploying |
| `--force` | Deploy even if current and latest versions match |
| `--if-changed` | Deploy only if the remote has new commits |
| `--skip-health` | Skip the post-deploy health check |
| `--no-rollback` | Disable automatic rollback on health check failure |
| `--job NAME` | Specify a job name (for scheduled fraises) |

**Automatic Configuration Synchronization**

When you run `fraisier deploy`, Fraisier automatically:
- Syncs `fraises.yaml` from the git worktree to the path in `FRAISIER_CONFIG` (set by the
  deploy service unit, defaults to `/opt/fraisier/fraises.yaml`)
- Detects if configuration changed using SHA-256 hash comparison
- Regenerates and installs scaffold files if needed

This keeps the server in sync with your git repository automatically. See [deployment-guide.md](./deployment-guide.md#first-deployment) for details.

**Examples:**

```bash
# Standard deploy
fraisier deploy my_api production

# Preview what would happen
fraisier deploy my_api production --dry-run

# Force redeploy even if versions match
fraisier deploy my_api production --force

# Deploy without health check
fraisier deploy my_api staging --skip-health

# Deploy a specific job within a scheduled fraise
fraisier deploy my_etl production --job nightly_sync

# Deploy only if there are new commits
fraisier deploy my_api production --if-changed

# Deploy an irreversible migration (no auto-rollback on failure)
fraisier deploy my_api production --no-rollback
```

---

### fraisier trigger-deploy

Trigger deployment by writing to systemd socket.

Connects to the deployment socket for the specified fraise and environment, sends a JSON deployment request, and waits for completion.

```bash
fraisier trigger-deploy FRAISE ENVIRONMENT [OPTIONS]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise to deploy.
- `ENVIRONMENT` (required) -- Target environment.

**Options:**

| Option | Description |
|--------|-------------|
| `--branch BRANCH` | Git branch to deploy (defaults to configured branch) |
| `--force` | Force deployment even if up to date |
| `--no-cache` | Skip deployment caches |
| `--timeout SEC` | Timeout in seconds (default: 300) |

**Examples:**

```bash
# Standard deployment
fraisier trigger-deploy my_api production

# Deploy specific branch
fraisier trigger-deploy my_api development --branch feature-x

# Force redeploy
fraisier trigger-deploy my_api staging --force

# Long-running deployment
fraisier trigger-deploy my_etl production --timeout 3600
```

---

### fraisier deployment-status

Show the last deployment status for a fraise.

Reads the deployment status from the socket-activated daemon's status file and displays current deployment information.

```bash
fraisier deployment-status FRAISE [OPTIONS]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise.

**Options:**

| Option | Description |
|--------|-------------|
| `--json` | Output in JSON format |

**Examples:**

```bash
# Human-readable status
fraisier deployment-status my_api

# JSON output for scripting
fraisier deployment-status my_api --json
```

**Sample Output:**

```
Project: my_api
Environment: production
Status: success ✓
Deployed: abc1234 (2026-04-02T11:15:23Z)
Available: def5678
Health Check: healthy ✓
Duration: 2m 34s
```

---

### fraisier rollback

Roll back a fraise to its previous deployment.

```bash
fraisier rollback FRAISE ENVIRONMENT [OPTIONS]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise.
- `ENVIRONMENT` (required) -- Target environment.

**Options:**

| Option | Description |
|--------|-------------|
| `--to-version SHA` | Roll back to a specific git commit SHA |
| `--force` | Skip confirmation prompt |

Rollback checks out the previous (or specified) commit, reverses database migrations by
the same number of steps that were applied in the failed deployment, and restarts the
service.

**Examples:**

```bash
# Roll back to the previous deployment
fraisier rollback my_api production

# Roll back to a specific SHA
fraisier rollback my_api production --to-version abc1234

# Roll back without confirmation
fraisier rollback my_api production --force
```

---

### fraisier status

Check the status of a fraise in an environment: current version, latest version, health, and recent deployments.

```bash
fraisier status FRAISE ENVIRONMENT
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise.
- `ENVIRONMENT` (required) -- Target environment.

**Examples:**

```bash
fraisier status my_api production
fraisier status my_worker staging
```

---

### fraisier status-all

Show a table of all fraise states, with optional filters.

```bash
fraisier status-all [--environment ENV] [--type TYPE]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--environment ENV` | Filter by environment |
| `--type TYPE` | Filter by fraise type |

**Examples:**

```bash
# All fraises
fraisier status-all

# Only production
fraisier status-all --environment production

# Only API fraises
fraisier status-all --type api
```

---



**Options:**

| Option | Description |
|--------|-------------|
| `--status-file PATH` | Path to a custom `deployment_status.json` file |

**Examples:**

```bash
fraisier deploy-status
fraisier deploy-status --status-file /var/lib/fraisier/deployment_status.json
```

---

## Database Commands

### fraisier db reset

Reset a database from its template. This is a sub-second operation. Fraises with `external_db` are skipped.

```bash
fraisier db reset FRAISE -e ENV [--force]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise.

**Options:**

| Option | Description |
|--------|-------------|
| `-e ENV` | Target environment (required) |
| `--force` | Skip confirmation prompt |

**Examples:**

```bash
fraisier db reset my_api -e development
fraisier db reset my_api -e development --force
```

---

### fraisier db migrate

Run database migrations using the configured framework (Django, Alembic, etc.).

```bash
fraisier db migrate FRAISE -e ENV [-d up|down]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise.

**Options:**

| Option | Description |
|--------|-------------|
| `-e ENV` | Target environment (required) |
| `-d up\|down` | Migration direction (default: `up`) |

**Examples:**

```bash
fraisier db migrate my_api -e staging
fraisier db migrate my_api -e staging -d down
```

---

### fraisier db build

Build the database schema.

```bash
fraisier db build FRAISE -e ENV [--rebuild]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise.

**Options:**

| Option | Description |
|--------|-------------|
| `-e ENV` | Target environment (required) |
| `--rebuild` | Drop and rebuild the database schema |

**Examples:**

```bash
fraisier db build my_api -e development
fraisier db build my_api -e development --rebuild
```

---

### fraisier db-check

Check database health and connection pool metrics.

```bash
fraisier db-check
```

---

### fraisier backup

Run a `pg_dump` backup of a fraise's database. Slim mode excludes tables configured for exclusion.

```bash
fraisier backup FRAISE -e ENV [--mode full|slim]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise.

**Options:**

| Option | Description |
|--------|-------------|
| `-e ENV` | Target environment (required) |
| `--mode full\|slim` | Backup mode (default: `full`). `slim` excludes configured tables. |

**Examples:**

```bash
fraisier backup my_api -e production
fraisier backup my_api -e production --mode slim
```

---

## Infrastructure Commands

### fraisier bootstrap

Provision a virgin server end-to-end via SSH. Connects as root (or `--ssh-user`) and
runs 10 ordered, idempotent steps to bring a fresh server to a state where
`fraisier validate-setup` passes and the first `fraisier trigger-deploy` can succeed.

Use this instead of manual server setup. Re-running on a partially-set-up server is safe —
steps that find the work already done are skipped.

```bash
fraisier bootstrap --environment <env> [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--environment`, `-e` | Environment to bootstrap (required) |
| `--ssh-user` | Privileged SSH user for the initial connection (default: `root`) |
| `--ssh-key PATH` | Path to SSH private key |
| `--server HOST` | Override `environments.<env>.server` from `fraises.yaml` |
| `--dry-run` | Print all steps without executing anything |
| `--yes`, `-y` | Skip confirmation prompt |
| `--verbose`, `-v` | Show already-done steps and verbose install output |

**What it does (10 steps):**

1. Create the `deploy_user` system account
2. Add `deploy_user` to the `www-data` group
3. Install `uv` for `deploy_user`
4. Install `fraisier` for `deploy_user`
5. Create `/opt/<project>`, `/opt/fraisier`, `/run/fraisier`
6. Upload `fraises.yaml` to `/opt/fraisier/fraises.yaml`
7. Upload generated scaffold files to a temp directory
8. Run `install.sh --standalone` (systemd units, nginx, sudoers)
9. Enable and start the deploy socket unit
10. Run `fraisier validate-setup` remotely and report

The generated deploy service unit automatically sets `GIT_SSH_COMMAND=ssh -o
StrictHostKeyChecking=accept-new`, so the first `git fetch` succeeds without needing to
pre-populate `known_hosts` for github.com or your git host.

**Requirements:**

- `environments.<env>.server` must be set in `fraises.yaml`, or pass `--server <host>`
- SSH access as root (or another privileged user) to the target server

**Examples:**

```bash
# Bootstrap production (reads server from fraises.yaml)
fraisier bootstrap --environment production

# Preview all steps without executing
fraisier bootstrap --environment production --dry-run

# Override the target server
fraisier bootstrap --environment production --server 203.0.113.42

# Use a specific SSH key and non-root user
fraisier bootstrap -e production --ssh-user deployer --ssh-key ~/.ssh/id_ed25519

# Skip confirmation
fraisier bootstrap -e production --yes
```

**After bootstrap:**

```bash
# First deployment
fraisier trigger-deploy <fraise> production
```

---

### fraisier setup

Provision the server: create system users, directories, permissions, sudoers rules, and
install systemd units. Run once per server, or again after significant config changes.
Requires sudo / root.

```bash
fraisier setup [OPTIONS]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be provisioned without making changes |
| `--environment ENV` | Provision only this environment |
| `--server HOSTNAME` | Provision only environments assigned to this server |
| `--all-environments` | Provision every environment, including ones hosted elsewhere |
| `--yes`, `-y` | Skip confirmation prompt |

`--environment`, `--server` and `--all-environments` are mutually exclusive.

**Which environments get provisioned**

With no selector, `setup` works out which host this machine is and provisions
only that host's environments. It matches the machine's hostnames against
`servers:.machine_hostnames` first, then against logical server names.

- **No environment declares a `server:`** — single-host config. Every
  environment is provisioned, because "everything" and "this host's" are the
  same set.
- **The machine resolves to a declared host** — only that host's environments.
- **The machine resolves to nothing** — `setup` stops with an error naming this
  machine and the hosts the config knows (v0.57.0, #331). It does not fall back
  to provisioning everything: setup creates users, chowns trees and *enables*
  systemd units and nginx vhosts, so acting on every environment from a box
  that cannot identify itself is how a production host acquires development
  units. Register the machine under `servers:`, name the host with `--server`,
  or pass `--all-environments` to say "everything, deliberately".

**Examples:**

```bash
# Provision this host's environments (auto-detected)
sudo fraisier setup

# Provision only production
sudo fraisier setup --environment production

# Provision only environments on a named host
sudo fraisier setup --server prod.myserver.com

# Provision every environment regardless of host
sudo fraisier setup --all-environments

# Preview without changes
fraisier setup --dry-run
```

---

### fraisier scaffold

Generate infrastructure files from `fraises.yaml`. Outputs systemd units, nginx configuration, GitHub Actions workflows, sudoers rules, `install.sh`, and shell scripts.

```bash
fraisier scaffold [--dry-run]
```

**The artifact manifest** (v0.57.0)

Every render also writes `artifact-manifest.json` beside the files it
describes: for each artifact, where it came from, where it installs, under
which environment gate, and its sha256.

- **Every rendered file must have a disposition.** A file the classifier does
  not recognise is a hard error naming it — so a new artifact cannot be
  rendered and then installed by nobody, which is the "rendered ≠ installed"
  bug class (#323, #325). If you add one, give it a disposition in
  `fraisier/scaffold/artifacts.py`.
- `install.sh` is generated **from** the manifest and bakes in those hashes. It
  verifies the whole tree before installing anything and refuses a tree that
  does not match — an old installer against a fresh scaffold dir, or the
  reverse, would otherwise install files nobody described.
- `fraisier doctor` runs the same check, so a problem surfaces on your terminal
  or in CI rather than first on a live host mid-deploy.

`install.sh` also reports what it did *not* install: units owned by
`fraisier scheduled-install` (with their source tree), and artifacts that are
rendered but installed by nothing.

**Options:**

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what files would be generated without writing them |
| `--server HOSTNAME` | Only generate files for environments assigned to this server |

**Examples:**

```bash
fraisier scaffold
fraisier scaffold --dry-run

# On a multi-server setup, generate only this server's files
fraisier scaffold --server prod.myserver.com
```

---

### fraisier scaffold-install

Install generated scaffold files to system locations (sudoers, systemd units, nginx configs, the systemctl wrapper script, system dependencies).

Must run `fraisier scaffold` first to generate the files. Requires sudo access or root privileges.

```bash
fraisier scaffold-install [OPTIONS]
```

**Prerequisites:**

- Must run `fraisier scaffold` first
- Must have sudo access (or be running as root)
- Generated files must be in `PROJECT_DIR` (usually `/opt/<project_name>`)

**Host scoping** (v0.59.0)

On a multi-host config, both the units *and* the managed directories are
scoped to this machine: a production-only host no longer creates the dev host's
`git_repo` and `app_path`. Paths that belong to no environment —
`/opt/fraisier`, `/var/lib/fraisier`, the config directory — are still created
everywhere.

Scoping is by `(fraise, environment)`
([#336](https://github.com/fraiseql/fraisier/issues/336)). Two fraises using
one environment name on different servers each install their own units only.
An environment declared in the global `environments:` section has no owning
fraise and binds every fraise using that name, so a config written that way
scopes exactly as it did before.

A few artifacts are scoped by environment alone, because no single fraise owns
them: the unit-installer helper is one per `(project, environment)`, and the
postgresql logging conf is per environment. Every host declaring that
environment installs them.

Units installed on a host before v0.59.0 that belong to a fraise running
elsewhere are **not** removed by re-running this command — they may be another
application's running services. `fraisier scaffold-diff` and `fraisier doctor`
list them with their owner; `--prune-foreign` disables and deletes them.

**Options:**

| Option | Description |
|--------|-------------|
| `--dry-run` | Preview what would be installed without making changes |
| `--validate-only` | Check prerequisites only (no installation) |
| `--yes`, `-y` | Skip confirmation prompt (useful for automation) |
| `--verbose`, `-v` | Enable verbose output |
| `--prune-foreign` | Disable and delete units owned by a fraise that does not run on this host. Never happens by default; run `scaffold-diff` first to see what would go. |

**Examples:**

```bash
# Preview what would be installed
fraisier scaffold-install --dry-run

# Check prerequisites
fraisier scaffold-install --validate-only

# Install without confirmation prompt
fraisier scaffold-install --yes

# Install with verbose output
fraisier scaffold-install --verbose
```

**Typical Workflow:**

```bash
# 1. Generate infrastructure files
fraisier scaffold

# 2. Review the changes
git diff scripts/generated/

# 3. Preview installation (without changes)
fraisier scaffold-install --dry-run

# 4. Install to system
fraisier scaffold-install --yes

# 5. Verify services are running
systemctl status <service-name>
```

**Adding a `type: scheduled` job:**

When declaring a *new* `type: scheduled` job (with `systemd_service` and `systemd_timer` under `jobs.*`) in `fraises.yaml`, the host must be reconciled in this order:

```bash
# 1. Re-render scaffold output with the updated systemctl-helper allowlist.
fraisier scaffold

# 2. Install the regenerated helper unit — this is what actually updates
#    the on-disk allowlist so the webhook-driven deployer can manage the
#    new timer/service via the helper socket.
fraisier scaffold-install --yes

# 3. Lay down the job's unit files into /etc/systemd/system/ and enable
#    the timer. Reads from <app_path>/scripts/systemd/<unit>.
sudo fraisier scheduled-install --env <env>
```

Skipping steps 1–2 leaves the on-disk `fraisier-<project>-systemctl-helper.service` carrying the *old* allowlist (rendered before the new job was declared). Webhook-driven redeploys of the new timer will then be rejected by the helper socket until those steps run. The change to `_collect_allowed_services` in v0.28.0 updates the *generator*; the rendered helper unit on each host is a separate artefact that only refreshes when `scaffold-install` runs.

#### `--via-socket` (v0.29+)

By default `fraisier scheduled-install` writes directly into `/etc/systemd/system/` and runs `systemctl` itself — both of which require root, so the operator must invoke the whole command under `sudo`.

The `--via-socket` flag routes the apply through the new `fraisier-unit-installer` socket helper (also rendered by `scaffold` in v0.29). The helper runs as root under systemd; the CLI itself can run as the deploy user. Drops the `sudo` requirement and gains real SO_PEERCRED enforcement, a render-time allowlist, and TOCTOU realpath checks on the dest parent.

```bash
fraisier scheduled-install --env production --via-socket --yes
```

By default the CLI uses `/run/fraisier/<env>/unit-installer-<project>.sock`. Override with `--socket-path` if you've rendered the helper to a different path.

The helper must be on the host first: run `fraisier scaffold && fraisier scaffold-install --yes` once on each host after upgrading to v0.29. If the socket isn't present, `--via-socket` fails with an actionable error pointing at `scaffold-install`.

`--via-socket` will become the default in a future release; the legacy direct-write path will stay available behind an opt-out flag for at least one release after that.

#### `--prune` (v0.29+)

Removes orphan units — those still on disk under their `.fraisier-managed` marker but no longer declared in `fraises.yaml`. Operator-driven cleanup for fraises (or job entries within them) that have been removed from config.

```bash
# List what would be pruned for this env. No writes.
sudo fraisier scheduled-install --env production --prune --dry-run

# Actually disable + remove the orphans.
sudo fraisier scheduled-install --env production --prune --yes
```

`--prune` walks `/etc/systemd/system/` for `*.fraisier-managed` markers whose `environment` field matches `--env` and whose `fraises_yaml_path` resolves to the same file as the current config (per-yaml + per-env scoping — running `--prune --env staging` from one project won't sweep another project's production units that happen to share a host). For each match:

1. If the unit is `.timer`, `systemctl disable --now` it first (so it can't fire mid-prune).
2. Else (the `.service` half), `systemctl stop` it.
3. Remove the unit file and its `.fraisier-managed` sidecar.
4. After all removes, `systemctl daemon-reload` once.

Markers without a paired unit on disk (operator manually `rm`'d the `.timer` but left the sidecar) or with corrupt JSON are classified as `stale_marker` — the marker is cleaned up, no `systemctl` invocations.

**Markers are advisory, not authenticated.** They live in `/etc/systemd/system/`, which is root-only-write — an unprivileged adversary on the host cannot plant fake markers to bait `--prune` into removing a victim unit. A root-side adversary can do anything anyway; the marker convention is a cross-project / cross-env safety net for honest operator mistakes, not a defense against root.

v0.29 only supports `--prune` under operator-typed `sudo` (the CLI walks the filesystem and invokes `systemctl` directly). `--prune --via-socket` will land in v0.30 with `RemoveFileOp` + ordered pre-actions in the helper protocol.

#### Webhook-driven auto-install (v0.29+)

After running `fraisier scaffold-install` once per host, webhook deploys of `type: scheduled` fraises **automatically** install new unit files into `/etc/systemd/system/` via the unit-installer socket helper. The manual `sudo fraisier scheduled-install` workflow becomes an override (rollback debugging, change-control), not a routine post-deploy step.

The drift policy per env lives in fraises.yaml:

```yaml
fraises:
  alerter:
    type: scheduled
    environments:
      production:
        scheduled:
          auto_install:
            on_missing: install       # default — copy new units from worktree
            on_drift: fail             # default — refuse to overwrite hand-edits
            # on_drift: overwrite      # opt-in: repo is source of truth
            # on_drift: skip           # opt-in: leave hand-edits, log warning
```

`on_drift` choices:

- **`fail`** (default): webhook deploy aborts before any write when source and dest differ. The operator sees the drifted unit names in the deploy error. Resolve by reverting the hand-edit, running `fraisier scheduled-install --env <env> --validate-only` to confirm convergence, or opting into `overwrite` / `skip` per-fraise.
- **`overwrite`**: webhook silently replaces the drifted dest with source. A `WARNING` is logged listing each unit overwritten. The `deploy_event` carries `drift_overwrites: [...]` so external tooling can surface the change.
- **`skip`**: webhook leaves drifted dest alone, logs a `WARNING`, and records the units in `deploy_event.skipped_drift_units`. ABSENT units (new declarations) still install.

Hosts that haven't been bootstrapped with v0.29's helper (no `/run/fraisier/<env>/unit-installer-<project>.sock`) silently fall back to the legacy systemctl path with a `WARNING` log pointing at `scaffold-install`. Run `fraisier scaffold-install --yes` once per host to close the gap.

Concurrent-deploy contention: if another deploy is in flight when the webhook tries to install, the helper returns `busy` and the deployer retries with 1s / 3s / 10s backoffs. Past the budget (3 attempts, ≤30s total wait), the deploy fails with `another deploy in flight` so the operator knows it's a contention issue, not a code bug.

The webhook never runs `--prune`. Orphan removal stays an explicit operator opt-in.

---

### fraisier validate-deployment

Run a comprehensive readiness check for a specific fraise/environment before deploying.
Checks config validity, bare repo reachability, required env vars, the systemctl wrapper
script, systemd service registration, and database credentials.

```bash
fraisier validate-deployment FRAISE ENVIRONMENT [--json]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise.
- `ENVIRONMENT` (required) -- Target environment.

**Options:**

| Option | Description |
|--------|-------------|
| `--json` | Output results as structured JSON |

**Examples:**

```bash
fraisier validate-deployment my_api production
fraisier validate-deployment my_api production --json
```

---

### fraisier validate

Run pre-deploy validation checks: `config_valid`, `deploy_user`, and `fraises_have_environments`.

```bash
fraisier validate [--json]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--json` | Output results as structured JSON |

**Examples:**

```bash
fraisier validate
fraisier validate --json
```

---

### fraisier health

Check health of all services. Displays a table by default.

```bash
fraisier health [--env ENV] [--json] [--wait]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--env ENV` | Filter by environment |
| `--json` | Output as JSON |
| `--wait` | Wait for services to become healthy |

**Examples:**

```bash
fraisier health
fraisier health --env production
fraisier health --json
fraisier health --env staging --wait
```

---

## Version Commands

### fraisier version

Show the Fraisier package version.

```bash
fraisier version
```

---

### fraisier version show

Show contents of `version.json`: version, commit, branch, schema hash, and database version.

```bash
fraisier version show [--version-file PATH]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--version-file PATH` | Path to a custom `version.json` file |

**Examples:**

```bash
fraisier version show
fraisier version show --version-file /opt/my_api/version.json
```

---

### fraisier version bump

Bump the semantic version. Creates a `.bak` backup of the version file.

```bash
fraisier version bump major|minor|patch [--version-file PATH] [--dry-run] [--no-tag]
```

**Arguments:**

- `major|minor|patch` (required) -- The version component to bump.

**Options:**

| Option | Description |
|--------|-------------|
| `--version-file PATH` | Path to a custom `version.json` file |
| `--dry-run` | Show what the new version would be without writing |
| `--no-tag` | Skip creating a git tag |

**Examples:**

```bash
fraisier version bump patch
fraisier version bump minor --dry-run
fraisier version bump major --no-tag
```

---

## Observability Commands

### fraisier history

Show deployment history as a table.

```bash
fraisier history [--fraise NAME] [--environment ENV] [--limit N]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--fraise NAME` | Filter by fraise name |
| `--environment ENV` | Filter by environment |
| `--limit N` | Number of entries to show (default: 20) |

**Examples:**

```bash
fraisier history
fraisier history --fraise my_api
fraisier history --fraise my_api --environment production --limit 50
```

---

### fraisier stats

Show deployment statistics: success rate, average duration, and more.

```bash
fraisier stats [--fraise NAME] [--days N]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--fraise NAME` | Filter by fraise name |
| `--days N` | Number of days to include (default: 30) |

**Examples:**

```bash
fraisier stats
fraisier stats --fraise my_api
fraisier stats --fraise my_api --days 7
```

---

### fraisier webhooks

Show recent webhook events.

```bash
fraisier webhooks [--limit N]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--limit N` | Number of events to show (default: 10) |

**Examples:**

```bash
fraisier webhooks
fraisier webhooks --limit 50
```

---

### fraisier metrics

Start a Prometheus metrics exporter endpoint.

```bash
fraisier metrics [--port PORT] [--address ADDR]
```

**Options:**

| Option | Description |
|--------|-------------|
| `--port PORT` | Port to listen on |
| `--address ADDR` | Address to bind to |

**Examples:**

```bash
fraisier metrics
fraisier metrics --port 9090
fraisier metrics --port 9090 --address 0.0.0.0
```

---

## Provider Commands

### fraisier providers

List all available deployment providers.

```bash
fraisier providers
```

The built-in providers are: `bare_metal` and `docker_compose`.

---

### fraisier provider-info

Show detailed information about a specific provider.

```bash
fraisier provider-info TYPE
```

**Arguments:**

- `TYPE` (required) -- Provider type (e.g., `bare_metal`, `docker_compose`).

**Examples:**

```bash
fraisier provider-info bare_metal
fraisier provider-info docker_compose
```

---

### fraisier provider-test

Run pre-flight checks for a provider to verify connectivity and configuration.

```bash
fraisier provider-test TYPE [-f CONFIG]
```

**Arguments:**

- `TYPE` (required) -- Provider type.

**Options:**

| Option | Description |
|--------|-------------|
| `-f CONFIG` | Path to a provider configuration file |

**Examples:**

```bash
fraisier provider-test bare_metal
fraisier provider-test docker_compose -f docker-provider.yaml
```

---

## Diagnostic Commands

These commands isolate individual deployment components for debugging. Run them when a
deployment fails to identify exactly which step is broken.

### fraisier test-git

Test git operations: bare repo existence, remote reachability, current and latest versions.

```bash
fraisier test-git FRAISE ENVIRONMENT
```

**Examples:**

```bash
fraisier test-git my_api production
```

---

### fraisier test-install

Run the `install.command` (e.g. `uv sync --frozen`) in `app_path` and report the result.

```bash
fraisier test-install FRAISE ENVIRONMENT
```

**Examples:**

```bash
fraisier test-install my_api production
```

---

### fraisier test-health

Perform one health check against `health_check.url` and report the HTTP status and response.

```bash
fraisier test-health FRAISE ENVIRONMENT
```

**Examples:**

```bash
fraisier test-health my_api production
```

---

### fraisier test-database

Open a connection using `database_url` and verify the database is reachable and the schema
is in the expected state.

```bash
fraisier test-database FRAISE ENVIRONMENT
```

**Examples:**

```bash
fraisier test-database my_api production
```

---

### fraisier test-wrapper

Verify that the systemctl wrapper script is present, executable, and that the sudoers rule
allows the deploy user to invoke it.

```bash
fraisier test-wrapper FRAISE ENVIRONMENT WRAPPER_TYPE COMMAND [ARGS...]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise.
- `ENVIRONMENT` (required) -- Target environment.
- `WRAPPER_TYPE` (required) -- `systemctl` (the only supported type).
- `COMMAND` (required) -- Command to test (e.g. `restart`).

**Examples:**

```bash
# Test that the deploy user can restart the service via wrapper
fraisier test-wrapper my_api production systemctl restart
```

---

## Ship Commands

### fraisier ship

Bump the version and ship: commit, push, and optionally open a pull request or deploy.

```bash
fraisier ship patch|minor|major [OPTIONS]
```

**Arguments:**

- `patch|minor|major` (required) -- The version component to bump.

**Options:**

| Option | Description |
|--------|-------------|
| `--no-bump` | Skip the version bump |
| `--dry-run` | Show what would happen without making changes |
| `--no-deploy` | Skip deployment after merging |
| `--pr` | Open a pull request instead of pushing directly |
| `--pr-base BRANCH` | Base branch for the pull request (default: `main`) |
| `--skip-checks` | Skip pre-ship checks (lint, tests) |
| `--version-file PATH` | Path to a custom `version.json` |
| `--pyproject PATH` | Path to a custom `pyproject.toml` |
| `--format text\|json` | Output format. `json` is supported on `--dry-run` only; emits `{version: {old, new, bump_type}, dry_run: true, ...}`. |

**Examples:**

```bash
# Bump patch, commit, push, deploy
fraisier ship patch

# Bump minor, open a PR to main
fraisier ship minor --pr

# Bump major, dry run
fraisier ship major --dry-run

# Ship without bumping (e.g. docs-only change)
fraisier ship patch --no-bump
```
