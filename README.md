# Fraisier

**Socket-activated deployment + migration orchestration for Python applications.**

Deploy Django, FastAPI, Flask, or any Python web app with database migrations that work reliably. Uses systemd (Linux) or rc.d (FreeBSD) service management with socket activation for secure, web-triggered deployments. Supports Django migrations, Alembic, Peewee, and Confiture. Coordinates preflight → migrate → restart → health check → rollback as one atomic operation.

```
webhook → socket → daemon → preflight → migrate up → restart → health check → done
          │          │          │          │            │          │            
          │ failure  │ failure  │ failure  │ failure    │ failure  │ failure
          ▼          ▼          ▼          ▼            ▼          ▼
    (no changes)  (no changes)  (no changes)  migrate down → git rollback
```

Works with PostgreSQL databases. Deploy to bare metal (Linux or FreeBSD) or Docker Compose.

---

## Why Fraisier?

Deployment tools treat database migrations as an afterthought. Fraisier makes them first-class citizens.

### The problem

Most deployment tools get migrations wrong:

| Tool | Migration story |
|------|----------------|
| **Kamal** | Rails migrations happen in the entrypoint — no coordination with deployment |
| **Dokku** | Manual `dokku run <app> python manage.py migrate` after deploy |
| **CI scripts** | Brittle `ssh && migrate && restart` that fails spectacularly |
| **Migration tools** | Run migrations with zero awareness of app deployment state |

### Fraisier's approach

**Atomic coordination**: Preflight checks → framework-specific migrations → service restart → health validation → automatic rollback on failure.

