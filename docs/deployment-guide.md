# Fraisier Deployment Guide

This guide walks through setting up and operating Fraisier deployments on a Linux server —
from first install through day-to-day operations.

---

## Prerequisites

- Linux server (Ubuntu 22.04+, Debian 12+, or similar)
- Python 3.11+
- Git
- systemd
- sudo access for the initial setup

---

## Installation

```bash
pip install fraisier
```

Verify:

```bash
fraisier --version
```

---

## Configuration: fraises.yaml

`fraises.yaml` is the single source of truth for your deployment configuration. Fraisier
searches for it in the current directory, `./config/`, `/opt/<project_name>/`, or the path
set in `$FRAISIER_CONFIG`.

### Minimal example

```yaml
name: myapp

git:
  provider: github
  github:
    webhook_secret: !envvar FRAISIER_WEBHOOK_SECRET

scaffold:
  deploy_user: fraisier       # system user that runs deployments
  output_dir: scripts/generated

fraises:
  my_api:
    type: api
    environments:
      production:
        branch: main
        clone_url: https://github.com/org/my-api.git
        git_repo: /var/lib/fraisier/repos/my-api.git   # local bare repo
        app_path: /var/www/my-api                       # git worktree
        systemd_service: my-api.service
        install:
          command: [uv, sync, --frozen]
          user: myapp            # run install as application user
        service:
          user: myapp
          exec: "/var/www/my-api/.venv/bin/gunicorn myapp.wsgi"
          port: 8000
        health_check:
          url: http://localhost:8000/health
          timeout: 30
          retries: 5
        database:
          framework: django  # or alembic, peewee, confiture
          name: myapp_prod
          django:
            settings_module: myapp.settings

branch_mapping:
  main:
    fraise: my_api
    environment: production
```

### How secrets work

Use the `!envvar VAR_NAME` YAML tag in `fraises.yaml`. The tag resolves to the contents of
`os.environ['VAR_NAME']` when the config file is parsed; missing variables raise
`ConfigurationError` immediately. Never commit actual secrets. A typical environment file at
`/etc/myapp/prod.env`:

```bash
FRAISIER_WEBHOOK_SECRET=<min 32 chars, random>
DATABASE_URL=postgresql://myapp:pass@localhost/myapp_production
DATABASE_ADMIN_URL=postgresql://postgres@/postgres?host=/var/run/postgresql
```

`${VAR}` shell-style substitution is supported **only** inside the `notifications:` and
`hooks:` blocks (expanded at fire time by their dispatchers). Use `!envvar` for everything
else — most consumers read the YAML value literally and will not expand `${VAR}`.

---

## Server Setup (one-time)

You have two paths depending on whether the server is completely fresh or already partially
configured.

### Option A — Bootstrap (recommended for fresh servers)

`fraisier bootstrap` provisions a virgin server end-to-end from your local machine via SSH.
It performs all setup steps in one command and leaves the server ready for the first deploy.

First, add the server hostname to `fraises.yaml`:

```yaml
environments:
  production:
    server: prod.myserver.com
```

Then run:

```bash
fraisier bootstrap --environment production
```

Bootstrap runs 10 idempotent steps (create user → install uv → install fraisier → upload
config → upload scaffold → run install.sh → enable socket → validate). Re-running is safe.

Preview without making changes:

```bash
fraisier bootstrap --environment production --dry-run
```

