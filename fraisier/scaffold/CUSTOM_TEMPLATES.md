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