**Post-migration verification**: idempotent SQL hooks (`database.post_migrate`) and authenticated `smoke_tests` probes wrap the migrate/restart with a verification layer. See [Post-migration verification](docs/deployment-guide.md#post-migration-verification) in the deployment guide.

**Multi-framework support**: Works with Django, Alembic, Peewee, and Confiture.

### Who this is for

Python developers deploying web applications who want:
- Reliable database migrations during deployment
- Support for major Python frameworks (Django, FastAPI, Flask)
- Automatic rollback when things go wrong
- Clean coordination between app and database
- Production-grade deployment workflows

### When to look elsewhere

Fraisier is great for Python web apps with PostgreSQL, but here are better tools for other scenarios:

- **Kubernetes**: Use Helm, ArgoCD, or Flux for container orchestration
- **Large fleets (10+ servers)**: Use Ansible, Terraform, or Pulumi for infrastructure management
- **Non-PostgreSQL databases**: Only supports PostgreSQL
- **Non-Python apps**: Designed for Python frameworks
- **Serverless**: Use Vercel, Netlify, or cloud-specific deployment tools
- **Complex multi-service apps**: Consider Docker Swarm or Kubernetes for service meshes

### Framework support

Fraisier supports major Python migration frameworks:

| Framework | Configuration | Use case |
|-----------|---------------|----------|
| **Django** | `framework: django` | Django projects with `manage.py migrate` |
| **Alembic** | `framework: alembic` | SQLAlchemy projects with Alembic |
| **Flask-Migrate** | `framework: flask_migrate` | Flask + SQLAlchemy projects |
| **Peewee** | `framework: peewee` | Peewee ORM projects |
| **Confiture** | `framework: confiture` | FraiseQL or custom PostgreSQL schemas |

---

## Quickstart

### 1. Install

```bash
pip install fraisier
# or
uv add fraisier
```

### 2. Configure

Create `fraises.yaml`:

```yaml
fraises:
  my_app:
    type: api
    environments:
      production:
        app_path: /var/www/myapp
        systemd_service: myapp.service
        database:
          framework: django  # or alembic, peewee, confiture
          name: myapp_prod
        health_check:
          url: http://localhost:8000/health
```

### 3. Provision the server (first time only)

```bash
fraisier bootstrap --environment production
```

Connects as root via SSH and runs all setup steps: creates the deploy user, installs `uv`
and `fraisier`, uploads config and scaffold files, enables the deploy socket.

### 4. Deploy

```bash
fraisier trigger-deploy my_app production
```

Fraisier handles: git pull → migrate → restart → health check → rollback on failure.

### 5. Ship new versions

```bash
fraisier ship patch    # Bump version, commit, push, deploy
```

---

## How It Works

### The deployment flow

1. **Git**: Pull latest code to deployment directory
2. **Database**: Run migrations with framework-specific commands
3. **Service**: Restart systemd service or Docker containers
4. **Health**: Verify application is responding
5. **Rollback**: If anything fails, rollback migrations and git

### Framework integration

Fraisier calls the appropriate migration commands for each framework:

- **Django**: `python manage.py migrate`
- **Alembic**: `alembic upgrade head`
- **Peewee**: Custom Peewee migration runner
- **Confiture**: `confiture migrate up`

### Rollback coordination

When health checks fail, Fraisier:
1. Rolls back database migrations (framework-specific down commands)
2. Reverts git to previous commit
3. Restarts services

---

## Configuration

Fraisier uses `fraises.yaml` for configuration. A *fraise* is a deployable application component.

### Django example

```yaml
fraises:
  myapp:
    type: api
    environments:
      production:
        app_path: /var/www/myapp
        systemd_service: myapp.service
        database:
          framework: django
          name: myapp_prod
          django:
            settings_module: myapp.settings
        health_check:
          url: http://localhost:8000/health
```

### FastAPI + Alembic example

```yaml
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /opt/api
        systemd_service: api.service
        database:
          framework: alembic
          name: api_prod
          alembic:
            script_location: migrations
            ini_path: alembic.ini
        health_check:
          url: http://localhost:8000/health
```

### Flask + Peewee example

```yaml
fraises:
  web:
    type: api
    environments:
      production:
        app_path: /var/www/web
        systemd_service: web.service
        database:
          framework: peewee
          name: web_prod
          peewee:
            models_module: app.models
        health_check:
          url: http://localhost:8000/health
```

### FreeBSD

For FreeBSD deployments, set the service manager explicitly or let Fraisier auto-detect it:

```yaml
service_manager: rc
```

When set to `rc`, scaffold generates rc.d service scripts instead of systemd units.

### Config reload

The running webhook auto-detects changes to `fraises.yaml`: each deploy syncs
the committed config to the server and the next `get_config()` re-reads it when
the file's mtime moves — no `systemctl restart` needed. Staleness is bounded to
a single deploy (a brand-new `install.command` shipped in commit *N* takes
effect on deploy *N+1*, since the config that drives deploy *N* is read before
*N* is pulled).

To force an immediate refresh without waiting for the next deploy:

```bash
systemctl reload fraisier-<project>-webhook   # sends SIGHUP → reloads config
```

The generated webhook unit wires this via `ExecReload=/bin/kill -HUP $MAINPID`.
A config that fails to parse is ignored: the webhook keeps serving the last-good
config and logs a warning, rather than failing every request.

### Install command

When `install.user` differs from the deploy user, the install step runs through
a per-fraise **install-helper** unit whose allowlist is the *exact*
`install.command`, baked into the unit at scaffold time (this is the
deploy-user → install-user security boundary). A deploy re-bakes that allowlist
automatically when the command changes, but you avoid re-bakes entirely — and
keep install *content* in code review — by making the allowlisted command a
**stable entrypoint** and putting the real steps in a repo-owned script:

```yaml
install:
  command: [bash, scripts/deploy-install.sh]   # stable — the allowlisted command
  user: appuser
```

```bash
#!/usr/bin/env bash
# scripts/deploy-install.sh — edit freely; the allowlisted command never changes
set -euo pipefail
uv sync --frozen
# add build steps here (uv run …, npm ci, cargo build, …)
```

The install-helper runs under `ProtectSystem=strict`; the caches/state of the
common toolchains (uv, pip, cargo, npm) are relocated under `app_path` via
`XDG_CACHE_HOME`/`XDG_DATA_HOME`/`XDG_STATE_HOME`/`UV_CACHE_DIR`/`CARGO_HOME`/
`npm_config_cache`. `HOME` stays the install user's, read-only under the
sandbox — so a tool that writes elsewhere in `$HOME` (e.g. `rustup` →
`~/.rustup`) needs its location exported to a path under `app_path` inside the
script (e.g. `export RUSTUP_HOME="$PWD/.rustup"`).

---

## Commands

### Core deployment

```
fraisier init                                    Create fraises.yaml config
fraisier trigger-deploy <fraise> <env> [--dry-run]  Deploy application
fraisier trigger-deploy <fraise> <env> --force       Force deployment
fraisier deployment-status <fraise>                  Show deployment status
fraisier rollback <fraise> <env>                 Rollback to previous version
fraisier list [--flat]                           List configured applications
fraisier health [--json]                         Check all health endpoints
```

### Version management

```
fraisier ship patch|minor|major [--dry-run]      Bump version, commit, push, deploy
fraisier ship patch --no-deploy                  Ship without deploying
fraisier version show                            Show current version
fraisier version bump patch|minor|major          Bump version number
```

### Database operations

```
fraisier db migrate <fraise> -e <env>            Run migrations only
fraisier db restore <fraise> -e <env>            Restore from backup (drains connections, force-drops on PG 13+)
fraisier db receipt <fraise> <env>               When did a restore last rewrite this database? (0 fresh, 1 stale, 3 unknown)
fraisier db reset <fraise> -e <env>              Reset database (development)
fraisier backup <fraise> -e <env>                Create database backup
fraisier backup prune --env <env>                Prune a corpus this host receives
```

### Infrastructure

```
fraisier bootstrap -e <env> [--dry-run]          Provision a fresh server end-to-end via SSH
fraisier scaffold [--dry-run]                    Generate systemd, nginx, CI files
fraisier scaffold-install [--dry-run] [--yes]    Install generated files to the system
fraisier providers                               List supported providers
fraisier provider-test <type>                    Test provider connectivity
```

---

## Branch namespace

fraisier owns the `fraisier/**` git branch namespace. Branches under
this prefix (currently `fraisier/sync/*`, more in future releases) may
be created, updated, deleted, or force-pushed by fraisier without
warning. **Do not push hand-authored work to `fraisier/**`** — it will
be reclaimed on the next fraisier run.

The flat `sync/*` namespace used by `fraisier sync` prior to 0.32 is no
longer touched. Merge or close any in-flight pre-0.32 sync PRs before
upgrading; the new sync run will not discover or update them.

---

## Deployment targets

Fraisier supports different deployment environments:

| Target | Description |
|--------|-------------|
| **Bare metal (Linux)** | systemd service management |
| **Bare metal (FreeBSD)** | rc.d service management |
| **Docker Compose** | Containerized deployments with docker-compose |

---

## Requirements

- **Python**: 3.11+
- **PostgreSQL**: Database server (local or remote)
- **Git**: Version control
- **Systemd** (Linux) or **rc.d** (FreeBSD): Service management (bare metal deployments)

### Framework-specific requirements

- **Django**: Django installed, `manage.py` with migrate command
- **Alembic**: alembic package, `alembic.ini` configuration
- **Peewee**: peewee package, migration files
- **Confiture**: confiture package, schema configuration

---

## Contributing

Fraisier welcomes contributions! Areas needing help:

- **New framework support**: Add migration strategies for Tortoise, PonyORM, etc.
- **Provider plugins**: Cloud platforms, container orchestrators
- **Documentation**: Tutorials, examples, troubleshooting guides
- **Testing**: Integration tests, CI improvements

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

---

## License

MIT - see [LICENSE](LICENSE) file