Once bootstrap reports success, skip straight to [First deployment](#first-deployment).

---

### Option B — Manual setup (server already accessible)

Use this path when you are already SSH'd into the server, or when the server has an
existing partial configuration that bootstrap would overwrite.

#### 1. Generate scaffold files

Scaffold generates all infrastructure files from `fraises.yaml` — systemd units, nginx
configs, sudoers fragments, wrapper scripts, and more.

```bash
fraisier scaffold
```

Review what was generated:

```bash
git diff scripts/generated/
```

For a multi-server setup where each server only needs its own environments:

```bash
fraisier scaffold --server prod.myserver.com
```

#### 2. Preview and install scaffold

```bash
# Preview without changes
fraisier scaffold-install --dry-run

# Install to the system (copies to /etc/systemd/system/, /etc/sudoers.d/, etc.)
sudo fraisier scaffold-install --yes
```

#### 3. Run server setup

```bash
sudo fraisier setup
```

`fraisier setup` performs these steps:

1. Creates `deploy_user` (e.g. `fraisier`) and any application users defined under
   `service.user` in each environment
2. Creates system directories:
   - `/var/lib/fraisier/repos/` — bare git repositories
   - `/var/lib/fraisier/status/` — deployment status files
   - `/run/fraisier/` — lock files
3. Sets ownership and permissions on `app_path` so the deploy user can write to the
   worktree while the application user owns the running code
4. Configures `git config --global safe.directory` for the app paths
5. Installs the sudoers fragment (`/etc/sudoers.d/<project>`)
6. Installs the webhook systemd unit and application service units
7. Reloads systemd: `systemctl daemon-reload`

Filter to a specific environment or server:

```bash
sudo fraisier setup --environment production
sudo fraisier setup --server prod.myserver.com
```

---

## The Git Model: bare repo + worktree

Fraisier does not use `git pull` in the traditional sense. It uses:

- **Bare repository** (`git_repo`): a local mirror of the remote, e.g.
  `/var/lib/fraisier/repos/my-api.git`. This is never modified by the application.
- **Worktree** (`app_path`): the checked-out application code, e.g. `/var/www/my-api`.
  Only Fraisier writes here, via `git checkout`.

On every deployment:

```
git -C /var/lib/fraisier/repos/my-api.git fetch origin
git --work-tree=/var/www/my-api --git-dir=.../my-api.git checkout -f origin/main
```

This means:
- Rollback is instant — it just checks out the previous SHA
- There is no risk of merge conflicts or dirty state in the worktree
- The bare repo is the only copy on disk that tracks history

The bare repo is created automatically on first deploy from `clone_url`.

---

## First Deployment

### 1. Validate readiness

```bash
fraisier validate
fraisier validate-deployment my_api production
```

`validate-deployment` checks that the bare repo exists or is fetchable, credentials are set,
wrapper scripts are present, and systemd services are known.

### 2. Deploy

```bash
fraisier deploy my_api production
```

The deployment sequence:

1. **Git fetch + checkout**: fetches from `clone_url`, checks out `branch` into `app_path`.
2. **Install dependencies**: runs `install.command` (e.g. `uv sync --frozen`) in `app_path`,
   optionally as `install.user` via `sudo -u`. If `install.user` differs from `deploy_user`,
   fraisier ensures `.venv` is owned by `install.user` before running (prevents permission
   errors if the directory was previously written by the deploy user).
3. **Config sync**: copies `fraises.yaml` from the git worktree to the path set in
   `FRAISIER_CONFIG` (injected into the deploy daemon by the systemd unit — defaults to
   `/opt/fraisier/fraises.yaml`). Detects whether the file changed using a SHA-256 hash. If
   changed, regenerates and installs scaffold automatically.
4. **Database migrations**: runs the configured strategy (see below).
5. **Service restart**: calls `systemctl restart` via the restricted wrapper script.
6. **Health check**: polls `health_check.url` with exponential backoff until the service
   responds healthy or retries are exhausted.
7. **Auto-rollback on failure**: if the health check fails and a previous SHA is available,
   Fraisier undoes migrations, checks out the old SHA, restarts the service, and marks the
   deployment as `ROLLED_BACK`.

### 3. Monitor progress

```bash
# Live status
fraisier status my_api production

# All fraises at once
fraisier status-all

# Recent deployments
fraisier history --fraise my_api --limit 10
```

---

## Database Migration Strategies

### Framework support

Fraisier supports major Python migration frameworks:

| Framework | Command | Use case |
|---|---|---|
| **Django** | `python manage.py migrate` | Django projects |
| **Alembic** | `alembic upgrade head` | SQLAlchemy projects |
| **Flask-Migrate** | `alembic upgrade head` | Flask + SQLAlchemy |
| **Peewee** | Custom migration runner | Peewee ORM |
| **Confiture** | `confiture migrate up` | FraiseQL or custom schemas |

Fraisier handles framework-specific migration commands automatically.

### Irreversible migrations

If a migration cannot be rolled back (e.g. a destructive schema change), use:

```bash
fraisier deploy my_api production --no-rollback
```

This disables the automatic rollback-on-health-check-failure. Combined with `--skip-health`:

```bash
fraisier deploy my_api production --no-rollback --skip-health
```

### Running migrations manually

```bash
# Apply pending migrations
fraisier db migrate my_api -e production

# Roll back one step
fraisier db migrate my_api -e production -d down
```

### Rebuild strategy: template version stamp

When `database.strategy: rebuild` and `database.create_template: true`, fraisier
writes the build-time application version into the source DB's
`public.tb_version.app_version` column immediately **before** cloning the
template. The atomic `CREATE DATABASE … TEMPLATE …` carries the stamp into the
template, so a downstream "reseed from template" endpoint can verify that the
template matches the running application before restoring.

The stamped version is resolved from, in order:

1. `database.app_version` in `fraises.yaml` (explicit override; rarely needed).
   An invalid value here (anything outside `[A-Za-z0-9._+\-]`, including PEP 440
   epoch forms like `1!2.3.4`) is rejected at construction with a `ValueError`
   — typos are not silently accepted.
2. `<app_path>/version.json` `version` field (preferred — written by the
   deployer).
3. `<app_path>/pyproject.toml` `[project].version` (fallback).

If none resolve, or if `public.tb_version` does not exist, or if `tb_version`
exists but contains zero rows, fraisier logs a warning and continues without
stamping. The protection is fail-safe: a missing or mismatched stamp causes
the *consumer* to refuse a reseed, not fraisier to refuse a rebuild.

**Schema requirement**: the stamp UPDATE has no WHERE clause and will silently
no-op on an empty `tb_version`. Projects must seed `tb_version` with at least
one row as part of their schema for the stamp to take effect. Fraisier emits
a distinct warning (`"tb_version is empty …"`) in this case so the
misconfiguration is visible in build logs.

**Upgrade note**: templates built by fraisier versions before this feature
shipped are unstamped. Consumers should either expect a rebuild after upgrade,
or treat missing stamps as "unknown" during a cutover window rather than
rejecting outright.

#### Consumer-side check (worked example)

The stamp lives in the template database, so the consumer must open a
connection targeting the template name — there is no asyncpg API for switching
databases on an existing connection. The example uses plain `asyncpg.connect`
against a DSN whose path is the template name:

```python
import asyncpg
from urllib.parse import urlparse, urlunparse


def _dsn_for_db(base_dsn: str, db_name: str) -> str:
    parsed = urlparse(base_dsn)
    return urlunparse(parsed._replace(path=f"/{db_name}"))


async def reseed_endpoint(
    app_version: str,
    template_name: str,
    base_dsn: str,
) -> None:
    conn = await asyncpg.connect(_dsn_for_db(base_dsn, template_name))
    try:
        stamped = await conn.fetchval(
            "SELECT app_version FROM public.tb_version LIMIT 1"
        )
    finally:
        await conn.close()

    if stamped is None:
        # Either the template was built by a pre-stamping fraisier version,
        # or the project's tb_version is empty. Decide policy:
        # - Strict: reject and require a rebuild.
        # - Lenient (cutover): accept, log a warning.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Template {template_name} has no version stamp. "
                f"Rebuild the template before reseeding."
            ),
        )
    if stamped != app_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Refusing to reseed: template {template_name} is stamped "
                f"with app_version={stamped!r} but the running app is "
                f"{app_version!r}. Rebuild before reseeding."
            ),
        )
    # ... proceed with reseed ...
```

Projects using a connection pool can replace the bare `asyncpg.connect` with
their pool's per-DB acquisition pattern (e.g. a registry of pools keyed by
database name) — the SQL is the same.

