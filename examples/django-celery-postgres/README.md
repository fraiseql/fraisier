# Example: Django + Celery + PostgreSQL with Fraisier

This example deploys a Django REST API and a Celery worker to bare metal
servers using Fraisier's atomic deploy-and-migrate workflow.

## What's included

| File | Purpose |
|------|---------|
| `fraises.yaml` | Fraisier config: two fraises (api + worker), dev + prod environments |
| `myapp/` | Minimal Django app with a Task model and JSON API |
| `myapp/tasks.py` | Celery task that processes tasks asynchronously |
| `confiture.yaml` | Database migration config (PostgreSQL) |
| `.github/workflows/deploy.yml` | GitHub Action with push-then-poll pattern |

## Setup

### 1. Install Fraisier on the server

```bash
pip install fraisier
```

### 2. Initialize the project

```bash
fraisier init --template django
# or copy fraises.yaml from this example
```

### 3. Validate the configuration

```bash
fraisier validate
```

### 4. Preview a deployment

```bash
fraisier deploy api production --dry-run
```

### 5. Deploy

```bash
fraisier deploy api production
```

### 6. Ship a new version

```bash
fraisier ship patch   # bumps version, commits, pushes
```

## Deployment flow

When you push to `main`, the following happens automatically:

1. **Webhook** triggers on the server (or GitHub Action polls)
2. **Backup** — `pg_dump myapp_production` (pre-migration safety net)
3. **Migrate** — `confiture migrate up` (applies pending migrations)
4. **Restart** — `systemctl restart gunicorn-myapp.service`
5. **Health check** — `GET http://localhost:8000/health`
6. **Rollback** — if health check fails, restore from backup

The Celery worker (`celery-myapp.service`) is restarted separately
and does not require database migrations.

## Architecture

```
                    ┌─────────────┐
    git push ──────►│  Fraisier   │
                    │  Webhook    │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         ┌─────────┐ ┌─────────┐ ┌──────────┐
         │ Backup  │ │ Migrate │ │ Restart  │
         │ pg_dump │ │confiture│ │ systemd  │
         └─────────┘ └─────────┘ └──────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │Health Check │
                    │GET /health  │
                    └─────────────┘
```
