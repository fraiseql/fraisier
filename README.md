# Fraisier

**Socket-activated deployment + migration orchestration for Python applications.**

Deploy Django, FastAPI, Flask, or any Python web app with database migrations that work reliably. Uses systemd socket activation for secure, web-triggered deployments. Supports Django migrations, Alembic, Peewee, and Confiture. Coordinates preflight → migrate → restart → health check → rollback as one atomic operation.

```
webhook → socket → daemon → preflight → migrate up → restart → health check → done
          │          │          │          │            │          │            
          │ failure  │ failure  │ failure  │ failure    │ failure  │ failure
          ▼          ▼          ▼          ▼            ▼          ▼
    (no changes)  (no changes)  (no changes)  migrate down → git rollback
```

Works with PostgreSQL databases. Deploy to bare metal or Docker Compose.

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
fraisier db reset <fraise> -e <env>              Reset database (development)
fraisier backup <fraise> -e <env>                Create database backup
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

## Deployment targets

Fraisier supports different deployment environments:

| Target | Description |
|--------|-------------|
| **Bare metal** | Direct systemd service management on Linux servers |
| **Docker Compose** | Containerized deployments with docker-compose |

---

## Requirements

- **Python**: 3.8+
- **PostgreSQL**: Database server (local or remote)
- **Git**: Version control
- **Systemd**: Service management (bare metal deployments)

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