---

## Multi-Environment Setup

A typical setup has staging and production on separate servers. In `fraises.yaml`, assign
each environment a server:

```yaml
environments:
  staging:
    server: staging.myserver.com
  production:
    server: prod.myserver.com
```

Then on each server, run scaffold and setup filtered to that server:

```bash
# On staging.myserver.com
fraisier scaffold --server staging.myserver.com
sudo fraisier scaffold-install --yes
sudo fraisier setup --server staging.myserver.com

# On prod.myserver.com
fraisier scaffold --server prod.myserver.com
sudo fraisier scaffold-install --yes
sudo fraisier setup --server prod.myserver.com
```

The `--server` filter ensures that:
- Systemd units are only generated for environments assigned to that server
- The webhook service's `ReadWritePaths` only includes paths that exist locally
- Sudoers entries only reference local service names

### Branch mapping for multiple environments

```yaml
branch_mapping:
  main:
    fraise: my_api
    environment: production
  staging:
    fraise: my_api
    environment: staging
```

Pushing to `main` triggers a production deployment; pushing to `staging` triggers a staging
deployment.

---

## Security Model

### Two-user model

Fraisier separates deployment and application concerns into two system users:

| User | Purpose | Has access to |
|---|---|---|
| `deploy_user` (e.g. `fraisier`) | Runs fraisier, the webhook, git operations | `app_path` (write), systemctl wrapper |
| `service.user` / `install.user` (e.g. `myapp`) | Runs the application process and install command | `app_path` (read/exec), database |

