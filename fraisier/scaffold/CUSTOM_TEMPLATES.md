# Custom Templates

The `scaffold.template_dir` setting lets you override any built-in scaffold
template by placing a file with the same path in a custom directory.

## Configuring template_dir

Add the setting to the `scaffold:` section of `fraises.yaml`:

```yaml
scaffold:
  output_dir: scripts/generated
  template_dir: .fraisier/templates   # relative to fraises.yaml location
```

The path can be absolute or relative. Relative paths are resolved from the
directory that contains `fraises.yaml`.

The conventional location is `.fraisier/templates/` inside your project root,
alongside `fraises.yaml`.

## How this works on a deployed server

Two things follow from "relative to the `fraises.yaml` location", and both used
to bite silently (#312):

- **The server's `fraises.yaml` is `/opt/fraisier/fraises.yaml`**, not the one
  in your checkout. A relative `template_dir` therefore resolves against
  `/opt/fraisier/`. Deploy-time config sync copies your template directory
  there alongside `fraises.yaml`, so a directory committed to your repo is the
  one the server renders from.
- **A commit that changes only a template still triggers regeneration.** Change
  detection hashes the template tree as well as `fraises.yaml`; a template edit,
  addition or rename all count.

If `template_dir` is set but the directory is not found at render time,
fraisier logs a **warning** naming the path it looked in and continues with
built-in templates. It does not fail the render — but that warning is the one
to look for if a customisation appears not to have taken effect. Before v0.53.0
there was no warning at all: the built-in template rendered and looked correct.

Both provisioning paths carry the directory: `fraisier bootstrap` uploads it
alongside `fraises.yaml`, and every deploy re-syncs it from the checkout. Before
v0.53.1 bootstrap uploaded only the config, so a freshly bootstrapped host had a
dangling `template_dir` until its first deploy — the initial scaffold was still
correct, because bootstrap renders it locally, but any server-side render in
that window fell back to built-ins.

An **absolute** `template_dir` is never synced — it names a location you manage
on the server yourself.

## Template path structure

Override files must mirror the built-in template path structure exactly.
Built-in templates live under `fraisier/scaffold/templates/` and are organised
into sub-directories:

```
core/
  gateway.conf.j2          # shared nginx gateway
  gateway_env.conf.j2      # per-environment nginx block
  service.j2               # per-fraise systemd service unit
  install.sh.j2            # install script
  backup.sh.j2             # backup script
  confiture.yaml.j2        # confiture config
  sudoers.j2               # sudoers drop-in
  ...
provider/
  deploy.yml.j2            # GitHub Actions workflow
```

To override a template, place the replacement file at the same relative path
inside your `template_dir`.  For example, to override the nginx environment
config:

```
.fraisier/templates/
└── core/
    └── gateway_env.conf.j2   # your override
```

## Example: overriding the nginx environment config

Suppose you want to add a custom `proxy_cache` directive to every per-environment
nginx block.  Copy the built-in `core/gateway_env.conf.j2` to your project:

```bash
cp fraisier/scaffold/templates/core/gateway_env.conf.j2 \
   .fraisier/templates/core/gateway_env.conf.j2
```

Edit `.fraisier/templates/core/gateway_env.conf.j2` to add your changes, then
run `fraisier scaffold` as normal.

## Fallback to built-in templates

Any template **not** present in `template_dir` is loaded automatically from the
built-in set.  You only need to override the templates you want to change; all
others continue to work as before.
