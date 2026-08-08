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

Use the `!envvar VAR_NAME` YAML tag in `fraises.yaml`. The tag parses into a placeholder
whose `os.environ['VAR_NAME']` lookup is deferred to consumption time; subcommands that do not
enter the relevant section run without the env tag set. Missing variables raise
`ConfigurationError` at the consumer boundary, naming the full YAML key path of the placeholder.
For a pre-deploy CI gate that wants every variable materialized in one pass, run
`fraisier validate --resolve-envvars`. Never commit actual secrets. A typical environment file at
`/etc/myapp/prod.env`:

```bash
FRAISIER_WEBHOOK_SECRET=<min 32 chars, random>
DATABASE_URL=postgresql://myapp:pass@localhost/myapp_production
DATABASE_ADMIN_URL=postgresql://postgres@/postgres?host=/var/run/postgresql
```

`${VAR}` shell-style substitution is supported **only** inside the `notifications:` and
`hooks:` blocks (expanded at fire time by their dispatchers). Use `!envvar` for everything
else — most consumers read the YAML value literally and will not expand `${VAR}`.

### Bytecode and startup time

If your `install.command` is `uv sync`, add `--compile-bytecode`:

```yaml
install:
  command: [uv, sync, --frozen, --compile-bytecode]
  user: myapp
```

**Why.** `uv sync` does not byte-compile by default, and since v0.50.1 every
generated app unit sets `Environment=PYTHONDONTWRITEBYTECODE=1` — so without the
flag the venv holds no `.pyc` and nothing ever writes one. Every service start
then recompiles the whole imported dependency tree from source.

Measured on a FastAPI + SQLAlchemy + psycopg app (49 MB site-packages, plus a
612 KB app package), median of 11 runs:

| install | app unit | median start |
|---|---|---|
| `uv sync` | `PYTHONDONTWRITEBYTECODE=1` | **1005 ms** |
| `uv sync --compile-bytecode` | `PYTHONDONTWRITEBYTECODE=1` | **612 ms** |
| both compiled (no env var) | — | 565 ms |

`--compile-bytecode` recovers ~88% of the difference for **+151 ms** on a fresh
`uv sync`, +62 ms on a no-op re-sync, and +19 MB of disk. The residual ~47 ms is
your own app package, which `uv` does not compile because an editable project's
source lives outside the venv. The cost scales with source volume, not package
count — on this hardware roughly 13 MB of source per second.

**The two settings compose; they do not conflict.** `PYTHONDONTWRITEBYTECODE`
disables bytecode *writing* only — a cache already on disk is still read. So the
`.pyc` are written once, at install time, by the **install user**, and the app
process never writes any. That is strictly safer than the pre-v0.50.1 behaviour:
the third-identity `__pycache__` that blocked `uv sync --frozen` can no longer
appear at all. Those install-user-owned `.pyc` are also excluded from the
stale-cache sweep, which only removes caches owned by someone *other* than the
venv's owner.