The deploy user never runs the application. The application user never touches deployment
infrastructure.

When `install.user` differs from `deploy_user`, fraisier runs `chown -R <install.user>
<app_path>/.venv` before the install step. This prevents permission errors that occur when the
`.venv` directory (gitignored, so not recreated by checkout) was previously owned by the deploy
user.

### Service restart wrapper

Fraisier generates one restricted wrapper script and installs it via sudoers:

**`systemctl-wrapper.sh`** — `deploy_user` can only restart the specific services listed in
`fraises.yaml`. It cannot stop, start, or touch any other service.

It is referenced via an environment variable:

```bash
FRAISIER_SYSTEMCTL_WRAPPER=/path/to/systemctl-wrapper.sh
```

### PostgreSQL admin operations

Strategies that perform privileged DB operations (`rebuild`, `restore_migrate`) require a
superuser `admin_url` in the environment's `database.admin_url` field. No sudo or wrapper
script is involved — `dbops` connects directly via libpq.

The recommended form is peer-auth over the Unix socket:

```yaml
database:
  admin_url: "postgresql:///postgres?host=/var/run/postgresql"
```

Check that the systemctl wrapper is in place before deploying:

```bash
fraisier validate-deployment my_api production
```

Or test it directly:

```bash
fraisier test-wrapper my_api production systemctl restart
```

### Webhook security

- HMAC signature verification (GitHub, Gitea, Bitbucket) or token comparison (GitLab)
- Requires `FRAISIER_WEBHOOK_SECRET` (minimum 32 characters)
- Rate-limited to 10 requests/minute per IP
- Webhook requests that fail signature verification are rejected with 403

---

## Webhook Setup

### 1. Start the webhook server

The scaffold generates a systemd unit (`fraisier-<project>-webhook.service`). Enable it:

```bash
sudo systemctl enable fraisier-myapp-webhook.service
sudo systemctl start fraisier-myapp-webhook.service
```

The webhook server listens on port 8080 by default. Configure via environment variables:

```bash
FRAISIER_WEBHOOK_SECRET=...
FRAISIER_PORT=8080              # default
FRAISIER_HOST=0.0.0.0           # default
FRAISIER_GIT_PROVIDER=github    # github, gitlab, gitea, or bitbucket
```

### 2. Configure the webhook in your git provider

**GitHub**: Repository → Settings → Webhooks → Add webhook
- Payload URL: `https://deploy.mycompany.com/webhook`
- Content type: `application/json`
- Secret: value of `FRAISIER_WEBHOOK_SECRET`
- Events: Just the push event

**GitLab**: Repository → Settings → Webhooks
- URL: `https://deploy.mycompany.com/webhook`
- Secret token: value of `FRAISIER_WEBHOOK_SECRET`
- Trigger: Push events

**Gitea** / **Bitbucket**: Similar — set target URL and secret.

### 3. Verify webhook delivery

```bash
fraisier webhooks --limit 10
```

---

## Operational Procedures

### Checking deployment status

```bash
# Single fraise
fraisier status my_api production

# All fraises in a table
fraisier status-all

# Last deployment from the status file
fraisier deploy-status

# Deployment history
fraisier history --fraise my_api --environment production --limit 20

# Statistics
fraisier stats --fraise my_api --days 7
```

### Manual rollback

```bash
# Roll back to the last known good SHA
fraisier rollback my_api production

# Roll back to a specific commit
fraisier rollback my_api production --to-version abc1234
```

Rollback runs the reverse migration steps, checks out the old code, and restarts the service.

### Viewing logs

```bash
# Webhook server logs
sudo journalctl -u fraisier-myapp-webhook.service -f

# Application logs
sudo journalctl -u my-api.service -f

# Deployment activity (fraisier's own output)
sudo journalctl -u fraisier-myapp-webhook.service --since "1 hour ago"
```

### Checking health

```bash
# All services
fraisier health

# Production only
fraisier health --env production

# Wait until healthy (useful in scripts)
fraisier health --env production --wait
```

### Pre-deployment validation

Before deploying to production, check readiness:

```bash
fraisier validate-deployment my_api production
```

Checks performed:
- Configuration is valid
- Bare repository is reachable
- Required environment variables are set
- Wrapper scripts exist and are executable
- Systemd service is known to systemd
- Database credentials are valid (if database configured)

### Conditional deployment

Deploy only if the remote has new commits:

