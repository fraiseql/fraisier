# Security

Fraisier's security model and hardening measures.

## Webhook Secret

The webhook server **requires** a secret to verify incoming requests. Without it, the server refuses to start.

### Requirements

- Set via `FRAISIER_WEBHOOK_SECRET` environment variable
- Minimum 32 characters
- Used for HMAC signature verification (GitHub, Gitea, Bitbucket) or token comparison (GitLab)

### Generating a Secret

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Configuration

```bash
export FRAISIER_WEBHOOK_SECRET="your-secret-here-at-least-32-characters"
fraisier-webhook
```

## Input Validation

### Shell Commands

Commands from `fraises.yaml` (e.g., `restore_command`, health check `command`) are validated before execution:

- **Rejected**: Shell metacharacters (`;`, `|`, `&`, `` ` ``, `$()`)
- **Parsed**: Using `shlex.split()` into a list of arguments
- **Executed**: Via `subprocess.run(list, ...)` with `shell=False`
- **Optional**: Binary allowlist (e.g., only `pg_restore` and `psql`)

This prevents command injection even if an attacker gains write access to the config file.

### Service Names

Systemd service names are validated against `^[a-zA-Z0-9_@.\-]+$` to prevent injection in `systemctl` commands.

### File Paths

- Paths are validated against `^[a-zA-Z0-9_./ -]+$`
- Path traversal (`..`) is detected and rejected
- When `base_dir` is specified, resolved paths must stay within it
- **Strict mode**: Rejects symlinks entirely (for backup paths)

### Docker CP Paths

- Must contain `:` separator
- Container path must be absolute (start with `/`)
- Path traversal (`..`) rejected

### Database Identifiers

PostgreSQL identifiers (schema names, table names) are validated against `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$`.

## Log Redaction

Sensitive values are automatically redacted in structured JSON logs. Any dict key containing these substrings has its value replaced with `***REDACTED***`:

- `password`, `secret`, `token`, `key`, `auth`, `credential`

Safe keys that would otherwise match (like `primary_key`, `foreign_key`, `sort_key`, `cache_key`) are explicitly excluded.

### Token providers (`smoke_tests.token_provider`)

Token providers acquire short-lived bearer credentials at deploy time. They share
the same trust envelope as `post_migrate`'s `psql -f` — the resolved value is
sensitive and the subprocess (for `exec`) runs as the deploy user. The
implementation guards against accidental leakage:

- **Resolved tokens never appear in logs** at any level (DEBUG included).
  Verified by `tests/test_token_providers.py::TestExecProvider::test_resolved_token_never_appears_in_logs`
  and the analogous OAuth2 cases.
- **`exec` subprocess argv** logs only `argv[0]` at INFO. Full argv (which may
  contain `--client-id` and similar non-token but operationally interesting
  args) is DEBUG-only.
- **`exec` subprocess is invoked with a list**, never `shell=True`.
- **`exec` subprocess `stderr` is not included in the raised
  `DeploymentError` message** — a wrapper with `set -x` enabled (or a
  helper that echoes its output to stderr) would otherwise have the token
  surface in the deploy journal via the outer `logger.exception(...)`.
  The stderr tail is emitted at DEBUG only; re-run with
  `FRAISIER_LOG_LEVEL=DEBUG` when triaging.
- **`format` placeholder validation.** A `format` string without
  `{token}` would silently drop the resolved value; a typo placeholder
  (`{access_token}`) would `KeyError` mid-deploy. Both shapes are
  rejected at config-load time.
- **Unknown keys in `token_provider:`** are rejected at config-load
  time. Operators who set the deferred `cwd` or `env_passthrough`
  options, or who typo a field name, learn at parse time rather than
  observing a silent no-op at deploy time.
- **OAuth2 `client_secret` and `refresh_token`** are redacted in the form-body
  log line at DEBUG. The token endpoint's error response body is never echoed in
  the raised `DeploymentError` — some IdPs include the client_secret in error
  envelopes.
- **Rotated OAuth2 refresh tokens** returned in the response are discarded.
  Fraisier does not write to your secrets store; rotation is the operator's
  responsibility.

## Rate Limiting

The webhook endpoint enforces rate limiting:
- 10 requests per minute per IP (configurable via `FRAISIER_WEBHOOK_RATE_LIMIT`)
- Maximum 256 tracked IPs (LRU eviction)

## User Separation

Fraisier supports separating the **deploy user** (runs deployments) from the **app user** (runs the application process). This follows the principle of least privilege: if the application is compromised, the attacker cannot drop databases, restart services, or modify deployed code.

### Two-user model

| User | Role | Privileges |
|------|------|-----------|
| `deploy_user` | Runs `fraisier deploy`, webhook, backups | CREATEDB (for rebuild strategy), sudoers for systemctl, git worktree write |
| `service.user` | Runs the application process | Connect to own DB, read app files |

### Configuration

```yaml
scaffold:
  deploy_user: myapp_deploy   # global deploy user

fraises:
  my_api:
    type: api
    environments:
      production:
        deploy_user: prod-deployer  # per-env override (optional)
        service:
          user: myapp               # app runs as myapp
```

When `service.user` differs from `deploy_user`, `fraisier setup` will:
1. Create both system accounts
2. Set `app_path` ownership to the app user
3. Add the deploy user to the app user's group for write access during deployment
4. Install sudoers for the deploy user's systemctl access

### When single-user is acceptable

On **development servers**, running both roles as the same user is acceptable (data is disposable, no external exposure). On **production**, use separate users. Production typically uses the `migrate` strategy which doesn't need CREATEDB.

### Database access

For strategies that need privileged database operations (rebuild, restore_migrate), configure `admin_url` to connect as a PostgreSQL superuser:

```yaml
database:
  strategy: rebuild
  admin_url: postgresql://postgres@/postgres?host=/var/run/postgresql
```

This avoids granting the deploy user OS-level sudo access to the postgres account.

## Host hardening

### No remote attach into a running interpreter (PEP 768)

Python 3.14 lets a debugger attach to a **running** interpreter and execute code
in it (`python -m pdb -p <pid>`, `sys.remote_exec`). The OS still gates it like
ptrace (same user or `CAP_SYS_PTRACE`, subject to Yama's `ptrace_scope`), but
anything already able to ptrace the webhook or a `confiture migrate up` could
inject Python into a process that holds database credentials.

fraisier opts out everywhere it starts Python:

- every unit it renders, root helpers and the app unit included, sets
  `Environment=PYTHON_DISABLE_REMOTE_DEBUG=1`. In the app unit it comes before
  `service.environment`, so a value there overrides it;
- importing fraisier sets it for the processes fraisier starts (`confiture`,
  `uv`) when it is not already set, which covers a `fraisier` run from a shell;
- `fraisier doctor` (`remote_debug_disabled`) warns about any fraisier unit on
  3.14 that would still accept an attach, drop-ins included.

The opt-out removes only this channel. ptrace itself, and tools that read a
process's memory through it (`py-spy dump`), are unaffected.

CPython disables the attach for **any** value of the variable, `0` and the empty
string included (measured on 3.14.5). Only an *unset* variable allows it, so
`Environment=PYTHON_DISABLE_REMOTE_DEBUG=` does not lift it.

### Lifting it to debug a hung deploy

The interpreter reads the variable when it starts, so lift it **before** the run
you want to attach to; a process that is already hung cannot be opened up. For a
deploy (its unit is `fraisier-<fraise>-<env>@.service`; `confiture migrate up`
runs inside it):

```bash
sudo systemctl edit fraisier-<fraise>-<env>@.service
# add:
#   [Service]
#   UnsetEnvironment=PYTHON_DISABLE_REMOTE_DEBUG
# reproduce, then attach to the process:
sudo python3.14 -m pdb -p <pid>
# when done, remove the drop-in:
sudo systemctl revert fraisier-<fraise>-<env>@.service
```

Processes the unit starts (a `confiture` or `uv` subprocess) keep the opt-out,
because fraisier sets it again for them. Until the drop-in is reverted,
`fraisier doctor` names it.

## Root helpers

fraisier runs four helpers as root: `systemctl-helper`, `scaffold-install-helper`,
`unit-installer` and `pgbackrest-helper`. Until
[#433](https://github.com/fraiseql/fraisier/issues/433) they ran from the deploy
user's uv tool dir, and the scaffold-install-helper ran a deploy-rendered
`install.sh`, so the deploy user was root-equivalent. Two people can act as the
deploy user here, and the model has to hold against both:

- anyone who can land a commit on an auto-deployed branch, with no host access,
  because each deploy installs that commit's `fraises.yaml` and re-renders the
  units;
- a compromised deploy-user account, the public webhook included.

### What root runs

The root helpers run a **second, root-owned copy of fraisier** under
`/usr/local/lib/fraisier-root`. They do not use `/opt/fraisier`, which the
deploy user owns. Each unit runs
`/usr/local/lib/fraisier-root/tools/fraisier/bin/python -I -m fraisier.<helper>`,
and `-I` keeps `PYTHONPATH`, the working directory and any user site out of what
it imports. uv installs the copy with every path pinned under that directory: the
tool venv, the managed Python, the cache, and uv itself, chowned to root after
its installer runs. It runs under `env -i`, so a `HOME` or `UV_*` that survived
`sudo` cannot decide where it writes.

Only an operator changes the root copy, with `sudo fraisier-root-upgrade VERSION`.
The webhook's self-upgrade changes only the deploy user's copy, so the two differ
after every self-upgrade. `fraisier doctor` reports that as
`root_helper_version_skew`. A root-owned `/usr/local/bin/fraisier` link makes
`sudo fraisier` run the root copy, and `root_fraisier_command` checks the link
and sudo's `secure_path`.

### What a deploy may install as root

A deploy still re-renders the scaffold as the deploy user. The
scaffold-install-helper now reads that render as **untrusted input** and runs
nothing from it. It applies only what the **root policy**
(`/etc/fraisier/<project>/root-policy.json`) allows. Only an operator's
`sudo fraisier scaffold-install` writes that file, and the helper refuses it
unless the file and every directory above it are root-owned and writable by
root alone. Nothing in `fraises.yaml`, `/opt/fraisier` or `/var/lib/fraisier`
can widen it.

- A deploy may rewrite only the units the policy lists, from the source the
  policy names for each. Each one must pass a **positive allowlist** of
  sections and directives, and any directive not on it refuses the unit:
  - `User=` and `Group=` must be non-root identities the policy lists. A
    service with no `User=` is refused.
  - Every `Exec*=` line names an executable under the policy's prefixes, with
    no `+`, `!`, `!!` or `|` prefix.
  - `EnvironmentFile=` and `LoadCredential=` may name only files the policy
    lists. `LogsDirectory=` and the other directories systemd creates and
    chowns may name only the policy's names. `StandardOutput=file:` and its
    relatives are refused.
  - `DynamicUser=`, `SupplementaryGroups=`, capabilities, `BindPaths=`,
    `RootDirectory=`, `PermissionsStartOnly=`, `LoadCredentialEncrypted=` and
    every directive the allowlist does not name are refused.
- Everything else root owns is the operator's: sudoers, every nginx file,
  sockets, users, directories and the root helpers themselves. If a deploy's
  render wants one of them changed, or names an artifact the policy has never
  seen, the helper writes nothing and reports it as pending. The deploy then
  stops.
- Nothing is written unless everything passes.
- The unit-installer, which copies an app's own `scripts/systemd` units, applies
  the same allowlist. It accepts only `.service` and `.timer` files, and writes
  the bytes it judged rather than reading the file a second time.

The policy is read off the render the operator approved: the identities,
executables, files and directories its units, and the app's own units, name.
It never grants an identity that resolves to uid or gid 0, or a read of
`/etc/shadow`, sudoers, `/etc/ssh` or `/root`. Config validation also refuses
`service.user`/`service.group` of `root`/`0` and `retain.user: root`. That
check is defence in depth: it runs from deploy-owned code, and the allowlist
is what holds.

### The operator's install

`sudo fraisier scaffold-install` renders `fraises.yaml` as root, into a private
directory. Before anything runs, it shows a diff of every root-owned file it
would write and of the root policy it would grant. `--yes` skips the question,
not the diff. Run as anyone but root, it refuses, because all it could do is
hand a script someone else wrote to `sudo`. The config it reads is still one a
commit author chose, so read the diff.

### What doctor checks

`fraisier doctor` (`root_unit_exec_trust`) reports every command that runs as
root from a path someone other than root can change. It reads the effective unit
through `systemctl show`, so drop-ins count. It follows symlinks, a script's `#!`
interpreter, the venv (a `bin/python -m` command included) and the base Python
named in its `pyvenv.cfg`, and every directory above them. It also counts a `+`
or `!` command in a unit that sets `User=`. Since #433's fix it **fails**. A host
fails until an operator has installed the root copy and re-run
`sudo fraisier scaffold-install`.

## What Fraisier Does NOT Protect Against

- **Host compromise**: If an attacker has shell access to the deployment server, fraisier cannot protect against them.
- **Network MitM**: Fraisier does not manage TLS. Use a reverse proxy (nginx, Caddy) with TLS termination.
- **Config file tampering**: If an attacker can write to `fraises.yaml`, command validation reduces but does not eliminate risk. Protect the config file with filesystem permissions.
- **Secrets in config**: Fraisier does not encrypt secrets at rest. Source secrets from environment variables instead of embedding them in `fraises.yaml`:
  - **`!envvar VAR_NAME`** (deferred, recommended): a custom YAML tag that parses into a placeholder; the `os.environ['VAR_NAME']` lookup is deferred to consumption time and re-reads on every access. Missing variables raise `ConfigurationError` *when the value is actually consumed*, naming the full YAML key path of the placeholder. Subcommands that do not enter a section (`--help`, `--version`, `ship --help`, etc.) do not require its env tags. Run `fraisier validate --resolve-envvars` to force-resolve every reference in one pass for a pre-deploy CI gate. Works anywhere in `fraises.yaml`, including `git.<provider>.webhook_secret`, `smoke_tests.headers`, and `database.database_url`.
  - **`${VAR_NAME}`** (runtime): scoped to the `notifications:` and `hooks:` blocks only. Expanded by the dispatcher at fire time. Do not use elsewhere — most consumers read the YAML value literally and will not expand it.