`fraisier doctor` warns when a `uv sync` install command is missing the flag —
see [`install_compile_bytecode`](doctor.md#check-catalog). It is advisory: this
is startup latency, not correctness.

**When not to bother.** A short-lived or rarely-restarted service, or a small
dependency tree, will not notice 400 ms. Measure your own startup before
changing anything — the numbers above are one app on one machine, and the ratios
transfer better than the milliseconds.

---

## Token providers for authenticated smoke tests

`smoke_tests` (introduced in #204) probes the freshly-deployed service with bearer
credentials after `/health` passes. The v0.21 shape sources the token from
`os.environ` via `!envvar`:

```yaml
smoke_tests:
  - name: authenticated_me
    url: /graphql
    headers:
      Authorization: !envvar SMOKE_TEST_JWT
```

This works when the operator can hold a long-lived JWT in a secrets manager and
export it to the deploy user. It does not work for IdPs that hand out short-lived
tokens that must be acquired at deploy time.

`token_provider:` (introduced in #215, v0.22.0) declares how to acquire the token.
**Absence of the block keeps the v0.21 behavior** — static headers flow through
unchanged. Each provider instance resolves at most once per deploy: smoke tests
that share the same `token_provider:` mapping via a YAML anchor (`&p` / `*p`)
get the same token. Smoke tests that duplicate the block in plain YAML each get
their own resolution call — use anchors when sharing matters. Provider failure
(script crash, IdP 401, network error) raises `DeploymentError` and aborts the
deploy with `status=failed`. **No rollback** is attempted on a token-provider
failure: by this point migrations have run and the service has restarted on the
new code, but a transient IdP issue is not a code regression — rolling back the
whole deploy on every IdP hiccup is the wrong default. Operators investigate
and re-deploy.

### `exec` — run a script

The most common shape: a shell script that knows how to mint a token (vault CLI,
federated assume-role wrapper, internal IdP helper) and prints it on stdout.

```yaml
smoke_tests:
  - name: vault_authenticated
    url: /graphql
    token_provider:
      type: exec
      command: ["/opt/myapp/bin/get-deploy-token.sh"]
      timeout: 10               # seconds; default 10
      # header: Authorization   # default; case-insensitive
      # format: "Bearer {token}" # default
    assert:
      - { json_path: $.data.me.id, not_null: true }
```

The subprocess runs as the deploy user. `argv[0]` is logged at INFO; full argv is
logged only at DEBUG. The resolved token never appears in any log line at any
level. Non-zero exit or timeout raises `DeploymentError` with the exit code and a
truncated stderr tail. `cwd` and `env_passthrough` are deferred — the subprocess
inherits the deploy user's environment, same as the `post_migrate` hook.

### `oauth2_client_credentials`

OIDC machine-to-machine grant. Posts `grant_type=client_credentials` to
`token_url`:

```yaml
smoke_tests:
  - name: oidc_machine
    url: /graphql
    token_provider:
      type: oauth2_client_credentials
      token_url: https://idp.example.com/oauth/token
      client_id: !envvar SMOKE_CLIENT_ID
      client_secret: !envvar SMOKE_CLIENT_SECRET
      audience: https://api.myapp.io  # optional
      scope: "read:me"                # optional
      timeout: 10                     # optional; default 10
```

The `client_secret` is redacted in all log lines (the request URL and grant type
are logged at INFO; a redacted form-body shape at DEBUG). Non-2xx, missing
`access_token`, or network errors raise `DeploymentError`. The token endpoint's
response body is **not** echoed in the error message — some IdPs include the
client_secret in their error envelopes.

### `oauth2_refresh_token`

OIDC refresh-grant. Posts `grant_type=refresh_token`:

```yaml
smoke_tests:
  - name: oidc_refresh
    url: /graphql
    token_provider:
      type: oauth2_refresh_token
      token_url: https://idp.example.com/oauth/token
      client_id: !envvar SMOKE_CLIENT_ID
      refresh_token: !envvar SMOKE_REFRESH_TOKEN
```

**Rotated refresh tokens are discarded.** If the IdP returns a fresh
`refresh_token` in its response, fraisier ignores it. Persisting rotated refresh
tokens is out of scope — the operator owns rotation (e.g. a separate scheduled
rotator that updates the deploy user's secrets file). Fraisier will not write to
your secrets store.

### Header collisions

A smoke test that declares both `headers.<X>` and a `token_provider` whose
`header` is `<X>` is rejected at config-load time with `ConfigurationError`.
Comparison is case-insensitive per RFC 7230 (`Authorization` and `authorization`
collide). Either drop the static header, or change the provider's target with
`token_provider.header:`.

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

## Post-migration verification

Fraisier ships two complementary hooks that run after `confiture migrate` and
before the deployment is reported successful:

| Hook | Runs | Reads | Failure policy |
|---|---|---|---|
| `database.post_migrate` | after migrate, before service restart | DB only (no app) | `on_error: halt | warn` |
| `smoke_tests` | after service restart and `/health` passes | the live HTTP app | `on_failure: rollback | halt | warn` |

The two solve different problems. `post_migrate` is for **schema-side**
side effects that the migration tool itself doesn't cover (grant
reconciliation, materialized view refreshes, vendor extension setup).
`smoke_tests` is for **application-side** behavior that an
unauthenticated `/health` probe can't reach (authenticated queries,
permission boundaries, cross-service contracts).

### `database.post_migrate`: SQL hooks after migrate

A list of steps; each step runs exactly one of `sql_dir` (every `.sql`
file in the directory, sorted) or `sql_file` (a single file) against the
fraise's `database_url`. Both modes are designed to be idempotent so a
re-deploy after a partial failure is safe.

```yaml
database:
  database_url: !envvar DATABASE_URL
  strategy: apply
  post_migrate:
    - sql_dir: db/7_grant/   # idempotent grant sweep — see below
      on_error: halt          # default: abort before restart
    - sql_file: db/post_migrate.sql
      on_error: warn          # log and continue
```

**Failure policy**:

- `halt` (default) — raises `DeploymentError` and aborts the deploy *before*
  the service is restarted. The new code never serves traffic, so no
  rollback is needed: the old service keeps running on the old commit.
- `warn` — logs the failure and continues. Use sparingly; this is
  appropriate for housekeeping (e.g. refresh a stale materialized view)
  but never for grants the app is about to require.

The canonical use case is **grant reconciliation**: when a confiture
migration creates a new relation, the app role's grants aren't
automatically extended. A small idempotent SQL file under `db/7_grant/`
keeps the app role's privileges in sync with whatever the migration
introduced.

### `smoke_tests`: authenticated probes after restart

A list of HTTP probes that run against the freshly-deployed service,
authenticated either via a static `!envvar` header or via a
`token_provider` block (see [Token providers for authenticated smoke
tests](#token-providers-for-authenticated-smoke-tests) earlier in this
guide for the provider catalog).

**Failure policy** — `on_failure:` on each probe:

- `rollback` (default) — invoke `ApiDeployer.rollback()`, which checks
  out the previous SHA, reverses migrations if reversible, and restarts
  the service. The deploy returns `status=rolled_back`.
- `halt` — leave the broken-but-unhealthy revision live. The deploy
  returns `status=failed`. Use when rollback would itself cause data
  loss (e.g. an irreversible migration just ran) — the operator
  investigates while the broken code keeps serving.
- `warn` — log the failure and let the deploy succeed. Use only for
  flaky probes whose failures are tolerable.

A token-provider failure (script crash, IdP 401, network error) is
distinct from a smoke-test failure: it raises `DeploymentError` and
**halts without rolling back**, since a transient IdP hiccup is not a
code regression.

### Worked example: post-migrate grants + authenticated smoke

The combined shape — verbatim from `fraises.example.yaml`:

```yaml
environments:
  production:
    database:
      database_url: !envvar DATABASE_URL
      strategy: apply
      backup_before_deploy: true
      pre_migrate_dump:          # enforced gate: dump+verify BEFORE migrating,
        enabled: true            # abort the deploy if the dump fails
        output_dir: /var/backups/myapp/pre_migrate
        min_free_gb: 20
        retention_hours: 168
      post_migrate:
        - sql_dir: db/7_grant/
          on_error: halt
    health_check:
      url: https://api.myapp.io/health
      timeout: 30
    smoke_tests:
      - name: authenticated_me
        method: POST
        url: /graphql
        headers:
          Authorization: !envvar SMOKE_TEST_JWT
        body: '{"query":"{ me { id role } }"}'
        on_failure: rollback
        assert:
          - { json_path: $.data.me.id, not_null: true }
          - { json_path: $.data.me.role, equals: admin }
          - { json_path: $.errors, null: true }
```

This wires the full chain: migrate → post_migrate grants → restart →
`/health` poll → authenticated GraphQL probe. Any step's failure
triggers its declared policy.

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

Then on each server, run scaffold and setup:

```bash
# On staging.myserver.com
fraisier scaffold
sudo fraisier scaffold-install --yes
sudo fraisier setup

# On prod.myserver.com
fraisier scaffold
sudo fraisier scaffold-install --yes
sudo fraisier setup
```

`--server` is optional and narrows the render to one logical server. It is
not needed for correctness: the generated tree is valid for every machine,
because the webhook unit is addressed by host in its filename and the
installer picks this machine's.

Naming an unknown server is an error, not a narrower render. A typo'd
`--server` used to produce a unit with the fraisier state directories and no
application paths — installable, and then broken on every deploy.

### How the webhook unit reaches the right host

The webhook service runs `ProtectSystem=strict`: the entire filesystem is
read-only inside its sandbox except the paths listed in `ReadWritePaths=`.
Every bare repo and application directory a host deploys must appear there,
or `git fetch` fails with `Read-only file system` (exit 255) even though the
same path is writable from a login shell.

The rule, in one line:

> When any environment declares a `server:`, the scaffold tree contains
> **only** `fraisier-{project}-webhook-{slug}.service` files — one per logical
> server — and `install.sh` copies the one matching `hostname -s` to
> `/etc/systemd/system/fraisier-{project}-webhook.service`. When no
> environment declares a `server:`, the tree contains the single unslugged
> file.

Consequences worth knowing:

- **The installed unit's name never changes.** Only the source filename in
  the scaffold tree carries the host. Nothing is renamed, enabled or disabled
  on the machine.
- **There is no fallback.** A leftover `fraisier-{project}-webhook.service`
  in a multi-server tree is never installed, and a machine whose slugged unit
  is missing is a hard error rather than a skipped step.
- **Every environment must declare a `server:`** once any of them does. An
  environment with no server belongs to no machine, so its trees reach no
  unit's allowlist; the render refuses rather than dropping it silently.

To check a host before installing:

```bash
fraisier doctor --check webhook_hosted_trees_writable   # reads the installed unit
sudo fraisier doctor --probe-sandbox                    # actually writes, under strict
```

The `--server` filter (when you do use it) ensures that:
- Systemd units are only generated for environments assigned to that server
- The webhook service's `ReadWritePaths` only includes that server's paths
- Sudoers entries only reference local service names

### Carrying a custom webhook unit

If you maintain your own `fraisier-{project}-webhook.service` — a drop-in
under `/etc/systemd/system/fraisier-{project}-webhook.service.d/`, or a
hand-edited unit — you own the `ReadWritePaths=` list, and no template fix
reaches it. Keep every `git_repo` and `app_path` of every environment that
host deploys in the list, plus any `database.pre_migrate_dump.output_dir`.
`fraisier doctor` reads the *installed* unit precisely so it can still tell
you when one is missing.

Operators who added a `prod-paths.conf` drop-in as a workaround for a webhook
unit carrying the wrong host's paths can remove it after upgrading and
re-running `fraisier scaffold && sudo fraisier scaffold-install --yes`.
Leaving it in place is harmless — a drop-in's `ReadWritePaths=` adds to the
unit's list rather than replacing it.

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

### Retention for a corpus you receive

A host that is rsync'd backups from somewhere else has no fraise producing
them, so nothing on that host knows the corpus exists — and nothing prunes
it. Declare the policy under `backup.environments.<env>.retain`, keyed by the
**receiving** environment:

```yaml
backup:
  environments:
    development:                    # the host that receives the corpus
      retain:
        - dir: /backup/production
          match: "*_full_*.dump"    # default "*.dump"
          retention_days: 3
          keep_minimum: 3           # default 3
          schedule: "*-*-* 05:30:00 UTC"
          name: production-full     # default: the dir basename
          user: fraisier            # default: scaffold.deploy_user
```

Each entry renders a `.service` and `.timer` pair that `scaffold-install`
installs **and enables**. `fraisier scaffold-diff` reports a missing one and
`fraisier doctor` reports a corpus nothing is pruning — which is the part
that was missing when a destination host filled its disk: the unit meant to
prune it was hand-written in the consuming repo and checked by nobody.

Run it by hand on the destination:

```bash
fraisier backup prune --env development
fraisier backup prune --env development --name production-full --dry-run
fraisier backup prune --env development --json
```

**Deletion runs on the destination, and only there.** Nothing gives the
producing side a way to expire this host's copies, and no `rsync --delete*`
flag appears anywhere fraisier renders: a compromised sender key cannot erase
the corpus it pushed.

#### `keep_minimum` and the stalled-producer warning

`keep_minimum` exempts the newest N dumps from the age rule *entirely*,
before the cutoff is applied. Without it, a producer that stops producing
ages its whole corpus out in one run — every dump is past the window on the
same night, and the destination is left with nothing.

When the floor is the only reason anything survived, the prune says so:

```
WARNING: every backup in /backup/production is past its retention window;
only keep_minimum=3 is holding the corpus open. The newest is 96h old
(prod_full_20260804.dump). Nothing recent has arrived — check the producer.
```

It exits 0 deliberately. A non-zero here would put the timer in `failed` and
stop the pruning that is still working, on top of a producer that is already
broken.

Two things this does **not** do:

- **It counts, it does not validate.** A partially transferred dump is the
  *newest* by mtime, so it is the first thing the floor protects. Verifying
  what arrives is a separate concern.
- **It does not watch the disk.** `keep_minimum` bounds how much a stalled
  producer can delete, not how much a healthy one can write.

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