```bash
fraisier deploy my_api production --if-changed
```

Useful in cron jobs or CI pipelines where you want to avoid no-op deployments.

---

## Diagnostic Commands

When a deployment fails, the `test-*` commands isolate which component is broken.

### Test git operations

```bash
fraisier test-git my_api production
```

Checks: bare repo exists, remote is reachable, current version, latest version.

### Test install step

```bash
fraisier test-install my_api production
```

Runs the `install.command` in `app_path` and reports the outcome and any error output.

### Test health check

```bash
fraisier test-health my_api production
```

Performs one health check against `health_check.url` and reports HTTP status and response.

### Test database connection

```bash
fraisier test-database my_api production
```

Opens a connection using `database_url` and verifies the database is reachable and the
schema is in the expected state.

### Test the systemctl wrapper

```bash
fraisier test-wrapper my_api production systemctl restart
```

Verifies that the wrapper script is in place, executable, and that the sudo rule allows
the deploy user to invoke it.

---

## Troubleshooting

### Deployment fails immediately

```bash
# Check the last deployment record
fraisier history --fraise my_api --limit 1

# Run full pre-flight check
fraisier validate-deployment my_api production

# Test each component individually
fraisier test-git my_api production
fraisier test-install my_api production
fraisier test-health my_api production
```

### Health check fails after deploy (auto-rollback triggered)

The deployment status will show `ROLLED_BACK`. To investigate:

```bash
fraisier status my_api production
sudo journalctl -u my-api.service -n 100
fraisier test-health my_api production
```

If rollback also failed, an incident file is written to
`/var/lib/fraisier/incidents/<fraise>_<timestamp>.json`.

### Wrapper script errors

`Error: FRAISIER_SYSTEMCTL_WRAPPER not set` or `not executable`:

```bash
# Check the env var is set in the deploy user's environment
sudo -u fraisier env | grep FRAISIER

# Check the script exists and is executable
ls -la $FRAISIER_SYSTEMCTL_WRAPPER

# Regenerate and reinstall if needed
fraisier scaffold
sudo fraisier scaffold-install --yes
```

### Webhook not triggering deployments

```bash
# Check events were received
fraisier webhooks --limit 20

# If events show but no deployment started, check branch_mapping
fraisier validate

# Test manually
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -d '{"ref":"refs/heads/main","repository":{"full_name":"org/my-api"}}'
```

### Config sync regenerated scaffold unexpectedly

During deploy, if `fraises.yaml` changed in git, Fraisier automatically regenerates and
installs scaffold. If the regenerated files differ from what is on disk, systemd units may
be updated. To see what changed:

```bash
git diff HEAD~1 -- fraises.yaml
fraisier scaffold --dry-run
```

### Database migration errors

Migration errors include the migration filename, direction, database error, rollback status,
and recovery suggestions. Read the full error output from:

```bash
fraisier history --fraise my_api --limit 1
fraisier test-database my_api production
```

For a production migration that cannot be rolled back automatically:

```bash
# Check current migration state
fraisier db migrate my_api -e production -d down  # careful: rolls back one step
fraisier db-check
```

### Deployment lock stuck

If a deploy was interrupted, the lock file may be left behind:

```bash
# File-backend lock
ls -la /run/fraisier/

# Remove stale lock (only if no deploy is actually running)
sudo rm /run/fraisier/my_api.lock
```

---

## Upgrading Fraisier

```bash
pip install --upgrade fraisier

# Regenerate scaffold after upgrading (templates may have changed)
fraisier scaffold
git diff scripts/generated/
sudo fraisier scaffold-install --yes
```

---

## Configuration Reference

See the [CLI Reference](./cli-reference.md) for all commands and flags.

For the full `fraises.yaml` schema, all fields are documented inline in the config validator
at `fraisier/config.py`. The key top-level sections are:

| Section | Purpose |
|---|---|
| `name` | Project name; prefixes all generated service and file names |
| `git` | Git provider and webhook secret |
| `scaffold` | Infrastructure generation settings (output dir, deploy user, systemd/nginx defaults) |
| `deployment` | Lock backend, status file path, default timeout |
| `health` | Global health check defaults |
| `notifications` | Slack/Discord/webhook notifications on success/failure/rollback |
| `environments` | Server assignment per environment (for multi-server filtering) |
| `branch_mapping` | Maps git branches to fraise/environment pairs |
| `fraises` | Per-fraise config: type, environments, install, service, database, health_check, nginx |

---

**Last Updated**: 2026-04-01
