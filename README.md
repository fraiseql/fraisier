# Fraisier

**Atomic deploy + migrate with automatic rollback.**

Deploy your PostgreSQL application to bare metal or Docker, with database
migrations that actually work. Preflight check, migrate up, restart, health
check — and `confiture migrate down` if anything fails.

```
preflight → migrate up → restart → health check → done
                │                        │
                │ failure                 │ failure
                ▼                        ▼
         (no changes)            migrate down → git rollback
```

---

## Why Fraisier?

Every deployment tool treats database migrations as an afterthought:

| Tool | Migration story |
|------|----------------|
| **Kamal** | Piggybacks on Rails entrypoint — no locking, no rollback |
| **Dokku** | Manual `dokku run <app> rake db:migrate` |
| **CI scripts** | Brittle `ssh && migrate && restart` — breaks at the worst moment |
| **Migration tools** | Run migrations with zero awareness of deployment state |

**Nobody coordinates preflight → migrate → deploy → health check → rollback as
a single atomic workflow.** Fraisier does.

### Who is this for?

Teams running PostgreSQL applications using [confiture](https://github.com/fraiseql/confiture)
for migrations on bare metal or Docker Compose. You've been burned by:

- A migration that locked a table during deploy
- A deploy that went live before the migration finished
- A failed migration with no way to roll back the schema
- A rollback that reverted the app but left the schema inconsistent

### When NOT to use Fraisier

- **Kubernetes**: Use Helm, ArgoCD, or Flux. Fraisier manages systemd services and Docker Compose, not pods.
- **Multiple databases**: Fraisier is PostgreSQL-only via confiture. MySQL, MongoDB, etc. are not supported.
- **Large fleets (10+ servers)**: Fraisier targets 1-3 servers. For larger fleets, use Ansible, Terraform, or a proper orchestrator.
- **No database migrations**: If your app doesn't have a database or doesn't use confiture, fraisier's main value proposition doesn't apply.

### Compared to

| Tool | Strength | Fraisier's difference |
|------|----------|----------------------|
| **Kamal** | Zero-config Docker deploy | No migration awareness, no atomic rollback |
| **Dokku** | Heroku-like git push | Migrations are manual, no preflight checks |
| **Coolify** | Web UI, broad language support | Fraisier is CLI-first, PostgreSQL-deep |
| **Ansible** | Infrastructure automation | Fraisier is deploy-only, not infra management |

---

## Quickstart

### 1. Install

```bash
pip install fraisier
# or
uv add fraisier
```

### 2. Initialize

```bash
fraisier init
```

This creates a `fraises.yaml` with sensible defaults.

### 3. Preview

```bash
fraisier deploy my_api production --dry-run
```

```
╭──────── DRY RUN ────────╮
│  Target      my_api -> production
│  Strategy    migrate
│  Preflight   check reversibility + duplicates
│  Migration   confiture migrate up
│  Restart     gunicorn-myapi.service
│  Health      http://localhost:8000/health (timeout: 30s)
│  Rollback    confiture migrate down (if health check fails)
╰─────────────────────────╯
```

### 4. Deploy

```bash
fraisier deploy my_api production
```

### 5. Ship (bump + commit + push + deploy)

```bash
fraisier ship patch              # 1.0.0 -> 1.0.1, commit, push, deploy
fraisier ship patch --no-deploy  # Skip deploy after push
```

---

## How It Works

### Deployment strategies

Three database-aware strategies, configured per environment:

| Strategy | Environment | What it does |
|----------|-------------|-------------|
| `rebuild` | development | Drop DB, rebuild schema from scratch via `confiture migrate rebuild` |
| `restore_migrate` | staging | Restore production backup, then `confiture migrate up` |
| `migrate` | production | Preflight → `confiture migrate up` → restart → health check. Rollback via `confiture migrate down` on failure |

### Rollback

When a health check fails after migration, Fraisier:

1. Calls `confiture migrate down --steps=N` to reverse exactly the migrations applied
2. Checks out the previous git commit
3. Restarts the service

Use `--no-rollback` to deploy irreversible migrations (those without down files).

---

## Configuration

Fraisier reads `fraises.yaml`. A *fraise* is a deployable service (API, worker, ETL, scheduled job).

```yaml
fraises:
  my_api:
    type: api
    environments:
      production:
        branch: main
        app_path: /var/www/my-api
        systemd_service: gunicorn-myapi.service
        database:
          name: myapp_production
          strategy: migrate
          confiture_config: confiture.yaml
        health_check:
          url: http://localhost:8000/health
          timeout: 30

branch_mapping:
  main:
    fraise: my_api
    environment: production
```

---

## CLI Reference

### Core

```
fraisier init                                    Scaffold fraises.yaml
fraisier deploy <fraise> <env> [--dry-run]       Deploy a fraise
fraisier deploy <fraise> <env> --no-rollback     Allow irreversible migrations
fraisier ship patch|minor|major [--dry-run]      Bump, commit, push, deploy
fraisier ship patch --no-deploy                  Ship without deploying
fraisier list [--flat]                           List all fraises
fraisier status <fraise> <env>                   Check fraise status
fraisier rollback <fraise> <env>                 Roll back to previous version
fraisier health [--json]                         Check all service health
```

### Database

```
fraisier db migrate <fraise> -e <env>            Run database migrations
fraisier db reset <fraise> -e <env>              Reset from template (dev)
fraisier backup <fraise> -e <env>                Database backup
```

### Infrastructure

```
fraisier scaffold [--dry-run]                    Generate systemd, nginx, CI files
fraisier providers                               List deployment providers
fraisier provider-test <type>                    Run provider pre-flight checks
```

### Versioning

```
fraisier version show                            Show version.json
fraisier version bump patch|minor|major          Bump version atomically
```

---

## Deployment Providers

| Provider | Description |
|----------|------------|
| `bare_metal` | SSH + systemd — the default for VPS deployments |
| `docker_compose` | Docker Compose stacks with container exec for migrations |

---

## Git Providers

Auto-detected from webhook headers. Supports per-fraise overrides.

| Provider | Self-hosted |
|----------|-------------|
| GitHub / GitHub Enterprise | Yes |
| GitLab / self-hosted GitLab | Yes |
| Gitea / Forgejo | Yes |
| Bitbucket Cloud / Server | Yes |

---

## Webhook Server

Event-driven deploys triggered by git push:

```bash
fraisier-webhook    # starts on port 8080
```

Configure your Git server to send push events to `https://your-server/webhook`.
The webhook auto-detects the Git provider from request headers.

---

## Part of the FraiseQL Ecosystem

| Tool | Purpose |
|------|---------|
| **confiture** | PostgreSQL schema migrations |
| **pgGit** | Database version control |
| **fraiseql** | Compiled GraphQL engine (Rust runtime) |
| **pg_tviews** | Incremental materialized views |

---

## License

MIT
