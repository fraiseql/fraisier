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
- Syncs `fraises.yaml` from git to the server
- Detects if configuration changed using hash comparison
- Regenerates and installs scaffold files if needed

This keeps the server in sync with your git repository automatically. See [deployment-guide.md](./deployment-guide.md#configuration-synchronization-automatic) for details.

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
| `--yes`, `-y` | Skip confirmation prompt |

**Examples:**

```bash
# Provision everything defined in fraises.yaml
sudo fraisier setup

# Provision only production
sudo fraisier setup --environment production

# Provision only environments on this host
sudo fraisier setup --server prod.myserver.com

# Preview without changes
fraisier setup --dry-run
```

---

### fraisier scaffold

Generate infrastructure files from `fraises.yaml`. Outputs systemd units, nginx configuration, GitHub Actions workflows, sudoers rules, `install.sh`, and shell scripts.

```bash
fraisier scaffold [--dry-run]
```

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

Install generated scaffold files to system locations (sudoers, systemd units, nginx configs, wrapper scripts, system dependencies).

Must run `fraisier scaffold` first to generate the files. Requires sudo access or root privileges.

```bash
fraisier scaffold-install [OPTIONS]
```

**Prerequisites:**

- Must run `fraisier scaffold` first
- Must have sudo access (or be running as root)
- Generated files must be in `PROJECT_DIR` (usually `/opt/<project_name>`)

**Options:**

| Option | Description |
|--------|-------------|
| `--dry-run` | Preview what would be installed without making changes |
| `--validate-only` | Check prerequisites only (no installation) |
| `--yes`, `-y` | Skip confirmation prompt (useful for automation) |
| `--verbose`, `-v` | Enable verbose output |

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

---

### fraisier validate-deployment

Run a comprehensive readiness check for a specific fraise/environment before deploying.
Checks config validity, bare repo reachability, required env vars, wrapper scripts, systemd
service registration, and database credentials.

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

Verify that a wrapper script is present, executable, and that the sudoers rule allows the
deploy user to invoke it.

```bash
fraisier test-wrapper FRAISE ENVIRONMENT WRAPPER_TYPE COMMAND [ARGS...]
```

**Arguments:**

- `FRAISE` (required) -- Name of the fraise.
- `ENVIRONMENT` (required) -- Target environment.
- `WRAPPER_TYPE` (required) -- `systemctl` or `pg`.
- `COMMAND` (required) -- Command to test (e.g. `restart`, `psql`).

**Examples:**

```bash
# Test that the deploy user can restart the service via wrapper
fraisier test-wrapper my_api production systemctl restart

# Test that the pg wrapper can connect to the database
fraisier test-wrapper my_api production pg psql
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
