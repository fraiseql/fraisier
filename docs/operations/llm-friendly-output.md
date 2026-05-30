# LLM-friendly output

Since v0.31.0, fraisier's CLI is **compact by default** — a one-line
`ok ...` summary on success, a focused failure with a tee'd log path on
error. This keeps fraisier readable to AI coding agents and CI runners
that pipe its output into bounded contexts.

Inspired by [rtk-ai/rtk](https://github.com/rtk-ai/rtk)'s compression
strategies, with the flag polarity inverted: rtk opts *into* compact;
fraisier opts *out* via `--verbose`.

## Output modes

| Mode | Trigger | Behaviour |
|---|---|---|
| **Compact** | default | One-line `ok ...` on success, focused failure + tee'd log on error, Rich markup stripped. |
| **Verbose** | `--verbose` / `-v` | Today's full Rich-markup output for interactive operator use. |
| **More verbose** | `-vv` | Same as `-v` plus DEBUG-level Python logging. |
| **Maximum verbose** | `-vvv` | Same as `-vv` plus full subprocess pass-through (no truncation). |
| **JSON** | `--json` | One structured JSON object on stdout; suppresses all text output. |

`--verbose` and `--json` are mutually exclusive — passing both fails
with a clear error.

`--no-tee` skips the failure-log file (useful in throwaway CI jobs
where you don't want disk artefacts).

## Auto-detect

fraisier *never auto-upgrades* to verbose. `CLAUDECODE=1`, `CI=1`, and
absent-TTY environments stay on compact. Verbose is always opt-in via
the explicit flag. This guarantees no environment-dependent surprises
in CI logs.

## Tee logs (failure recovery)

When a command fails (or raises mid-run), the full Rich-mode output is
preserved at:

```
$XDG_DATA_HOME/fraisier/logs/<command>-<UTC_timestamp>.log
# default: ~/.local/share/fraisier/logs/
```

The log file is created with `0o600` permissions (operator-only
readable) since `-vvv` subprocess pass-through can include deploy
tokens or env vars.

Clean exits delete the log; only failed runs preserve it. This avoids
filling the disk during successful invocations while keeping verbose
context one `cat` away when something breaks.

Disable the tee with `--no-tee` if you don't want log artefacts on
disk.

## Examples

### `fraisier ship`

**Compact (default):**

```
$ fraisier ship --pr --wait-deploy
ok Version bumped: 0.30.0 -> 0.31.0
ok PR created: https://github.com/fraiseql/fraisier/pull/247
ok Auto-merge enabled (squash): https://github.com/fraiseql/fraisier/pull/247
ok Shipped v0.31.0
ok Deploy successful! 0.30.0 -> 0.31.0
```

**Verbose (`-v`):** today's Rich-markup output verbatim.

**JSON (`--json`):**

```json
{"command": "ship", "events": [
  {"status": "ok", "label": "Version bumped: 0.30.0 -> 0.31.0"},
  {"status": "ok", "label": "PR created: https://..."},
  {"status": "ok", "label": "Auto-merge enabled (squash): https://..."},
  {"status": "ok", "label": "Shipped v0.31.0"},
  {"status": "ok", "label": "Deploy successful! 0.30.0 -> 0.31.0"}
]}
```

### `fraisier sync`

**Compact:**

```
$ fraisier sync main staging
ok Done. PR created and auto-merge enabled: https://github.com/...
```

Or, when nothing to do:

```
ok Already up to date — nothing to sync.
```

### `fraisier trigger-deploy`

**Compact:**

```
$ fraisier trigger-deploy my_api production
ok Deployment successful - api/production v0.31.0 (8.2s)
```

**Failure:**

```
FAILED: Deploy timed out
  fraise/env: my_api/production
  full log: ~/.local/share/fraisier/logs/trigger-deploy-20260530T161842Z.log
```

## Migration for CI workflows that grep output today

The new compact format preserves these existing tokens as literal
substrings:

| Token | Where |
|---|---|
| `Shipped v<version>` | end of `fraisier ship` |
| `Version bumped: X -> Y` | `fraisier ship` bump phase |
| `PR created: <url>` | `fraisier ship --pr` and `fraisier sync` |
| `Auto-merge enabled (<method>):` | `--auto-merge` path |
| `Deploy successful!` | end of deploy run |
| `Deployment successful` | `trigger-deploy` socket response |
| `Deployment triggered successfully` | `trigger-deploy` async path |
| `Already up to date` | `fraisier sync` no-op |
| `Done.` | `fraisier sync` completion |

A grep like `grep "Shipped v" build.log` continues to work without
modification.

For **new** tooling, prefer `--json` and parse the structured payload:

```bash
fraisier ship --json | jq '.events[] | select(.status == "error")'
```

## Future work (v0.32.0+)

- Extend compact-default to `fraisier doctor`, `preflight`,
  `scheduled-install`, `status`, `sync --check`.
- Apply the same treatment to the webhook server's `journalctl`
  output (`logger.info(...)` lines in `fraisier.webhook`).
- Optional `-u`/`--ultra-compact` mode (rtk's design) once usage data
  justifies the additional surface.
