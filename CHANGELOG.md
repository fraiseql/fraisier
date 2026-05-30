# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.32.0] - 2026-05-30

### Fixed

- **`fraisier sync` no longer aborts when a prior squash-merged PR left an orphan remote head branch behind** ([#248](https://github.com/fraiseql/fraisier/issues/248)). When a prior sync PR was squash-merged without `--delete-branch` (the GitHub Web UI default and the natural shape of `gh pr merge --auto --squash`), the remote head branch kept its pre-squash commits. The next sync run created a fresh local branch from `origin/<source>`, tried to push, and was rejected with `! [rejected] (fetch first)` — the whole sync aborted with `subprocess.CalledProcessError` and the operator had to `git push origin --delete <branch>` by hand and re-run. The push path now runs through a new `_push_sync_branch(...)` helper that pre-flights the orphan check (delete the remote branch when its most recent PR is MERGED or CLOSED, skip when OPEN) and retries once on the canonical non-fast-forward shape if the race window between pre-flight and push elected a PR merge in the gap. The retry uses plain push when the orphan can be reclaimed, and `--force-with-lease` only when a live OPEN PR is still using the branch — and only inside the new `fraisier/**` namespace. The push subprocess runs with `LC_ALL=C` so the English-only stderr sniffer (`"non-fast-forward"`, `"fetch first"`) is locale-stable regardless of operator environment. Reclaim deletes use `subprocess.run(..., check=False)` so a branch that vanishes between the exists-check and the `--delete` (concurrent fraisier run, manual cleanup) is treated as the desired end state rather than a fresh failure.

### Changed

- **BREAKING (operational, not API): sync branches moved into the `fraisier/**` namespace.** `fraisier sync` now creates `fraisier/sync/<target>-from-<source>` instead of `sync/<target>-from-<source>`. The new namespace is declared **fraisier-owned** in `README.md` — any branch under `fraisier/**` may be created, updated, deleted, or force-pushed by fraisier without warning. This contract is what makes the orphan-reclaim and the live-PR-race `--force-with-lease` retry above safe in principle, not just in practice. The previous flat `sync/*` namespace is no longer touched by 0.32; the rename has no fallback shim. **Upgrade note:** merge or close any in-flight pre-0.32 sync PRs **before** upgrading. After upgrade, a new sync run will not find the old PR via `_find_existing_pr` (it looks for the new branch name) and will open a fresh PR alongside the stale one — you will end up with two open PRs into the same target if the old one is still around. Old `sync/*` branches left in the remote are not auto-cleaned; delete them by hand (`git push origin --delete sync/<target>-from-<source>`) once their PRs are closed.

### Added

- **`fraisier/**` reserved as the tool-owned branch namespace.** Future fraisier commands will colocate their throwaway branches under this prefix (`fraisier/<command>/...`). Hand-authored work pushed under this prefix will be reclaimed on the next fraisier run — keep your branches outside it. See the new README "Branch namespace" section.

## [0.31.0] - 2026-05-30

### Fixed

- **Webhook self-upgrade no longer kills concurrent deploys** ([#246](https://github.com/fraiseql/fraisier/issues/246)). On multi-environment hosts (e.g. dev + staging served by one webhook), the canonical "merge fraisier bump → ship dev → ship staging" promotion previously hit a race: the second deploy arrived during the upgrade worker's restart window and was killed by systemd's `SIGTERM` mid-flight, with no retry. The upgrade worker now touches a `.draining` flag in `lock_dir` **before** running `uv tool install`, sleeps a short settle window so dispatch-accepted tasks reach their `with deployment_lock(...)` line, waits for any in-flight `*.lock` to be released (default 10-minute timeout), and only then issues the restart RPC. During that window the webhook returns `HTTP 503 Service Unavailable` with a `Retry-After: 60` header and a structured JSON body identifying the refused fraises, so upstream callers (GitHub Actions, curl, monitors) record a loud, retriable failure rather than the previous silent drop. Drain-timeout exits with rc `2` (distinct from install-failure / restart-RPC-failure rc `1`), logs the held lock basenames, and leaves the unit unrestarted for operator intervention. Four new defaulted `webhook.self_upgrade_*` config keys (`drain_timeout_s` = 600, `drain_poll_s` = 1.0, `drain_settle_s` = 2.0, `retry_after_s` = 60) tune the timing; no `fraises.yaml` change is required for existing hosts to pick up the fix. The coordination is correct for `lock_backend=file` (the default); `lock_backend=database` hosts get the dispatch refusal but the drain loop sees no `*.lock` files and proceeds to restart immediately — behaviour for those hosts is no worse than today and a SQL-backed drain helper is tracked as a follow-up. New operator doc at [`docs/operations/self-upgrade.md`](docs/operations/self-upgrade.md) covers the knobs, failure modes, an optional GH Actions retry snippet, and the explicit residual-race scope.

### Changed

- **CLI output is now LLM-friendly by default**. Inspired by [rtk-ai/rtk](https://github.com/rtk-ai/rtk)'s compression strategies, with the flag polarity inverted: rtk opts *into* compact, fraisier opts *out* via `--verbose`. fraisier is increasingly invoked by non-human callers (GitHub Actions runners, webhook subprocesses, Claude Code Bash tool calls, ship pipeline self-invocations); today's Rich-markup output is verbose, ANSI-heavy, and noisy in those contexts. The new default mode strips Rich markup tags from all `console.print(...)` paths (Phase 1 leverage: one `_LazyConsole.print` rewrite covers ~149 call sites in `ship`, `sync`, `_deploy`, and adjacent CLI modules) and routes the structural success events (`Shipped vX.Y.Z`, `PR created: <url>`, `Auto-merge enabled (<method>)`, `Deploy successful!`, `Already up to date`, `Done. PR created/updated`, `Deployment successful`, `Version bumped: X -> Y`) through a new `fraisier._output.success(...)` helper that prefixes each line with `ok ` so LLMs and CI parsers can scan deterministically. Failure paths route through `fraisier._output.failure(...)` which prints `FAILED: <label>` + a focused detail line + the path to a tee'd full log under `~/.local/share/fraisier/logs/<command>-<ts>.log` (XDG-compliant, `0o600` permissions; clean exits delete the log, failures keep it for inspection). Three new global flags wired by `fraisier._output.install_cli_flags`: `--verbose`/`-v` (count flag; `-v` restores today's Rich story, `-vv` adds DEBUG logging, `-vvv` includes full subprocess pass-through), `--json` (one structured payload on stdout, mutually exclusive with `--verbose`), and `--no-tee` (skip the failure log file). Auto-detect *never upgrades* to verbose — `CLAUDECODE=1`, `CI=1`, and absent-TTY environments stay on compact unless the explicit flag is passed, so CI logs never get unexpected Rich noise. **Migration**: workflows wanting today's verbose output should add `--verbose` to their fraisier invocations; new tooling should prefer `--json`. The compact format preserves the key success-line substrings (`Shipped`, `PR created:`, `Deploy successful!`, `Auto-merge enabled`, `Already up to date`, `Done.`, `Deployment successful`, `Deployment triggered successfully`, `Version bumped:`) so existing `grep` patterns in CI workflows continue to match. v0.32.0 extends this treatment to `fraisier doctor`, `preflight`, `scheduled-install`, `status`, and the webhook server's `journalctl` output. New operator doc at [`docs/operations/llm-friendly-output.md`](docs/operations/llm-friendly-output.md) covers the output modes, tee location, the explicit `--verbose` opt-in story, and worked examples for each command.

## [0.29.0] - 2026-05-29

### Added

- **Webhook-driven scheduled install** ([#240](https://github.com/fraiseql/fraisier/issues/240)). After running `fraisier scaffold-install` once per host, webhook deploys of `type: scheduled` fraises now automatically copy declared unit files into `/etc/systemd/system/`, `daemon-reload`, and `enable --now` the timers — no more remembering to ssh in and run `sudo fraisier scheduled-install` after every config change. Manual `scheduled-install` becomes an override (rollback debugging, change-control), not a routine step. Per-fraise drift policy in `fraises.yaml` (`scheduled.auto_install.{on_missing,on_drift}`) controls behaviour when source and dest differ; default `on_drift: fail` aborts the deploy rather than silently overwriting operator hand-edits to `/etc/systemd/system/` (which is operator-editable in a way `app_path` is not). `overwrite` and `skip` are opt-in per-fraise. `deploy_event` grows `drift_overwrites`, `skipped_drift_units`, and `retried_busy` fields so external tooling can surface the structured change records.
- **`fraisier-unit-installer` socket helper** ([#240](https://github.com/fraiseql/fraisier/issues/240)). New root-privileged daemon receives manifest-driven install requests over a Unix socket. Enforces real `SO_PEERCRED` (UID must match the configured `deploy_user`), a render-time allowlist (`--allow <src_prefix>:<dest_prefix>` pairs baked at scaffold time), full TOCTOU defense via snapshotted `(dev, inode)` on each `dest_prefix` + `os.open(dir_fd, basename, O_NOFOLLOW)` on every file open (no `shutil.copy2`), per-op subprocess timeouts (30s `daemon-reload`, 60s per unit-targeting action), 5-minute overall wall-clock cap, and `fcntl.flock`-protected manifest execution so two concurrent deploys can't interleave (second gets `{"status": "busy"}` and retries). Marker-presence runtime gate on `disable_now`/`stop` post-actions prevents a deploy_user from disabling `sshd.service` via a crafted manifest. Manifest size capped at 1 MiB. New `--via-socket` flag on `fraisier scheduled-install` routes the manual command through the helper too, dropping the operator-sudo requirement for that path.
- **`SO_PEERCRED` retrofit across all four socket helpers** ([#240](https://github.com/fraiseql/fraisier/issues/240)). The existing `systemctl-helper`, `scaffold-install-helper`, and `install-helper` had docstrings implying a peer-creds check that the code never actually performed — the v0.28 trust boundary was purely `SocketUser=root, SocketGroup=<deploy_user>, SocketMode=0660` (any process in the deploy group could connect). All three now read `SO_PEERCRED` via the shared `fraisier._peer_creds` module and reject non-matching UIDs. The unit's `ExecStart` carries `--deploy-user <name>` injected by the renderer; the helper resolves to UID at startup via `pwd.getpwnam` (deferred resolution survives renderer/target-host split). A transitional shim (`--deploy-uid` numeric override, falls back to "log a one-time warning, skip the check, continue" when neither flag is passed) keeps pre-v0.29 units on disk working through the upgrade window; v0.30 will make the flag mandatory.
- **`scheduled-install --prune` for orphan removal** ([#240](https://github.com/fraiseql/fraisier/issues/240)). New `*.fraisier-managed` marker convention writes a JSON sidecar (mode 0600, root-owned) next to each fraisier-installed unit, carrying `fraises_yaml_path` (always `Path.resolve(strict=True)`), `fraise_name`, `environment`, `job_name`. `fraisier scheduled-install --env <env> --prune --dry-run` lists units with markers but no current `fraises.yaml` declaration; `--prune --yes` disables/stops them (timer-first ordering so a timer can't fire mid-prune), removes the unit + marker, and `daemon-reload`s once. Per-yaml + per-env scoping (both sides resolved to absolute paths) keeps cross-project hosts safe. Stale markers (corrupt JSON or no paired unit) are classified separately and cleaned up without invoking `systemctl`. Markers are **advisory, not authenticated** — `/etc/systemd/system/` is root-only-write so unprivileged adversaries can't plant fakes; root-side attackers can do anything anyway, so the marker convention is scoped to honest cross-project / cross-env mistakes, not security. v0.28-installed units auto-backfill their markers on the first v0.29 webhook deploy via a new `write_marker` op kind that the client emits for IDENTICAL diffs whose marker is missing on disk (idempotent: re-runs after the marker exists emit no op for that unit). `--prune --via-socket` lands in v0.30 with `RemoveFileOp` + ordered pre-actions in the helper protocol; v0.29's `--prune` runs under operator-typed sudo.
- **Drop the synthesised `<project>_<scheduled-fraise>_<env>.service` from `_collect_allowed_services`** ([#240](https://github.com/fraiseql/fraisier/issues/240)). The systemctl-helper allowlist no longer carries this phantom entry for `type: scheduled` fraises — no such unit exists on disk; the real per-job entries (added in v0.28.0 [#239](https://github.com/fraiseql/fraisier/issues/239)) are what actually need to be reachable. Cosmetic cleanup of the rendered allowlist + the legacy `systemctl-wrapper.sh` allowed-services array.

## [0.28.0] - 2026-05-29

### Added

- **`fraisier scheduled-install` — one-shot bootstrap of `type: scheduled` job systemd units** ([#239](https://github.com/fraiseql/fraisier/issues/239)). When a consumer adds a new `type: scheduled` job to `fraises.yaml`, the unit files land in the deployed worktree at `<app_path>/scripts/systemd/` but never reach `/etc/systemd/system/`. The operator previously ssh'd in, `cp`'d files, ran `daemon-reload`, and `systemctl enable --now <timer>` by hand — for every host, every new job, forever. The new `fraisier scheduled-install --env <env>` reads `fraises.yaml`, walks every `type: scheduled` fraise's `jobs.*`, and for each declared `systemd_service`/`systemd_timer`: compares source bytes against `/etc/systemd/system/<unit>`, copies + chmods 0o644 on `ABSENT`, classifies `DRIFTED` and fails closed (operator must pass `--force` to overwrite — no backup files are left behind), `daemon-reload`s once per call when any write happened, and `enable --now`s each timer that was written. `IDENTICAL` units are left untouched (re-runs are idempotent: zero file writes and zero `daemon-reload` invocations on a converged host). `MISSING_SOURCE` (operator declared a unit in yaml but the file isn't on disk) raises before any mutation. Flags: `--env` (required; lists available envs on omission), `--dry-run`, `--validate-only` (exits 0/1/2 for identical/missing/drifted), `--force` (overwrite drifted), `--yes/-y`, `--verbose/-v` (full unified diffs for drifted), `--fraise NAME` (narrow scope). Runs under operator-typed `sudo` — file copy and `systemctl` invocations both need root, and no new sudoers rule is rendered by `scaffold` for this command. Path-traversal defences run *before* any filesystem mutation: `unit_name` containing `/` or `..` is rejected (`validate_service_name`'s regex `^[a-zA-Z0-9_@.\-]+$` accepts `..` because `.` is in the allowlist, so an explicit substring check guards against that gap), `dest_path.parent` must resolve to `/etc/systemd/system`, and `source_path.resolve()` must be contained under `<app_path>/scripts/systemd/` (blocks a hostile worktree symlinking `scripts/systemd/foo.timer` outside the app dir). No automated CI coverage for the apply phase — `systemctl daemon-reload` and `enable --now` need a real init system; the operator-runtime contract is verified by the manual smoke checklist in the PR description only. Enumeration, classification, CLI surface, and the helper-allowlist fix below are fully unit-tested.
- **`_collect_allowed_services` now includes scheduled-job systemd units** ([#239](https://github.com/fraiseql/fraisier/issues/239)). The systemctl-helper allowlist (`fraisier/scaffold/renderer.py`) walked each fraise's environments but never descended into `jobs.*`, so the `systemd_service` / `systemd_timer` names declared on `type: scheduled` fraises' jobs were absent from the helper allowlist. The webhook-driven `ScheduledDeployer` runs unprivileged through this helper socket, so any newly-declared scheduled unit was rejected at deploy time until the operator separately re-ran `scaffold-install` — defeating the seamless-redeploy property webhook deploys are supposed to provide. NOT [#218](https://github.com/fraiseql/fraisier/issues/218) (which was the missing webhook unit, closed in v0.22.2); this is a separate, previously-untested gap surfaced by #239's work. **Staleness caveat**: the change updates the *generator*; the rendered `/etc/systemd/system/fraisier-<project>-systemctl-helper.service` on a target host is a separate artefact that only refreshes when `scaffold-install` runs. After declaring a new `type: scheduled` job, the operator must run `fraisier scaffold && fraisier scaffold-install --yes && sudo fraisier scheduled-install --env <env>` in that order. Skipping the middle step leaves the helper carrying the old allowlist; webhook-driven redeploys of the new timer will then be rejected until `scaffold-install` runs. Documented in `docs/cli-reference.md` under the `scaffold-install` "Adding a type: scheduled job" subsection.

### Deferred / out of scope

- `fraisier scheduled-install --prune` (marker-file-based orphan removal) — deferred to a follow-up issue. The hard part is "fraisier-owned naming pattern"; the design wants more data on the post-install path before committing to a marker-file convention.
- Webhook-driven install — letting `ScheduledDeployer.execute()` invoke the install path on first sight of a new unit, so operators never need to run `scheduled-install` by hand. Worth designing after a few cycles of operating the manual command.
- Generalising `scaffold_install_helper.py` to accept install-job manifests so this command could run unprivileged via socket. Larger redesign; bare `sudo fraisier scheduled-install` is the right MVP shape.

## [0.27.1] - 2026-05-29

### Added

- **Documented "bring-your-own batched releases" workflow** ([#234](https://github.com/fraiseql/fraisier/issues/234)). New [`docs/release-strategies.md`](docs/release-strategies.md) explains how to pair fraisier with [release-please](https://github.com/googleapis/release-please) (or any equivalent batcher) so feature PRs land code only via `fraisier ship --no-bump`, and a single release PR accumulates the version bump and CHANGELOG. Includes a working release-please GitHub Actions example, a trade-off table against the default per-PR workflow, and notes on how `--wait-deploy` and `fraisier sync` behave in each. No `release_strategy` yaml field is added; the design memo for that proposal (#234) is summarised in the doc page itself — the existing `--no-bump` flag plus a release-please workflow file is sufficient, and committing the schema surface is premature without adoption data.
- **`fraisier ship --no-bump --wait-deploy` now prints an explanatory note** before the health poll: `--no-bump: no version change — polling vX.Y.Z to confirm the current redeploy stays healthy. A later release-PR merge (if any) produces a separate deploy.` Operators running a bring-your-own release-please workflow could otherwise mistake the immediate health-poll success for the release-PR-triggered deploy they were expecting; the note makes the semantics explicit. `_trigger_deploy_for_current_branch` gained a keyword-only `no_bump` parameter; default `False` preserves the per-PR path's output verbatim.

## [0.27.0] - 2026-05-28

### Added

- **`fraisier ship` detects version-bump races against origin at push time** ([#232](https://github.com/fraiseql/fraisier/issues/232)). Two concurrent `fraisier ship` invocations starting from the same base both computed the same next version locally; the second to finish pushed a duplicate-version branch whose PR was `mergeable=CONFLICTING`, auto-merge never fired, and nothing paged anyone. After `run_verify_phase` succeeds and immediately before `git commit`, ship now runs `git fetch --quiet origin <race-branch>` (30 s timeout) and re-reads the `version` field from `pyproject.toml` at `origin/<race-branch>`. The race branch is the PR target when `--pr` is set (falls back to `ship.pr_base` in `fraises.yaml`) — the race is on the destination, not the working feature branch — else the current branch. When origin no longer matches the version observed at start, ship rolls back the on-disk pyproject bump (`git checkout HEAD -- pyproject.toml`) and exits 1 with `✗ Version race detected.` naming both versions and a copy-pasteable rebase recipe (`git pull --ff-only` on the base, `git rebase` on the working branch, re-run `fraisier ship <bump-kind>`). For `--no-bump` re-ships the recovery message tells the operator to decide whether to abandon or pick a new version, since re-shipping at the stale version is regressive. The check runs in both the pipeline and legacy ship paths; `--dry-run` skips it (no real push); `--no-deploy` does not skip it (still pushes, race still matters). Cost: one `git fetch` (~1 s) on a ~10-minute ship. "Branch missing on origin" (first push) degrades silently. Other fetch failures (network blip, auth, timeout) print a yellow `Warning:` and proceed — a flaky network shouldn't block ship.

## [0.26.1] - 2026-05-28

### Fixed

- **`fraisier sync` now propagates source-side deletions when target is unchanged** ([#235](https://github.com/fraiseql/fraisier/issues/235)). `git merge` doesn't surface "source deleted X, target never touched it" as a UU-style conflict — it silently keeps target's copy, so the sync PR carried the deleted file forward and the next deploy still saw the legacy file. A new `_propagate_source_deletions` pre-pass runs after `git merge --no-commit` and before the existing conflict loop: for each path in the merge-base→source `--diff-filter=D` list that target hasn't modified since merge-base, `git rm` mirrors the deletion onto the sync branch. Files that target *did* modify since merge-base are left for the operator (the existing tier-1 auto-resolver still handles the source-deleted-target-modified conflict case via `cat-file -e`). Surfaces as `Auto-resolved (source deletion): <path>` in the operator output.
- **`fraisier sync --prefer-source` now always creates the merge commit** ([#233](https://github.com/fraiseql/fraisier/issues/233)). When the conflict resolver auto-resolved every file back to source's version (the sync branch's tip), the index after `git add` was byte-identical to HEAD, the old `_commit_if_staged` helper's `git diff --cached --quiet` guard skipped the commit, MERGE_HEAD stayed set, and `git push` silently dropped the merge parent. GitHub then marked the PR `mergeable=CONFLICTING` and the CI sync workflow re-ran the merge and re-discovered the conflicts — the operator saw green local output but a stuck PR. The new `_commit_merge_or_staged` helper detects MERGE_HEAD via `git rev-parse --verify --quiet MERGE_HEAD` and commits unconditionally during a merge — git is happy to create a merge commit with no tree diff, which is exactly what records both parents. The non-merge fallback path keeps the original commit-if-staged semantics so empty commits outside a merge remain refused.
- **`fraisier sync` aborts before push if HEAD isn't a merge commit** ([#233](https://github.com/fraiseql/fraisier/issues/233) Layer 2 defence-in-depth). A new `_assert_merge_finalized` pre-push assertion verifies that `git log -1 --pretty=%P HEAD` reports at least two parent SHAs (so octopus merges with ≥3 parents pass too) *and* `MERGE_HEAD` is cleared. If either invariant is broken (a future code path skips the commit, a corrupt merge state, …) the push is refused with `✗ Sync abort: auto-resolve completed but HEAD is not a merge commit` rather than silently producing a CONFLICTING PR.
- **Deletion-propagation log no longer claims success when `git rm` fails** (post-review polish on #235). When the underlying `git rm` exits non-zero (submodule pathspec, sparse-checkout exclusion, …), the operator no longer sees `Auto-resolved (source deletion): <path>` followed by an unchanged index. The path is omitted from the propagation log and a `Warning: could not propagate deletion of <path>: <git stderr>` line is printed instead. Sync continues with the unresolved deletion still pending; downstream checks (the conflict loop, the pre-push merge-commit guard) handle the remainder.
- **`fraisier sync` surfaces a clear error if `git commit` fails mid-merge** (post-review polish on #233). `_commit_merge_or_staged` now catches `subprocess.CalledProcessError` from the underlying `git commit` and prints `✗ Sync abort: git commit failed during {merge finalization | pre-merge commit} (exit N). Inspect git status …` before exiting. The outer cleanup still resets the working branch and deletes the half-built sync branch; the new message just replaces the previous opaque traceback.

## [0.26.0] - 2026-05-27

### Added

- **`fraisier.introspection` — subcommand → config-section map + `!envvar` walker** ([#221](https://github.com/fraiseql/fraisier/issues/221), foundation for bundle B). Internal API that answers "if I ran `fraisier <subcommand>`, which YAML paths would it touch, and which `!envvar` refs would be reachable?" without running the subcommand. `ConfigPath` (dotted globbing), `EnvVarRef` (named tuple of name / yaml_path / is_set), `reachable_envvars(config, subcommand, *, fraise, environment)`. Walker never resolves `LazyEnv` — only inspects placeholders. A drift-guard test asserts every registered CLI command is either in `SUBCOMMAND_CONFIG_SECTIONS` or in `COMMANDS_WITHOUT_CONFIG_ACCESS`.
- **"Reads envvars:" section in every subcommand `--help`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 2). Click `format_epilog` hook derives the listing from the introspection map and the loaded config. Marks unset variables with `[unset]`. Renders a graceful placeholder when no `fraises.yaml` is discoverable. Verified safe across both `fraisier <cmd> --help` and `fraisier --config X <cmd> --help` invocation orders via a Cycle-0 spike before designing.
- **`fraisier env-check <subcommand>`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 3). Standalone preflight that prints which env vars a given subcommand would read and which are currently unset. Designed for CI gates: exit 0 if all set, 1 if any unset, 2 if subcommand invalid. `--format text` (rich.Table) or `--format json` for piping. `--required-only` filters to unset variables. `--fraise` / `--environment` narrow scope.
- **`fraisier doctor`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 4). Host-wide self-diagnosis independent of any particular fraise; answers "is this install OK to use?" Pluggable check registry with seven shipped checks: `python_version`, `fraisier_version`, `confiture_version`, `fraises_yaml_loadable`, `fraises_yaml_resolves` (uses the introspection walker), `secrets_env_readable`, `helper_sudoers` (stat-only — never invokes `visudo`, never parses sudoers syntax; byte-diffs against the expected fragment via `fraisier.scaffold.sudoers_diff` when content is readable). Each check is side-effect-free and isolated — one failing check never aborts the rest. `--format text|json`, `--check NAME` (repeatable) for filtering, `--skip-network` for offline environments. Exit codes: 0 / 1 / 2 for pass / fail / warn-only. Documented in `docs/doctor.md`.
- **`--format json` on `ship` (dry-run) and `deployment-status`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 6). New shared `--format text|json` option pattern via `fraisier/cli/_json.py`. `fraisier ship <bump> --dry-run --format json` emits a structured `{version: {old, new, bump_type}, dry_run, create_pr, pr_base, auto_merge, ...}` payload suitable for piping into `jq`. `fraisier deployment-status --json` is preserved as a hidden deprecated alias for `--format json`; the legacy flag emits a one-time per-process stderr warning. Other commands' existing `--json` flags are left untouched in this release — migration of the remaining ~10 sites will follow as a separate change.



### Added

- **Worked-example epilogs on every `--help` body, plus a `ship` flag-interaction matrix** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 1). Every subcommand under `fraisier ...` now ends its `--help` with at least two `fraisier ...` example lines so an agent or operator can grok flag interactions without source-reading. `ship --help` additionally enumerates the `--pr` / `--auto-merge` / `--wait-deploy` / `--no-deploy` / `--no-bump` / `--skip-checks` relationships in a single block. A new `tests/test_help_epilogs.py` parametrizes over every leaf command in `fraisier.cli.main.main` and asserts the contract holds — drift catches a missing epilog at CI time.
- **Per-instance `recovery_hint` on framework errors, surfaced into `str(err)`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 7). `FrameworkError.__init__` accepts a `recovery_hint` kwarg; when set, `str(err)` renders the hint as a trailing `Recover with: <hint>` line. A new `fraisier.errors.RECOVERY_HINTS` catalog supplies canonical strings keyed by scenario tag (`migration_preflight`, `migrate_partial`, `health_check_unhealthy`, `rollback_failed`, `deploy_timeout_unknown_phase`) so wording stays consistent across raise sites. The `MigrationPreflightError` raise site in `fraisier/strategies/_restore.py` is wired through to the canonical hint — the exact incident the issue references. CLI error rendering picks up the trailing line for free since handlers interpolate `str(exc)`. Empty-string explicitly suppresses the line.
- **`docs/webhook-protocol.md`** ([#221](https://github.com/fraiseql/fraisier/issues/221) item 5). Public contract for the JSON payload fraisier POSTs to `type: webhook` notification destinations. Documents every `DeployEvent.to_dict()` field, all four `event_type` values, the operator-supplied header auth pattern (fraisier does not HMAC-sign outbound notifications — the inbound `_verify_signature` machinery is unrelated), stability guarantees, three worked examples covering `failure` / `rollback` / `success`, and idempotency semantics. A new `tests/test_docs_webhook_protocol.py` parses every fenced JSON block in the doc and asserts every documented field is emitted by `DeployEvent.to_dict()` (and vice-versa) so doc and code can't silently drift.

## [0.24.0] - 2026-05-27

### Added

- **`scaffold-install` now diffs the current `/etc/sudoers.d/<project>` against the rendered fragment and warns about rules that would be removed** ([#224](https://github.com/fraiseql/fraisier/issues/224)). Previously the install silently overwrote the target file, so any operator-added rule (workarounds, non-fraisier components' rules, leftover entries from prior fraisier versions) silently disappeared on next `scaffold-install`. The wrapper now reads the current sudoers file via `sudo cat` (piggybacking on the existing sudo timestamp — no extra password prompt in practice), diffs against the rendered fragment using a line-set comparison that ignores comments and normalizes whitespace, and prints a clearly-formatted list of rules that would be removed. In interactive mode the existing `Proceed with installation?` confirm covers both the install plan and the sudoers diff — there is deliberately no second prompt. In `--yes` / `--dry-run` / `--validate-only` modes the diff still prints (`--yes` opts out of the prompt, not out of visibility). The check is silently skipped when `/etc/sudoers.d/<project>` doesn't exist yet (fresh installs).
- **`scaffold-install --strict-sudoers` flag** for CI/automation that should fail loud on any sudoers removal. Exits with code 3 (distinct from generic `1` and user-abort `2`) when rules would be removed OR when the current sudoers state can't be read — "couldn't verify" is treated as failure under strict, not silently as "no changes". Naming is deliberately per-resource: future nginx/systemd safety nets get sibling flags (`--strict-nginx`, ...) rather than a single coarse `--strict-overwrites`.
- **New module `fraisier.scaffold.sudoers_diff`** — pure-function `diff_sudoers(current, new) -> SudoersDiff` with `added` / `removed` rule lists and a `has_changes` property. Stdlib-only, no I/O, fully unit-tested.

## [0.23.1] - 2026-05-27

### Fixed

- **`scaffold-install` no longer crashes with an unhandled `PermissionError` on `Path.exists()` / `is_file()` / `chmod()`** ([#222](https://github.com/fraiseql/fraisier/issues/222)). On hosts where a parent directory of the generated `install.sh` isn't traversable by the invoking user, `Path.exists()` propagates `EACCES` as `PermissionError` rather than returning `False`. `is_file()` has the same failure mode. The wrapper now treats any `OSError` from those probes as "can't see the file" and prints the friendly "not found or not readable" error instead of a traceback. `chmod(0o755)` is similarly hardened: when the file is owned by another user the chmod is allowed to fail silently as long as the file is already executable (the common case for a freshly-generated script).

### Changed

- **`scaffold-install` failure messages now include the install.sh exit code and a copy-pasteable rerun command** ([#225](https://github.com/fraiseql/fraisier/issues/225)). The previous "Review the output above for details" line was misleading when no output existed (the wrapper inherits stdio, so a quiet `install.sh` produces no preceding output). Failures now print the actual exit code (`Preview exited with code 42` / `Validation exited with code 42` / `Installation failed (exit code 42)`) and a verbose+tee command the operator can paste to reproduce with a persistent log: `sudo /opt/<project>/scripts/generated/install.sh <flags> --verbose 2>&1 | tee /tmp/install.log`. `--verbose` is added only when not already present in the original invocation. The success path is unchanged. No behavioral change to subprocess invocation — this is strictly an error-message UX fix.

## [0.23.0] - 2026-05-26

### Changed

- **Lazy `!envvar` resolution** ([#220](https://github.com/fraiseql/fraisier/issues/220)). Env vars referenced via `!envvar` are now resolved at consumption time, not at config load. Subcommands that don't enter a section (`fraisier --help`, `fraisier --version`, `fraisier ship --help`, `fraisier ship patch --pr ...`, `fraisier list`) no longer require those env vars to be set. Resolution failures raise `ConfigurationError` (or the consumer's wrapping exception, e.g. `SmokeTestError` for smoke probes) with a message that names the full dotted YAML key path of the offending placeholder — `fraises.<name>.environments.<env>.smoke_tests[0].headers.Authorization` instead of an opaque `KeyError: 'SMOKE_TEST_JWT'`.
- **Read-each-access semantics for `!envvar`.** Each consumption of a `!envvar` field re-reads `os.environ`; there is no resolution cache. Previously the value was baked into the loaded config dict at parse time. The one-shot CLI invocation is unaffected; long-running consumers (the webhook daemon, tests using `monkeypatch.setenv`) now observe env-var mutations made after `FraisierConfig` construction.
- **Section-lazy validation.** Deep section validators (`fraises`, `notifications`, `hooks`) now run on first access of the matching property, not at `FraisierConfig.__init__`. Stage 1 of load is restricted to cheap structural / cross-reference checks (`servers`, `branch_mapping`, `service_manager`). Callers that depended on eager load failure for a specific section should use `fraisier validate` or access the relevant property explicitly.
- **`fraisier validate` semantics.** Default validate traverses every section and reports structural errors but does NOT resolve `!envvar` references — CI workflows that lint YAML shape without secrets are first-class. Pass `--resolve-envvars` to opt into the previous eager-resolution behavior (recommended for pre-deploy CI gates that need every secret materialized).
- **`fraisier validate --json` placeholder output.** Unresolved `!envvar` fields in dump output now appear as `"<envvar:NAME>"` placeholders rather than resolved values. Downstream JSON parsers should not assume resolved secrets in `validate --json` output.
- **YAML anchor / alias path tracking.** When an `!envvar` node is anchored and reused (`&shared !envvar X` … `*shared`), the resolution error names the *first* location encountered during the depth-first walk, not all of them. Operators diagnosing a shared anchor see only one of the use sites. PyYAML constructs each anchored node once, so this is the only stable single-path attribution available.

### Added

- **`fraisier validate --resolve-envvars` flag.** Walks the parsed config tree, resolves every reachable `LazyEnv`, and collects errors. Each unset variable surfaces with its env var name AND the YAML key path where it was declared. Shared LazyEnv instances (anchors / aliases) are resolved once per invocation.
- **`LazyEnv` placeholder and `to_str()` boundary helper** exported from `fraisier.config`. Plugin and external-callsite authors handling config values should call `to_str()` at any non-trivial consumer boundary (subprocess argv, HTTP headers, file paths). `LazyEnv` carries the env var name and YAML path for diagnostics; its `__repr__` never resolves, so logging a raw placeholder cannot leak the secret.

### Breaking

- **`format: !envvar X` rejected for token providers.** The token-provider `format` field must now be a literal string. Migration: replace `format: !envvar BEARER_FORMAT` with the literal pattern, e.g. `format: "Bearer {token}"`. Rationale: `format` is a `str.format`-style template whose `{token}` placeholder must be visible at config-load time for the placeholder-validity check; lazy resolution would defer that check until deploy time, by which point the template error is harder to diagnose.

## [0.22.2] - 2026-05-23

### Fixed

- **Webhook self-upgrade now surfaces helper allowlist rejections in the journal** ([#218](https://github.com/fraiseql/fraisier/issues/218)). The scaffold-template fix landed in 0.18.0 (`fraisier-{project}-webhook.service` is unconditionally prepended to the helper allowlist by `_collect_allowed_services`), so any host re-scaffolded on 0.18.0+ already self-upgrades end-to-end. Hosts originally bootstrapped before 0.18.0 still carry a stale `/etc/systemd/system/fraisier-{project}-systemctl-helper.service` whose `ExecStart` argv omits the webhook unit — `uv tool install` upgrading the binary doesn't rewrite that file — so the detached worker's restart RPC silently fails, recorded only in `/var/lib/fraisier/self-upgrade/<project>-<ts>.log`.
- **Pre-flight check in `maybe_self_upgrade`.** Before spawning the detached worker, the parent webhook now sends a read-only `is-active` RPC for its own service unit (`fraisier/webhook_self_upgrade.py:_preflight_helper_allowlist`). On a `service not allowed` rejection the webhook logs a WARNING **in its own journal** naming the cause and the remediation — `fraisier scaffold && fraisier scaffold-install --yes` — and skips the upgrade entirely (avoiding the half-state where the binary is upgraded but the process isn't). Operators watching `journalctl -u fraisier-<project>-webhook.service` see the actionable error on the next deploy attempt instead of having to discover the per-event log file.
- **Narrow scope.** Empty `FRAISIER_SYSTEMCTL_SOCKET` (install-only mode) and transient `ConnectionRefusedError` (startup races) fall through unchanged — those are non-actionable here and let the existing worker behaviour log its own per-event failure.

## [0.22.1] - 2026-05-23

### Fixed

- **Deploy daemon now finds `fraisier` under `uv tool install`** ([#216](https://github.com/fraiseql/fraisier/issues/216)). `BaseDeployer._get_fraisier_executable()` is replaced by a layered, cached resolver (`fraisier/deployers/base.py:_resolve_fraisier_executable`) that probes, in order: the `sys.executable` sibling (correct by construction for `uv tool install`, venv, pipx, and system-package layouts), `shutil.which("fraisier")`, then a hardcoded fallback list expanded to cover `~/.local/bin/fraisier` and `~/.local/share/uv/tools/fraisier/bin/fraisier`. Candidates are accepted only when they are regular files (or symlinks to one) with the executable bit set, so directories and broken symlinks no longer slip through. Lazy probe iteration means the happy path resolves with a single `stat(2)` — `$PATH` is not consulted when the sibling matches. The resolver is cached for the process lifetime (`functools.cache`); since the deploy daemon restarts after every self-upgrade, the cache cannot outlive a relocation of the binary it points at.
- **Self-diagnosing failure mode.** When no candidate resolves, the raised `RuntimeError` lists every probed location by name (`sys.executable sibling`, `$PATH`, each fallback path) plus the current `sys.executable` and a remediation hint (`uv tool install fraisier`, or symlink workaround). The journald entry is now actionable without re-running with `-v`.
- **Operator observability when the sibling probe misses.** When resolution falls back past the sibling, an `INFO` line is emitted once per process naming the strategy that succeeded and instructing the operator to re-run `uv tool install fraisier` to bring the daemon's Python and the `fraisier` CLI back into lockstep. The `ERROR` swallowing in `APIDeployer._sync_config_if_needed` is unchanged, so transient discovery anomalies still do not fail the deploy.

## [0.22.0] - 2026-05-22

### Added

- **Pluggable token providers for `smoke_tests`** ([#215](https://github.com/fraiseql/fraisier/issues/215)). New optional `token_provider:` block per smoke test acquires a short-lived bearer credential at deploy time, then injects it into the configured `header` using `format` (default `"Bearer {token}"`). Closes the gap where a long-lived JWT in `os.environ` doesn't fit — vault-issued tokens, OIDC machine clients, federated assume-role workflows. Three provider types are built in:
  - **`exec`** — runs a configured `command` (typed as a list of strings, invoked with no shell) and uses stdout (trailing newline stripped). Non-zero exit, timeout, or unexpected failure raises `DeploymentError` naming the exit code; the subprocess stderr tail is emitted at DEBUG only (never in the exception message, since a `set -x` wrapper could leak the token there). `argv[0]` logs at INFO; full argv at DEBUG; the resolved token never appears in any log line. `cwd` and `env_passthrough` are deferred to a later release and are **rejected at config-load time** if set — the subprocess inherits the deploy user's environment (same envelope as `post_migrate`).
  - **`oauth2_client_credentials`** — POSTs `grant_type=client_credentials` to `token_url` with `client_id`, `client_secret`, and optional `audience` / `scope`. Returns the response's `access_token`. Non-2xx, missing `access_token`, or network errors raise `DeploymentError`; the response body is never echoed in the error message (some IdPs include the `client_secret` in error envelopes). `client_secret` is redacted in all log lines.
  - **`oauth2_refresh_token`** — POSTs `grant_type=refresh_token` with `client_id` and `refresh_token`. Rotated `refresh_token`s in the response are **discarded** — fraisier never writes to your secrets store; rotation is the operator's responsibility.
- **`fraisier/token_providers.py`** — abstract `TokenProvider` base plus one concrete frozen dataclass per type: `ExecTokenProvider`, `Oauth2ClientCredentialsTokenProvider`, `Oauth2RefreshTokenProvider`. Each subclass carries only its own type-specific fields (no `None`-typed sentinels for sibling-type fields) and owns its `.resolve()` method. `parse_token_provider(raw)` dispatches on the YAML `type:` to the matching subclass. Pure-structural parse, never shells out or hits the network at config-load time. `materialize_test_headers(tests)` in `fraisier/smoke_tests.py` resolves each provider instance at most once per deploy (cache keyed on `id(provider)`); smoke tests sharing a `token_provider:` block via a YAML anchor (`&p` / `*p`) get the same token. Plain YAML duplication of the block produces distinct instances and distinct resolution calls — use anchors when sharing matters. The deploy pipeline enters via `smoke_tests.resolve_and_run(tests)`, which sequences materialization and execution and surfaces the two failure modes as distinct exception classes (`DeploymentError` vs `SmokeTestError`).
- **Header collision check.** A smoke test that declares both `headers.<X>` and a `token_provider.header=<X>` is rejected at config-load time with `ConfigurationError`. Comparison is case-insensitive per RFC 7230.
- **Strict `token_provider:` schema.** Unknown keys (typos, the deferred `cwd` / `env_passthrough` options, fields valid on a different provider type) are rejected at config-load time with the valid-key list for the declared type. Prevents silent no-ops at deploy time.
- **`format` placeholder validation.** The `format` value must contain the literal `{token}` placeholder and no other placeholders. A `format: "Bearer ABC"` would silently drop the resolved token; a `format: "Bearer {access_token}"` would `KeyError` mid-deploy. Both shapes fail at config-load now.

### Changed

- **`load_smoke_tests` now raises `ConfigurationError` instead of `ValueError`** for all schema errors (unknown `method`, unknown `on_failure`, malformed JSONPath, relative URL without base, unknown `token_provider.type`). The `_validate_smoke_tests` wrapper in `fraisier/config/_validation.py` catches both classes during the transition. Brings `smoke_tests.py` in line with the rest of `fraisier/config/`, which has always raised `ConfigurationError`. Callers catching `ValueError` from `load_smoke_tests` should switch to `ConfigurationError`.

### Upgrade notes

- Opt-in: a smoke test without a `token_provider:` block keeps v0.21.x behavior — the static `Authorization: !envvar X` header flows through verbatim. No deployment changes for users not using the new feature.
- A v0.22.0 fraises.yaml that uses `token_provider:` requires v0.22.0+ fraisier to parse. Roll forward, not back, once a provider is configured.
- Provider runtime failures (exec script crash, IdP 401, network error) raise `DeploymentError` and **halt** the deploy with `status=failed`. By this point migrations have run and the service has restarted on the new code; a transient IdP issue is not a code regression, so the previous version is **not** automatically restored — operators investigate and re-deploy. (Code regressions are still caught by `run_smoke_tests`'s `on_failure: rollback` default, which is unaffected by this change.)
- `exec` subprocess `stderr` is **no longer included** in the raised `DeploymentError` message: a wrapper with `set -x` would otherwise leak the token to the deploy journal. Operators who need the stderr tail can re-run with `FRAISIER_LOG_LEVEL=DEBUG`.
- See `fraises.example.yaml` and `docs/deployment-guide.md` for worked examples.

## [0.21.1] - 2026-05-22

### Fixed

- **`fraisier sync` is now idempotent on retry** ([#213](https://github.com/fraiseql/fraisier/issues/213)). Re-running after a previous interrupted or completed sync no longer fails:
  - The local sync branch is force-created (`git checkout -B`), so a stale branch from an interrupted prior run is silently overwritten by the fresh checkout. Sync branches are fraisier-owned end-to-end; any commits on them are re-derived from `origin/<source>` + pre-merge on every run.
  - After pushing, fraisier checks for an existing PR for the sync branch via `gh pr view`. When the prior PR is OPEN, it re-enables auto-merge (idempotent for `--squash`) on the existing PR instead of failing on `gh pr create`. The PR URL appears in the success message as `PR updated and auto-merge enabled: <url>`.
  - When the prior PR is CLOSED or MERGED, fraisier opens a fresh PR (GitHub allows reusing a head ref after closure). The prior PR URL is logged for operator context.

### Upgrade notes

- A user who has manually committed to a sync branch will lose those commits on the next `fraisier sync` invocation (the new `-B` overwrites the local branch). Sync branches were already documented as fraisier-owned; if you want to keep commits on a sync branch, push them to a non-sync ref.

## [0.21.0] - 2026-05-22

### Added

- **Authenticated smoke tests** ([#204](https://github.com/fraiseql/fraisier/issues/204) PR B). New `smoke_tests:` list runs configured HTTP requests with bearer credentials and JSONPath assertions immediately after the unauthenticated `/health` poll succeeds. Closes the class of regressions where a new table or `CREATE OR REPLACE VIEW` slips past unauthenticated `/health` and only fails when authenticated traffic hits. Each test takes `method`, `url` (absolute, or relative — joined onto `health_check.url`), `timeout`, `headers`, optional `body`, an `on_failure` policy (`rollback` default, `halt`, or `warn`), and a list of `assert` entries. JSONPath is the minimal `$.dotted.path` subset — no `$..foo` recursion, no array indexing, no wildcards (rejected at config-load time with a clear "use $.dotted.path only" message).
- **`fraisier/smoke_tests.py`** — `Assertion`, `SmokeTest`, `SmokeTestError`, `_walk_json_path`, `load_smoke_tests`, `run_smoke_tests`. HTTP via `httpx.Client`. `Authorization`, `Cookie`, and `X-API-Key` header values are redacted in log output.
- **`!envvar` YAML tag** in `fraises.yaml`. Values like `Authorization: !envvar SMOKE_TEST_JWT` are resolved from `os.environ` at config-load time; missing variables raise `ConfigurationError` immediately. Implemented as a `yaml.SafeLoader` subclass so safety guarantees are preserved.

### Upgrade notes

- Opt-in: a fraise without a `smoke_tests:` block is unchanged.
- `rollback` is the default `on_failure`: an authenticated probe that fails after `/health` passes is by definition a regression the unauthenticated probe missed — the safest default is to restore the previous version.
- `halt` is for cases where rolling back would be more disruptive than serving a partially-broken version (rare; typically only useful when the failure points to *external* state).
- Smoke tests run before the success result is constructed, so a rollback or halt failure is recorded as `status=failed` in `tb_deployment` with the smoke-test error message.
- See `fraises.example.yaml` for the config block shape.

## [0.20.0] - 2026-05-22

### Added

- **Post-migration SQL hooks** ([#204](https://github.com/fraiseql/fraisier/issues/204)). New `database.post_migrate:` list runs configurable SQL files between `confiture migrate up` and the service restart — typically the project's idempotent `db/7_grant/*.sql` sweep, but any cross-script reconciliation (post-migration ACL fixups, default-value sync) fits the slot. Each entry takes exactly one of `sql_dir` (runs all `*.sql` lexicographically) or `sql_file` (single file), plus an `on_error` knob (`halt`, default — raises `DeploymentError` and aborts before the service restart, so no rollback is needed; or `warn` — logs and continues). Closes the gap that lets a new table without grants slip past unauthenticated `/health` and fail under authenticated traffic.
- **`fraisier/post_migrate.py`** — `PostMigrateStep`, `load_post_migrate_steps(database_config, *, app_path)`, `run_post_migrate_steps(steps, *, database_url, runner)`. psql shellout uses `["psql", database_url, "-v", "ON_ERROR_STOP=1", "-f", str(sql_file)]`.

### Upgrade notes

- Opt-in: a fraise without a `database.post_migrate` block is unchanged.
- The hook runs **before** the service restart. A `halt` failure aborts the deploy with `status=failed` and leaves the previous service version serving — there is nothing to roll back because nothing was restarted yet.
- The SQL files run under the `database.database_url` role (the same role confiture uses for the migration). This is intentional: drift from a non-owner `CREATE OR REPLACE` is the specific bug the hook closes.
- See `fraises.example.yaml` for the config block shape.

## [0.19.1] - 2026-05-22

### Added

- **`fraisier trigger-deploy` prints the duration estimate before dispatching** ([#201](https://github.com/fraiseql/fraisier/issues/201) follow-up). Human operators now see `Estimated completion: ~Nm (history|fallback, N samples)` immediately above `Deployment triggered successfully`, closing the gap between the webhook surface (which already carried the estimate) and the CLI surface. Best-effort: any estimator failure (missing local fraisier DB, import error, etc.) is swallowed so the deploy still dispatches.
- **`tb_deployment.db_size_mb` is now populated** ([#201](https://github.com/fraiseql/fraisier/issues/201) follow-up). After each successful deploy with a configured `database.database_url`, fraisier samples `SELECT pg_database_size(current_database())` via `psql` and stores the byte count converted to MB. Lights up the fallback path of the duration estimator (which already knew how to use `db_size_mb` — it just got `None` before). Best-effort: psql failures, missing `database_url`, or unparseable output leave the column NULL. The estimator continues to work via its per-strategy floor — only the size-aware scaling is lost. Debug-level log entries carry a password-redacted form of the URL so misconfigured deploy roles are diagnosable from logs.

### Internal

- **Shared estimate helpers in `fraisier/duration_estimate.py`** — `build_estimate`, `to_dispatch_dict`, `format_estimate_line`. The webhook surface (`webhook._build_estimate`) and the new CLI surface both consume these; the previous in-module copy in `webhook.py` is gone.
- **`fraisier/dbops/sizing.py`** — `query_database_size_mb(database_url, *, runner)` standalone helper. One consumer (`_complete_db_record`); kept separate from `dbops/operations.py` for test focus.

### Upgrade notes

- No schema migration. The `db_size_mb` column already existed (introduced in v0.19.0); this release only changes the write path.
- The new sampling adds one `psql` round-trip per successful deploy. On databases with millions of relations `pg_database_size` can take 1-2 seconds; on typical sizes the cost is negligible against a multi-minute deploy.
- Deploy roles that cannot CONNECT to the **app DB** (only the maintenance DB) will see `db_size_mb` stay NULL silently. The estimator's fallback path still works, but the history-aware path never benefits. Check the debug log for `query_database_size_mb: psql failed` entries if estimates do not improve over time.

## [0.19.0] - 2026-05-21

### Added

- **Deployment duration estimates in webhook responses** ([#201](https://github.com/fraiseql/fraisier/issues/201)). Webhook dispatch responses now carry `estimated_duration_s`, `estimated_ready_at` (UTC), and `estimate_confidence` (`"history"` or `"fallback"`) for each triggered deployment that has a `database.strategy`. Agentic and human callers can use these to size their `sleep`/poll loops at trigger time instead of guessing.
- **`tb_deployment.strategy` + `tb_deployment.db_size_mb` columns**. Recorded per successful deploy via `complete_deployment(..., strategy=..., db_size_mb=...)`. Legacy installs are migrated in place via idempotent `ALTER TABLE ... ADD COLUMN`. ETL and docker_compose fraises (no database section) record `strategy=NULL` and are excluded from the estimator.
- **`FraisierDB.get_successful_deploy_durations(*, fraise, environment, strategy, limit)`**. New repository method consumed by the estimator; returns the most recent successful deploy durations filtered by the trinity, excluding NULL durations and non-success rows.
- **`fraisier/duration_estimate.py`** — `estimate_duration(db, *, fraise, environment, strategy, db_size_mb)` returns an `EstimateResult(seconds, confidence, samples_used)`. Uses the median of the most recent up-to-5 successful runs (with a 1.20 buffer) when ≥3 samples exist, otherwise falls back to a per-strategy seconds-per-MB rate clamped to a per-strategy floor (180s rebuild, 120s restore_migrate, 30s migrate, 60s for unknown strategies). Wrapped in `try/except` so a flaky history store cannot block a deploy.

### Upgrade notes

- Schema additions are additive; no migration required beyond starting the upgraded fraisier process (which runs the `ALTER TABLE` step on first connect).
- The CLI surface (`fraisier trigger-deploy` printing `Estimated completion: ~Nm`) is deferred to a follow-up PR — the webhook surface is the higher-value one for agentic callers and the CLI version can land independently once this lands.
- `db_size_mb` is reserved for future use; values are recorded as NULL today. Plumbing it through requires a `pg_database_size()` query before each deploy.

## [0.18.0] - 2026-05-21

### Added

- **Webhook self-upgrade when deployed pyproject pins a newer fraisier** ([#162](https://github.com/fraiseql/fraisier/issues/162)). After every successful deploy the webhook inspects the deployed `pyproject.toml`. If `[project].dependencies` (or any `[project.optional-dependencies.*]` group) contains an exact `fraisier==X.Y.Z` pin newer than the running webhook, fraisier detaches a worker that runs `uv tool install --force --refresh-package fraisier fraisier==X.Y.Z` against the webhook user's own uv tool directory, then asks the systemctl-helper socket to restart the webhook unit. The current deploy is unaffected — the upgrade applies to the next deploy. Range-pinned (`>=`, `~=`, `!=`) and unpinned dependencies are intentionally skipped. Complements the bootstrap-side restart already shipped in [#156](https://github.com/fraiseql/fraisier/issues/156) by covering the deploy-driven path (the operator-driven path was already covered).
- **`fraisier-{project}-webhook.service` in the systemctl-helper allowlist**. The self-upgrade above restarts the webhook via the existing root-privileged `fraisier-systemctl-helper.service` socket — the webhook process is not root, so it cannot `systemctl restart` itself directly. The helper rejects any service not in its compile-time allowlist; the webhook unit is now included by `_collect_allowed_services` in `fraisier/scaffold/renderer.py`.
- **`webhook.self_upgrade` config knob**. Default: `true`. Set `webhook: { self_upgrade: false }` in `fraises.yaml` to opt out per webhook.

### Upgrade notes

- Existing installations must re-scaffold and re-run `install.sh` to add the webhook unit to the systemctl-helper allowlist. Until they do, the install side of the self-upgrade succeeds but the restart RPC is rejected (logged at `error` level, deploy is not affected).
- Self-upgrade logs land in `/var/lib/fraisier/self-upgrade/{project}-{ts}.log` (one file per upgrade attempt). Inspect there when a self-upgrade does not seem to have taken effect.

## [0.17.0] - 2026-05-21

### Added

- **Parallel `pg_dump` via `backup.jobs`** ([#202](https://github.com/fraiseql/fraisier/issues/202)). Set `backup.jobs: N` in `fraises.yaml` or pass `--jobs N` to `fraisier db backup` to run pg_dump with N parallel workers. When `jobs == 1` (default), behaviour is byte-identical to today — a single-stream `pg_dump -Fc` producing one `.dump` file. When `jobs > 1`, fraisier switches to directory format (`pg_dump -Fd -j N`) producing a `<db>_<mode>_<ts>_<algo>.dump/` directory containing `toc.dat` plus per-table `*.dat` blobs. Parallelism comes from concurrent table COPYs.
- **`find_latest_backup` discovers directory-format dumps**. All restore consumers (the CLI restore path, `RestoreMigrateStrategy`) now transparently locate either form. `Path.glob` matched both already; the change is in behaviour lock-in and tests. `pg_restore` auto-detects `-Fd` from the positional directory path, so no caller needed adjusting.
- **`OnFailure=` hook on scaffolded backup units** ([#202](https://github.com/fraiseql/fraisier/issues/202) Phase 4). Closes the alerting gap that hid the original prod incident — when a backup exits non-zero (pg_dump SIGTERM, disk full, TOC verification rejection, size-sanity rejection), systemd now triggers `fraisier-<project>-backup-alert@%n.service`. The default alert is passive — pipes one line through `systemd-cat` into the journal. Operators who want real alerting can override the template with `systemctl edit fraisier-<project>-backup-alert@.service` or replace the file. fraisier ships no notifier choice to avoid lock-in.

### Changed

- **Size sanity check for directory dumps** uses recursive content size, not the directory inode's bare `stat().st_size` (~4096 bytes). Extracted as `_dump_size(path)` in `fraisier/dbops/backup.py`. Without this, a directory dump would falsely trigger the size check whenever a prior file dump existed.
- **`cleanup_old_backups` removes both file and directory dumps** via `shutil.rmtree` for directories, guarded by a resolved-path containment check against `backup_dir` so a glob result cannot escape via a symlinked entry.

### Upgrade notes

- Existing single-file `.dump` backups remain readable; nothing in the v0.16.x line is invalidated.
- Existing installations must re-run `install.sh` to pick up the new `fraisier-<project>-backup-alert@.service` unit and the `OnFailure=` line on the existing backup unit.
- Setting `backup.jobs > 1` requires that the database disk, the backup target disk, and the network between them can sustain N parallel reads/writes; higher `jobs` does not always mean faster. Tune to your hardware.
- The pre-deploy `BackupHook` (the `pg_dump | gzip > *.sql.gz` path in `fraisier/hooks/backup.py`) is intentionally unchanged — that code path is separate from `run_backup()` and does not benefit from parallelism in its current form.

## [0.16.6] - 2026-05-21

### Fixed

- **Scaffolded deploy service unit silently dropped `StrictHostKeyChecking`** ([#152](https://github.com/fraiseql/fraisier/issues/152)). The `Environment="GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new"` line was tokenised by systemd before quote handling, so `-o` was rejected with `Invalid environment assignment, ignoring: -o` and the option silently dropped — first-time `git fetch` then failed with SSH exit 255 (the symptom #116 had originally tried to address). The template now uses the equivalent no-space form `-oStrictHostKeyChecking=accept-new`; existing installations need to re-scaffold to pick up the fix.
- **Generated sudoers fragment ended with a double newline** ([#161](https://github.com/fraiseql/fraisier/issues/161)). `pre-commit-hooks/end-of-file-fixer` flagged the file on every commit. The blank-line separator between rules is now suppressed on the final iteration via `{% if not loop.last %}`, preserving readability between rules without trailing whitespace.
- **`fraisier sync` aborted with "nothing to commit" when conflicts auto-resolved back to source HEAD** ([#164](https://github.com/fraiseql/fraisier/issues/164)). The clean-merge path already guarded its pre-merge commit with `git diff --cached --quiet`; the conflict-resolution path did not. When every conflicted file was a fraisier-owned file that resolved to source HEAD (which was already the sync branch's tip), the staged index ended byte-identical to HEAD and `git commit --no-edit` exited non-zero. Both paths now route through a single `_commit_if_staged(message)` helper.

## [0.16.5] - 2026-05-21

### Fixed

- **`RebuildStrategy` / `RestoreMigrateStrategy` fail on re-deploy when template database already exists** ([#200](https://github.com/fraiseql/fraisier/issues/200)). Postgres refuses to drop a database with `datistemplate=true` (even `WITH (FORCE)`), so when `create_template=true` is configured, the drop step silently failed on every re-deploy and the subsequent `create_db` failed with "database already exists" until manual intervention. `drop_db` now accepts `clear_template_flag=True`, which issues `UPDATE pg_database SET datistemplate=false WHERE datname=...` before the drop. Both rebuild and restore-migrate strategies pass this flag when re-creating the template, and now check the drop return code so future failures surface immediately instead of cascading.

### Added

- **Post-dump backup verification** ([#202](https://github.com/fraiseql/fraisier/issues/202)). `run_backup()` now runs two cheap defences after `pg_dump` exits successfully:
  - **TOC integrity check** via `pg_restore --list <path>`. Reads the archive header only — no database connection needed — and rejects the backup if the TOC fails to parse. Catches the truncation pattern seen in the recent prod incident (pg_dump SIGTERMed mid-write, leaving the TOC intact but data blocks truncated).
  - **Size sanity check** vs the most recent same-mode dump in the output directory. A new dump under 50% of the previous one's size is rejected with a `BackupResult` that names the byte counts.
  Both checks shrink the silent-truncation window from "until someone tries to restore" down to "next backup attempt." Implements step 1 of #202; parallel `pg_dump -Fd -j N` support and systemd `OnFailure=` scaffold hooks (steps 2 and 3) follow separately.

## [0.16.4] - 2026-05-19

### Added

- **Template database version stamping** ([#198](https://github.com/fraiseql/fraisier/issues/198)). `RebuildStrategy` now writes a build-time version stamp into the source database's `public.tb_version.app_version` immediately before cloning the template. The atomic `CREATE DATABASE … TEMPLATE …` carries the stamp into the template, so downstream consumers (e.g. a reseed endpoint) can read `tb_version.app_version` from the template and reject reseeds from stale templates.
  - The version is auto-discovered from `<project>/version.json` (preferred) or `<project>/pyproject.toml`. No `fraises.yaml` change is required for standard projects.
  - New optional `database.app_version` key in `fraises.yaml` to override auto-discovery. Invalid values (anything outside `[A-Za-z0-9._+\-]`, including PEP 440 epoch versions like `1!2.3.4`) cause `RebuildStrategy` to fail loudly at construction with a `ValueError` so typos do not silently produce unstamped templates.
  - The stamp is best-effort: if no version is resolvable, if `tb_version` is missing, or if psql returns a non-zero exit, fraisier logs a warning and the rebuild succeeds normally — the protection is fail-safe on the consumer side.
  - **Requires `public.tb_version` to contain at least one row.** The UPDATE has no WHERE clause and would silently affect zero rows on an empty table; fraisier detects this case (`"UPDATE 0"` in psql stdout) and emits a distinct warning ("`tb_version` is empty; template will be unstamped") instead of logging false-positive success. Projects must seed `tb_version` with one row as part of their schema for the stamp to take effect.
  - The race against a reconnecting app overwriting the stamp between commit and clone is closed by a `terminate_backends → stamp → terminate_backends → create_db(template, template=source)` window inside the rebuild's `create_template` block. `CREATE DATABASE … TEMPLATE …` itself fails closed if any backend slipped through.

### Upgrade notes

- Templates created by earlier fraisier versions are not stamped. Consumers that strictly reject unstamped templates will refuse reseeds from these pre-existing templates until the next rebuild. Operators should either trigger a rebuild after upgrade, or implement a grace period in their consumer that treats a missing stamp as "unknown" rather than "stale" during the cutover.

## [0.16.3] - 2026-05-18

### Fixed

- **`gateway.conf` crashes nginx in multi-server setups** ([#197](https://github.com/fraiseql/fraisier/issues/197)). When per-environment nginx configs exist (`gateway_env.conf`), `gateway.conf` no longer emits environment-specific HTTPS `server {}` blocks with SSL certificate paths that may not exist on every machine. It now contains only shared directives (`limit_req_zone`, HTTP catch-all) so it can be safely installed unconditionally on all servers. The legacy single-server path (no per-env nginx) is unchanged.

## [0.16.2] - 2026-05-16

### Added

- **`create_template` support in `RebuildStrategy`**. Set `create_template: true` (and optionally `template_name: <name>`) in fraises.yaml under `database:` to snapshot the freshly-rebuilt database as a PostgreSQL template database after every rebuild. The default template name is `template_<db_name>`. This allows downstream tooling (e.g. a reseed endpoint) to restore from the template using `CREATE DATABASE … TEMPLATE …` for a fast, schema-current reset without re-running the full rebuild.

## [0.16.1] - 2026-05-16

### Fixed

- **`uv sync` fails when root-owned `__pycache__` dirs exist in app venv** ([#196](https://github.com/fraiseql/fraisier/issues/196)). Three-layer fix: (1) `fraisier/__init__.py` now sets `sys.dont_write_bytecode = True` and `PYTHONDONTWRITEBYTECODE=1` at import time to prevent future `__pycache__` creation regardless of invocation context; (2) `install.sh` cleans up existing root-owned `__pycache__` directories in app venvs and the deploy user's tool install dir on every scaffold run; (3) the install helper and deployer now detect the `__pycache__` permission pattern and provide actionable advice in the error message.

## [0.16.0] - 2026-05-11

### Added

- **Parallel `pg_restore` via `restore.jobs`** ([#195](https://github.com/fraiseql/fraisier/issues/195)). Set `restore.jobs: N` in fraises.yaml or pass `--jobs N` on the CLI to run pg_restore with `-j N` for parallel restore. Default is 1 (no change in behavior).

- **Compression preference for backup selection** ([#195](https://github.com/fraiseql/fraisier/issues/195)). Set `restore.preferred_compression: lz4` (or `zstd`, `gzip`) to prefer backups compressed with a faster algorithm. Falls back to the newest backup if no match is found. Also pass `--preferred-compression` on the CLI.

- **Compression algorithm in backup filenames** ([#195](https://github.com/fraiseql/fraisier/issues/195)). `run_backup()` now encodes the compression algorithm in the dump filename (e.g. `mydb_full_20260511_0030_lz4.dump`), enabling compression-aware backup selection.

- **Restore timing observability** ([#195](https://github.com/fraiseql/fraisier/issues/195)). The restore pipeline now tracks per-phase timing (pg_restore, migration, total). Durations are logged, returned in `StrategyResult`, printed by the CLI after restore, and recorded in the `fraisier_restore_duration_seconds` Prometheus histogram.

## [0.15.0] - 2026-05-05

### Added

- **SSH dispatch for `history`, `rollback`, and `stats`** ([#194](https://github.com/fraiseql/fraisier/issues/194)). When an environment is SSH-configured, these commands now fetch data from the remote server's database instead of reading the local (nearly empty) database. The remote invocation is controlled by two new optional SSH config fields: `ssh.db_path` (sets `FRAISIER_DB_PATH` on the remote) and `ssh.fraisier_bin` (path to the remote executable, default `"fraisier"`).
- `stats` command gains `--env/-e` flag to filter by environment and `--json` flag for structured output.
- `webhooks` command prints a `Note: Showing local webhook events only.` notice to set expectations when SSH-configured environments exist.

## [0.14.3] - 2026-05-05

### Security

- **`fraisier-install-helper` accepted arbitrary commands with no allowlist** (adversarial review). Any caller with write access to the Unix socket could invoke any subprocess as the privileged deploy user. Fixed by baking the allowed command into the systemd unit at scaffold render time (`ExecStart=fraisier-install-helper uv sync --frozen`) and enforcing an exact-match check in the handler. Requests with a mismatched command are rejected with a structured error response.

- **Shell injection in `_regenerate_scaffold` and `_install_scaffold`** (adversarial review). Both methods built `sh -c "cd {project_dir} && {fraisier_exe} ..."` using f-strings with unquoted paths. A project directory or executable path containing shell metacharacters would be executed verbatim. Fixed by replacing the shell invocation with `runner.run([...], cwd=str(project_dir))`, eliminating the shell entirely.

- **`_DeliveryDedupe` webhook replay store was not thread-safe** (adversarial review). Concurrent webhook requests could race on `_store` dict mutation, causing missed deduplication or corruption. Fixed by adding a `threading.Lock()` around all read/write access.

### Fixed

- **`is_deployment_locked` had a TOCTOU race** (adversarial review). An `exists()` pre-check before `open()` could return `False` for a directory that was created between the check and the open, or `True` for a file deleted in the same window. Removed the pre-check entirely; `FileNotFoundError` from `open()` is now caught and returns `False`.

- **`mkstemp()` file descriptor leak in `status.py`** (adversarial review). `tempfile.mkstemp()` returns an OS-level fd that must be closed with `os.close()` before `Path.write_text()` opens the same path. Fixed by adding `os.close(_fd)` immediately after `mkstemp()`.

- **Remote tmp dir collision in `_upload_tree_with_password`** (adversarial review). The hardcoded path `/tmp/.fraisier-upload-tree` would be shared by concurrent uploads to the same host. Fixed by appending a 12-character random hex suffix (`uuid4`). Added try/except to clean up the remote dir if the tar step fails.

- **Jinja2 context mutation without cleanup in `_render_scaffold_install_helper`** (adversarial review). A `KeyError` or render failure after `self.context["scaffold_install_script"]` was set left the renderer in dirty state for subsequent calls. Fixed by wrapping the mutation in `try/finally` so the key is always deleted.

- **`systemctl enable --now` does not reload an already-running unit** (adversarial review). `install.sh` used `enable --now`, which starts a stopped unit but does not restart a running one after the unit file changes. Split into `systemctl enable` + `systemctl restart` so re-running install always picks up changes.

- **Starlette `QueryParams` private dict mutated in legacy webhook route** (adversarial review). The `github_webhook` compatibility endpoint set `request._query_params` to a plain `dict`, but Starlette's routing layer expects a `QueryParams` instance. Fixed by assigning `QueryParams("provider=github")`.

- **`_try_scaffold_install_via_socket` swallowed `json.JSONDecodeError` and `UnicodeDecodeError`** (adversarial review). A malformed response from the helper socket would propagate as an unhandled exception rather than triggering the sudo fallback. Added both error types to the `except` clause.

## [0.14.2] - 2026-05-05

### Fixed

- **`fraisier sync` aborts when pre-commit hooks modify auto-resolved files** ([#192](https://github.com/fraiseql/fraisier/issues/192)). The two internal bookkeeping commits made during a sync operation (`Pre-merge <tgt> into sync branch`) were invoked without `--no-verify`, so hooks such as `end-of-file-fixer` or `trailing-whitespace` could modify a resolved file and exit with code 1, causing fraisier to abort and clean up the branch. Fixed by adding `--no-verify` to both commit calls — these commits are fraisier internals, not user code, and must never trigger user-configured hooks.

## [0.14.1] - 2026-05-04

### Fixed

- **`confiture migrate preflight` fails with `FileNotFoundError` when confiture is installed in a venv** ([#190](https://github.com/fraiseql/fraisier/issues/190)). `_run_confiture_preflight` invoked `confiture` as a bare subprocess command, which requires the binary to be on `PATH`. When fraisier and confiture are installed together via `uv sync` (not `uv tool install confiture`), the binary lives in the venv's `bin/` directory rather than on `PATH`. Fixed by resolving the executable relative to `sys.executable` (`Path(sys.executable).parent / "confiture"`), which is always correct regardless of how the venv is activated.

## [0.14.0] - 2026-04-29

### Added

- **Migration preflight for `RestoreMigrateStrategy`** ([#187](https://github.com/fraiseql/fraisier/issues/187), [#188](https://github.com/fraiseql/fraisier/issues/188), [#189](https://github.com/fraiseql/fraisier/issues/189)). Before the expensive `pg_restore`, fraisier now optionally runs all pending migrations against a schema-only copy of the backup — using `confiture migrate preflight --against` (requires fraiseql-confiture ≥ 0.9.4). Each migration executes inside a SAVEPOINT that is always rolled back; the original database is never touched. A structured `MigrationPreflightResult` with per-migration pass/fail details is returned. On failure, a `MigrationPreflightError` (recoverable, code `MIGRATION_PREFLIGHT_FAILED`) is raised before any destructive operation begins.

  Key details:
  - Controlled via `preflight:` block in `fraises.yaml` (`enabled`, `timeout_seconds`); enabled by default for `restore_migrate` strategy.
  - `RestoreMigrateStrategy.execute()` gains a `skip_preflight=True` escape hatch.
  - New CLI command `fraisier db preflight <fraise> -e <env>` runs the check standalone with text or JSON output.
  - `PreflightConfig` replaces the old `preflight_enabled: bool` field on `RestoreConfig`.
  - Bumped `fraiseql-confiture` dependency to `>=0.9.4` to pick up `--against` support.

## [0.13.3] - 2026-04-29

### Fixed

- **Restore strategy fails when deploy user has no matching PostgreSQL database** ([#185](https://github.com/fraiseql/fraisier/issues/185)). `_pg_cmd` silently dropped the database path from `connection_url`, so `terminate_backends`, `drop_db`, `check_db_exists`, `create_db`, and `prune_templates` connected to a database named after the unix user instead of the maintenance database. Fixed by extracting the URL's database path and injecting `-d` (for psql/pg_restore) or `--maintenance-db` (for createdb/dropdb) when the caller hasn't already specified one. `admin_url` must now include an explicit maintenance database path (e.g. `/postgres`).

- **Resolved all `ty` type-checking errors** across `bootstrap_freebsd.py`, `service_managers/systemd.py`, and several test files.

## [0.13.2] - 2026-04-27

### Fixed

- **`trigger-deploy` path fails: `sudo -u <app_user> uv sync` cannot find `uv`** ([#184](https://github.com/evoludigit/fraisier/issues/184)). When the socket-activated install helper is unavailable, the fallback `sudo -u` path ran the install command with a bare binary name (e.g. `uv`). Since `sudo` resets `PATH`, per-user installations of `uv` (e.g. `~/.local/bin/uv`) were not found. Fixed by resolving the binary to an absolute path via `shutil.which()` before constructing the `sudo` command. Falls back gracefully to the bare name if resolution fails.

## [0.13.1] - 2026-04-27

### Fixed

- **`is_active()` crashes when service is already stopped (exit code 3)** ([#183](https://github.com/fraiseql/fraisier/issues/183)). `_call_via_socket()` raised `CalledProcessError` for any non-zero exit code, but `systemctl is-active` returns exit code 3 for inactive services. The `check` parameter was not forwarded to the socket path, so `status()` (which passes `check=False`) still crashed. Fixed by adding a `check` parameter to `_call_via_socket()` and forwarding it from `_run_systemctl()`. This unblocks all automated restore-migrate deployments when the target service happens to be stopped.

## [0.11.1] - 2026-04-21

### Fixed

- **Nginx config not regenerated when `fraises.yaml` scaffold configuration changed** ([#176](https://github.com/evoludigit/fraisier/issues/176)). The `ConfigWatcher.save_hash()` method was never called after scaffold regeneration, so the `.config_hash` file was never written. This caused either unnecessary regeneration on every deployment, or (if the hash file existed from a previous operation) changes were never detected. Additionally, scaffold install failures were silently logged as warnings, leaving nginx unreloaded with no visible indication of failure. Fixed by:
  - Calling `save_hash()` after successful scaffold regeneration + install to persist state across deployments
  - Upgrading error logging from `logger.warning` to `logger.error` with helpful hints for manual recovery
  - Removing `# pragma: no cover` annotations from scaffold methods so they're properly tested

## [0.11.0] - 2026-04-20

### Added

- **`--prefer-source` flag for `fraisier sync` command** — auto-resolves sync conflicts by preferring the source (upstream/target-branch) when the current branch is behind. Useful for unattended sync workflows where local changes should be discarded in favor of the upstream version.

## [0.10.0] - 2026-04-18

### Added

- **`fraisier db exec` — run read-only SQL on any fraise, locally or over SSH.** New CLI command and supporting `fraisier.dbops.exec` module providing:
  - `is_readonly_sql()` — strips leading comments and validates the first keyword against a safe allowlist (`SELECT`, `EXPLAIN`, `SHOW`, `WITH`, `TABLE`). Rejects all DDL/DML (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `CREATE`).
  - `build_psql_argv()` — constructs a `psql --no-psqlrc` invocation with `SET statement_timeout` injected, supporting `table` (default), `--csv`, and `--json` output formats.
  - Runs locally via `subprocess.run` when no `ssh` block is configured, or tunnels through `fraisier.ssh.short_cmd` when an SSH target is present — same code path, zero duplication.
  - Production safety gate: prompts for confirmation when `--env production` is passed.
  - `--timeout` (default 30 s) with validation (must be > 0).
  - `--file` flag to read SQL from a file; mutually exclusive with the inline SQL argument.
  - Accepts a plain database name or a full `postgresql://` URL from `database.admin_url`.

## [0.8.5] - 2026-04-17

### Fixed
- **`uv tool install --force` fails with Permission denied on __pycache__ in tool environment** (#167). Python creates root-owned `__pycache__/` directories inside the deploy user's tool installation when the systemctl helper runs as root, causing reinstalls to fail. Four service templates (deploy-service.j2, fraisier-webhook.service.j2, poll-deploy.service.j2, restore-staging.service.j2) now set `PYTHONDONTWRITEBYTECODE=1`, consistent with the existing fix in systemctl-helper.service.j2 and install-helper.service.j2.

## [0.8.4] - 2026-04-17

### Fixed
- **Restore strategy fails with "database already exists" on re-deploy** ([#166](https://github.com/evoludigit/fraisier/issues/166)). The `drop_db` return code was silently ignored: if `dropdb` failed (e.g. a connection reconnected in the window between `pg_terminate_backend` and the drop), execution continued to `createdb`, which then failed with a confusing "already exists" error. The return code is now checked and raises `DatabaseError` immediately. `dropdb` also uses `--if-exists` so a first deploy (database absent) is handled cleanly.

## [0.8.3] - 2026-04-14

### Fixed
- **Zombie uvicorn workers surviving service restart** ([#165](https://github.com/evoludigit/fraisier/issues/165)). Generated systemd units now include `KillMode=control-group` and `TimeoutStopSec=10`, ensuring all worker processes are terminated on stop within a bounded window. Previously, orphaned workers could hold the listening port and cause `Address already in use` crash loops on fast restarts.
- **Default `service.type` changed from `notify` to `exec`.** uvicorn does not implement `sd_notify`, so `Type=notify` confused systemd's lifecycle tracking during stop. `Type=exec` reflects actual server behavior. Existing configs that explicitly set `service.type` are unaffected; configs relying on the default will now get `Type=exec`.

## [0.8.2] - 2026-04-11

### Fixed
- `install-helper` systemd template now allows `AF_INET`/`AF_INET6` so `uv sync` can reach PyPI when the local cache is cold.

## [0.8.1] - 2026-04-11

### Security
- SQL identifier validation in `strategies._provision_roles` — role and database names are now validated against an allowlist before being interpolated into SQL.
- Webhook replay protection via `X-GitHub-Delivery` deduplication — duplicate delivery IDs within a sliding window are rejected.

### Improved
- Replaced 10 unnamed `except Exception:` clauses with named, narrowed handlers across CLI, config, and strategies modules.
- Health check failures now include the check name in log output for easier diagnosis.
- Type annotations in refactored modules (`cli/`, `config/`, `strategies/`) and `webhook.py` — `Any` usage reduced from 52 to ≤20 files.

### Refactored
- `cli/main.py`, `strategies.py`, and `config/loader.py` split into focused sub-modules.
- Timeout constants centralized in `fraisier/constants.py`.

### Tests
- Coverage extended to the CLI module; unjustified `# pragma: no cover` markers removed.

### Known follow-up
- ~100 `except Exception as e:` broad handlers across `strategies/`, `providers/`, `deployers/`, and `health_check.py` still need a focused audit. Deferred to a future remediation round.

## [0.8.0] - 2026-04-11

### Changed — Breaking

- **Privilege model collapsed to `admin_url` only.** Strategies that perform privileged database operations (`rebuild`, `restore_migrate`) now require a superuser connection string in `database.admin_url`. Fraisier no longer falls back to `sudo -u postgres`, generates a `pg-wrapper.sh` script, emits a `(postgres) NOPASSWD:` sudoers rule, or injects `FRAISIER_PG_WRAPPER` into the generated systemd units. The runtime probes that previously tried to detect a broken sudo+`NoNewPrivileges=true` combination are gone — there is no sudo path left to probe. `validate-deployment` / `validate-remote` check only the systemctl wrapper. `fraisier test-wrapper` only accepts `systemctl` as its wrapper type.

  **Migration.** Set `database.admin_url` on every environment using `rebuild` or `restore_migrate`. The recommended form is peer-auth over the Unix socket:

  ```yaml
  database:
    strategy: rebuild
    admin_url: "postgresql:///postgres?host=/var/run/postgresql"
  ```

  Or via environment variable:

  ```yaml
  database:
    strategy: rebuild
    admin_url: "${PG_ADMIN_URL}"
  ```

  **Backups are unaffected.** Backups now run `pg_dump` against the fraise's own `database_url` instead of `sudo -u postgres`. `pg_dump` only needs SELECT on the app's own tables, which `database_url` already provides. No action required if `database_url` is already set — and it is, for every fraise with a database.

  **Post-upgrade cleanup.** After re-running `fraisier scaffold` and `fraisier scaffold-install`, the `pg-wrapper` script and its sudoers rule are no longer generated, but the old files remain on disk. Remove them to close the residual privilege:

  ```bash
  sudo rm -f /usr/local/libexec/fraisier/pgadmin-<project>
  sudo rm -f /etc/sudoers.d/<project>-pg-wrapper
  ```

  (Substitute `<project>` with your scaffold `project_name`.)

### Fixed

- **Unified SSH invocation path.** `runners.py` SSH commands now include the same `ConnectTimeout` and `BatchMode` defaults as `fraisier logs`, fixing a latent hang on IPv6-only networks. All subprocess-based SSH calls now route through `fraisier.ssh`, which applies the full defensive flag set (`BatchMode=yes`, `ConnectTimeout=30`, `StrictHostKeyChecking=accept-new`, `-n` where appropriate) by construction.

---

## [0.7.12] - 2026-04-10

### Fixed

- **Issue #156: bootstrap restarts the webhook service after a fraisier self-upgrade** — `fraisier bootstrap` now runs a `systemctl restart fraisier-{project}-webhook.service` step immediately after upgrading fraisier, so the new binary takes effect without manual intervention. If the service is not yet running (fresh install), the step is a no-op.

---

## [0.7.11] - 2026-04-10

### Fixed

- **Issue #153: deploy daemon no longer errors on config sync when `/opt/fraisier/` is absent** — `_sync_fraises_yaml` now runs `mkdir -p` on the destination directory before `cp`, so the first deployment on a freshly bootstrapped server succeeds silently instead of logging a recurring "Config sync failed" warning.

---

## [0.7.5] - 2026-04-10

### Fixed

- **`fraisier logs` works correctly from background processes and scripts** — Replaced `os.execvp` with `subprocess.Popen` (inherited stdin/stdout/stderr). TTY behaviour is identical for interactive use (colours, terminal size, Ctrl-C), but the Python process now stays alive so background runners and scripts can track the PID properly.
- **`fraisier trigger-deploy --follow` was silently broken** — The `--follow` path imported `_resolve_deploy_unit_pattern` by its old name after the #154 refactor, causing a silent `ImportError` and never exec-ing into journalctl. Fixed by reusing the `socket_stem` already computed just above the call site.

---

## [0.7.4] - 2026-04-10

### Added

- **Issue #154: `fraisier logs` fetches logs from remote servers** — `fraisier logs <fraise> <env>` now detects whether the target environment is remote (has an `ssh:` block in its config) and SSHs to the target server to run `journalctl` there instead of querying the local machine. `os.execvp` is used so the TTY is inherited and `--follow` / Ctrl-C work correctly over SSH. A new `--service [app|deploy]` option (default: `deploy`) lets operators switch between the deploy-daemon template instances and the main app service. Unit pattern generation now uses the same naming logic as the scaffold (`deploy_socket_name` / `app_service_name`), so patterns always match installed units. `app_service_name()` is extracted to `fraisier.naming` as the single source of truth; the scaffold renderer delegates to it. The `ssh:` block is now validated by the config loader (`host` required, typed fields enforced).

---

## [0.7.3] - 2026-04-10

### Fixed

- **Issue #151: `fraisier sync` auto-resolves file deletions from the source branch** — When the source branch has deleted a file that still exists in the target branch, the sync now detects the deletion (via `git cat-file -e origin/{source}:{file}`) and accepts it with `git rm`, instead of halting with an unresolved conflict error. A log line is printed for each auto-resolved deletion. Conflicts where both sides modified the same file are still surfaced to the user.

---

## [0.7.2] - 2026-04-09

### Fixed

- **Issue #149: stale deploy socket/service files not cleaned up on re-scaffold** — `ScaffoldRenderer` now removes `fraisier-*.socket` and `fraisier-*@.service` files from the `systemd/` output directory that are no longer part of the current render set (e.g. after a fraise rename or the 0.7.1 socket naming change).
- **Issue #150: install.sh migrates pre-0.7.1 generic deploy socket units** — `install.sh` now detects and removes stale `fraisier-{env}.socket` / `fraisier-{env}@.service` / `fraisier-{env}.service` units left by older scaffold versions before installing the new fraise-specific units, preventing duplicate or conflicting unit names on the server.

---

## [0.7.1] - 2026-04-09

### Fixed

- **Issue: deploy socket name collision with multiple fraises** — `deploy_socket_name()` now includes the fraise name in the default unit name: `fraisier-{fraise_name}-{env_key}.socket`. Previously, multiple fraises sharing the same env key (e.g. `production`) generated the same socket unit name, causing last-write-wins overwrites. Resolution order: `systemd_deploy_socket` override → `fraisier-{env.name}.socket` (if `env.name` set) → `fraisier-{fraise}-{env}.socket` (new default) → `fraisier-{env}.socket` (legacy fallback).

---

## [0.7.0] - 2026-04-09

### Added

- **`fraisier sync` CLI command (#140)** — Replaces the `sync.sh` scaffold template. `fraisier sync [pair]` fetches the source branch, detects drift, creates a squash sync branch, auto-resolves fraisier-owned files on merge conflict, and opens a PR with auto-merge enabled. Supports `--list`, `--check` (drift detection only), `--dry-run`, and `--yes` flags.

### Fixed

- **Issue #148: nginx configs always generated for all servers** — `fraisier scaffold` (with or without `--server`) now generates per-environment nginx configs for every server, not just the local one. `scripts/generated/nginx/` is always a complete, committable artifact; each server's `install.sh` uses `_env_active()` to install only its own configs.
- **Issue #147: CORS wildcard regex anchored and dot-escaped** — Wildcard patterns (e.g. `https://*.example.com`) now produce correctly anchored nginx regexes: dots in the domain are escaped before `*` is expanded, and `^`/`$` anchors are added.
- **Issue #145: legacy rate_limit.conf removed before nginx reload** — `install.sh` now deletes `/etc/nginx/conf.d/rate_limit.conf` before reloading nginx when `gateway.conf` owns `limit_req_zone`, preventing duplicate-directive errors on servers upgraded from older fraisier versions.
- **Issue #144: install.sh SCAFFOLD_DIR always derived from script location** — The `SCAFFOLD_DIR` default no longer depends on the `STANDALONE` flag; it is always inferred from the script's own path, fixing silent no-ops when run outside the standard directory.

### Removed

- **`sync.sh.j2` scaffold template** — Replaced by the `fraisier sync` CLI command. Projects that referenced a generated `sync.sh` should switch to `fraisier sync`.

---

## [0.6.0] - 2026-04-08

### Breaking Changes

- **CORS origins configuration format changed** — `cors_origins` now requires structured objects. Plain strings are no longer accepted. Update `["https://*.example.com", "https://api.example.com"]` to `[{"pattern": "https://*.example.com", "type": "wildcard"}, {"pattern": "https://api.example.com", "type": "literal"}]`.

### Added

- **Structured CORS origins with explicit types** — `cors_origins` entries now support:
  - `"type": "literal"` — Escapes dots for nginx regex (e.g., `https://api.example.com` → `~https://api\.example\.com`)
  - `"type": "wildcard"` — Converts `*` to subdomain regex (e.g., `*.example.com` → `~https://[a-zA-Z0-9-]+\.example\.com`)
  - `"type": "regex"` — Raw nginx regex without processing (e.g., `^https://.*\.custom\.com$`)
- **Conditional WatchdogSec support** — `service.watchdog_sec` field enables `WatchdogSec` in systemd service units for servers that support `sd_notify` (e.g., gunicorn). Defaults to disabled for uvicorn compatibility.
- **Server type configuration** — `service.server_type` field (`"uvicorn"` or `"gunicorn"`) for future server-specific optimizations.

- **Socket-based install helper (#124)** — New `fraisier-install-helper` daemon (socket + service systemd units) runs as `install_user`, accepts install commands over a Unix socket owned by `deploy_user`. Eliminates the `sudo -u` dependency at deploy time. `fraisier-webhook` emits `FRAISIER_INSTALL_SOCKET_*` env vars; `install.sh` installs and enables the units automatically.
- **Scaffold sync promotion scripts (#139)** — New `sync.sh` scaffold template generates a git-based promotion script per `scaffold.sync` pair defined in `fraises.yaml`. The script fetches, detects drift, creates a sync branch, pre-merges the target, auto-resolves fraisier-owned files, and opens a PR via `gh`.
- **Stale service file warning at deploy time (#142)** — `fraisier deploy` now warns when the live systemd service file differs from what fraisier would generate, prompting a `fraisier scaffold` run before the next deploy.

### Fixed

- **Issue #138: nginx CORS map supports wildcard subdomain patterns** — Wildcard patterns like `https://*.example.com` now generate proper nginx regex patterns.
- **Issue #137: WatchdogSec properly omitted for non-sd_notify servers** — WatchdogSec is now conditionally included only when configured, preventing uvicorn services from being killed every 60 seconds.
- **Issue #127: gateway.conf spurious SSL catch-all block suppressed** — `server_name` is now aggregated across per-environment nginx blocks when not set at the fraise level, so `has_server_names` is correctly `True` and the `server_name _` SSL catch-all is omitted.

---

## [0.5.28] - 2026-04-07

### Added

- **`fraisier health --width` option** — Table now expands to terminal width by default (`os.get_terminal_size()`), eliminating column truncation. Pass `--width N` to override. Falls back to 120 when terminal size is unavailable (e.g. CI/piped output).

## [0.5.27] - 2026-04-07

### Added

- **Custom per-environment health endpoints with headers and field mappings (#136)** — `fraisier health` now uses the full `health_check.url` from each environment config as-is (no path stripping). Optional `health_check.headers` dict allows authenticated endpoints. Optional per-environment `version_field` and `migration_field` override the global defaults with dot-notation path support (e.g. `versions.app`). Falls back to global `health.version_field` / `health.migration_field` when not set per-environment.

### Fixed

- **pytest-asyncio missing from dev dependency group** — `pytest-asyncio` was declared in `[project.optional-dependencies]` but not in `[dependency-groups]`, causing async tests to fail silently after `uv sync`.

## [0.5.25] - 2026-04-07

### Fixed

- **fraisier health only shows last environment when multiple environments share the same fraise name (#130)** — The services dict used `fraise_name` as key, causing overwrites for each environment iteration. Changed to use composite key `f"{fraise_name}-{env_name}"` so all environments appear in the health table.

- **fraisier health URL column strips scheme for remote services (#131)** — The URL display logic incorrectly split on `:` and took the last part, dropping `https://` for remote URLs. Now displays the full base URL.

- **fraisier health table lacks Environment column (#132)** — The table merged fraise name and environment into a single Service column. Added a dedicated Environment column for clarity, with Service showing only the fraise name.

- **fraisier health does not display migration number** — Added support for parsing and displaying migration information from health endpoint responses. Added `include_migration` config option and Migration column in health table.

## [0.5.24] - 2026-04-07

### Added

- **`fraisier db restore` command (#129)** — Expose the staging restore workflow
  (pg_restore → rollback template → migrate → validate) as a user-facing command. The
  nightly `restore-staging-from-production-backup.service` systemd unit previously called
  a broken `trigger-deploy` path and did nothing useful; it now calls
  `fraisier db restore api staging` and works correctly. Supports `--from-backup` to restore
  from a specific dump file (useful for testing), `--dry-run` to preview the plan, and
  `--no-service-restart` to skip service management if needed.

- **Scaffold templates for staging restore** — Generated `restore-staging.service` and
  `restore-staging.timer` systemd units for nightly restores. Auto-discovers fraises with
  `restore_migrate` strategy and generates correct `fraisier db restore` commands.
  When running `fraisier scaffold-install --apply` on existing servers, the broken service
  unit is automatically replaced with the corrected one.
  Conditionally rendered only when `restore_migrate` is configured to avoid invalid
  systemd units in configs without staging restore.

- **CLI consistency** — All fraise database commands now use positional `ENVIRONMENT`
  arguments instead of flags (e.g., `fraisier db restore api staging` instead of
  `fraisier db restore api -e staging`), matching the pattern of `trigger-deploy`, `logs`,
  and other top-level commands.

### Fixed

- **Server-scoped scaffold collectors** — `pg_allowed_databases`, `allowed_services`, and
  `has_database` now use server-filtered fraises (`local_fraises`) instead of the unfiltered
  global list. Previously, rendering with `--server` included databases and services from
  all servers, causing cross-server leakage in generated pg-wrapper and systemctl-wrapper
  allowlists.

- **ReadWritePaths use configured `app_path`** — `restore-staging.service` and
  `poll-deploy.service` templates now derive `ReadWritePaths` from the actual `app_path`
  in environment config instead of hardcoded `/opt/<fraise_name>`. restore-staging further
  restricts paths to environments with `restore_migrate` strategy.

- **Socket-based systemctl for restore and poll-deploy** — `restore-staging.service` and
  `poll-deploy.service` now use `FRAISIER_SYSTEMCTL_SOCKET` (the socket-activated helper
  running as root) instead of the legacy `FRAISIER_SYSTEMCTL_WRAPPER` which lacked
  privilege escalation inside systemd's security namespace.

- **CWD-independent `db restore` migrations** — Relative `confiture_config` paths (default:
  `confiture.yaml`) are now resolved against `app_path` before being passed to confiture.
  Previously, migrations failed when the service's working directory was not the app
  directory, because confiture could not find `db/environments/<env>.yaml` relative to CWD.

- **Deduplicate ReadWritePaths in service templates** — `poll-deploy.service` and
  `restore-staging.service` now emit each `app_path` once even when multiple fraises
  share the same path on a server.

- **Deduplicate sudoers pg-wrapper rules** — The sudoers fragment now emits each
  `(user, pg-wrapper)` rule once regardless of how many environments use admin
  strategies. Also scoped to `local_fraises` to avoid cross-server leakage.

---

## [0.5.17] - 2026-04-06

### Fixed

- **Multi-server `install.sh` silent overwrites (#125)** — Running `fraisier scaffold --server A`
  then `--server B` silently overwrote `install.sh` with B's config, corrupting deployments on
  server A. The problem occurred because `install.sh` was rendered per-server at scaffold time,
  and successive runs overwrote the same output file. Fixed by adding runtime server detection:
  `install.sh` now detects the current machine's hostname at startup and conditionally installs
  only the units relevant to that machine. Requires new `servers:` section in `fraises.yaml`
  mapping logical server hostnames to machine hostnames (e.g., `prod.example.com` → 
  `[backend-prod-01, backend-prod-02]`). Single `install.sh` now works across the entire cluster
  with zero ambiguity.

---

## [0.5.16] - 2026-04-06

### Fixed

- **`poll-deploy.service.j2` deploys wrong fraise on wrong environment** — The service
  hardcoded `{{ fraise_names | first }} production`, so only the first fraise was polled and
  the environment was always `production` regardless of which server the unit ran on. The
  service now loops over `local_fraises` (server-filtered) and generates one `ExecStart`
  line per (fraise, environment) pair. The binary path was also wrong (`/usr/local/bin/fraisier`
  instead of the deploy user's uv-tool path) and is now consistent with all other templates.

- **`ScaffoldConfig` missing `socket_user`/`socket_group` fields** — The deploy-socket
  template referenced `scaffold.socket_user` and `scaffold.socket_group` via Jinja
  `|default('www-data')` filters, making them silently unconfigurable. Both fields are now
  declared in `ScaffoldConfig` with `"www-data"` as the default.

- **Bootstrap uploads scaffold without checking for placeholder files** — When a template
  file is missing from the package, `ScaffoldRenderer` writes a `# Placeholder:` stub. The
  bootstrap step now inspects each rendered file and fails early with a clear error rather
  than uploading broken stubs that produce confusing failures on the remote server.

- **Config module refactor lint cleanup** — The `config/` package re-export module had
  stale unused imports (`_config`, `_config_lock` without `noqa`), an unsorted `__all__`,
  and test files had `noqa` directives that were no longer needed after adding `F401` to the
  test ignore list. All cleaned up.

---

## [0.5.15] - 2026-04-06

### Fixed

- **Watchdog kills uvicorn-based services every 60 seconds** — `service.j2` unconditionally
  added `WatchdogSec=60` but uvicorn does not implement `sd_notify` keepalive pings, so
  systemd would kill every instance exactly 60 seconds after startup. Removed `WatchdogSec`
  from the template (the fraisier-webhook template already omitted it with an explanatory
  comment for the same reason).

---

## [0.5.14] - 2026-04-06

### Fixed

- **Bootstrap installs stale fraisier version due to uv index metadata cache** — `uv tool install
  --force` reinstalls but uses cached PyPI index metadata, so newly published versions are invisible
  until the cache expires. Added `--refresh-package fraisier` to the bootstrap install command so
  the index is always refreshed when bootstrapping, ensuring the server receives the exact client
  version.

---

## [0.5.13] - 2026-04-06

### Fixed

- **`uv tool install --force` fails with "Permission denied" on reinstall** — the systemctl
  helper service runs as root and causes Python to create root-owned `__pycache__/`
  directories inside the deploy user's fraisier tool installation. When uv later tries to
  force-reinstall fraisier it cannot remove those root-owned directories. Added
  `Environment=PYTHONDONTWRITEBYTECODE=1` to the helper service so Python never writes
  bytecode cache files, keeping the tool directory entirely owned by the deploy user.

---

## [0.5.12] - 2026-04-06

### Fixed

- **`/run/fraisier/` still root-owned: `systemctl-helper.socket` had `RuntimeDirectory=fraisier`** —
  the socket unit (which runs as root via `SocketUser=root`) also declared `RuntimeDirectory=fraisier`
  with `RuntimeDirectoryMode=0755`. On every socket start/restart, systemd recreated `/run/fraisier/`
  owned by root, overwriting the `printoptim_deploy`-owned directory managed by the webhook service.
  Removed `RuntimeDirectory` and `RuntimeDirectoryMode` from the socket unit; `/run/fraisier/` is
  now managed exclusively by the webhook service.

---

## [0.5.11] - 2026-04-06

### Fixed

- **`/run/fraisier/` still wiped during v0.5.7 → v0.5.10 upgrade** — the previous fix
  stopped the helper service *before* daemon-reload, which meant systemd used the OLD
  unit (which had `RuntimeDirectory=fraisier`) for the stop phase and still wiped the
  directory. The correct approach is to copy the new unit files, run `daemon-reload`
  first (so systemd loads the new unit without `RuntimeDirectory`), and *then* restart
  the service — the stop phase now uses the new unit and `/run/fraisier/` is preserved.

---

## [0.5.10] - 2026-04-06

### Fixed

- **`/run/fraisier/` wiped during upgrade from v0.5.7 → v0.5.9** — `install.sh` now
  explicitly stops the old helper service and socket *before* copying the new unit files
  and running `daemon-reload`. This prevents the old helper service (which had
  `RuntimeDirectory=fraisier`) from wiping `/run/fraisier/` at daemon-reload time. After
  the new files are in place, deploy sockets are restarted to ensure their socket files
  exist, then the helper socket is started.

---

## [0.5.9] - 2026-04-06

### Fixed

- **Every deployment wiped `/run/fraisier/` and destroyed all socket files** — the
  `systemctl-helper.service` template included `RuntimeDirectory=fraisier` (and
  `RuntimeDirectoryMode=0755`). Since the helper runs as root, systemd recreated
  `/run/fraisier/` as a root-owned directory on every activation, wiping the deploy
  socket files and the helper socket file itself. Removed `RuntimeDirectory` from the
  helper service; `/run/fraisier/` is now managed exclusively by the webhook service
  (which uses `RuntimeDirectoryPreserve=restart`).

---

## [0.5.8] - 2026-04-06

### Fixed

- **systemctl helper socket file missing after webhook restart** — `install.sh` installed the
  helper service and socket unit files but never restarted the socket, so the socket file in
  `/run/fraisier/` was never (re)created. Added `systemctl restart fraisier-{project}-systemctl-helper.socket`
  after the install step, mirroring the pattern used for deploy socket units.

---

## [0.5.7] - 2026-04-06

### Fixed

- **Duplicate `ReadWritePaths` in generated deploy service units** — the `deploy-service.j2`
  template had hardcoded `ReadWritePaths=/run/fraisier` and `ReadWritePaths={{ scaffold.config_dir }}`
  entries below the manifest loop. After the v0.5.6 fix those same paths are now emitted by the
  manifest loop, producing duplicates. The hardcoded lines have been removed; the manifest is now
  the sole source of all `ReadWritePaths` entries.

---

## [0.5.6] - 2026-04-06

### Fixed

- **Deploy service missing `ReadWritePaths` for `/opt/fraisier`, `/var/lib/fraisier`, `/run/fraisier`** —
  `build_manifest()` was collecting deploy socket stems *after* building the global paths, so the deploy
  service unit stems were absent from `read_write_units` on the three shared fraisier directories.
  Stems are now collected first and included in `shared_rw_units` used by all global paths.
- **`GIT_SSH_COMMAND` env assignment ignored by systemd** — the unquoted value
  `Environment=GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new` caused systemd to log
  `Invalid environment assignment, ignoring: -o` (spaces split the value into separate tokens).
  Fixed to `Environment="GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new"`.

---

## [0.5.5] - 2026-04-06

### Fixed

- **Deploy socket files not recreated after first-time webhook install** — `install.sh` now
  restarts all local deploy socket units after the webhook service restart, ensuring their
  socket files are always present in `/run/fraisier/` for `validate-setup` to find.

---

## [0.5.4] - 2026-04-06

### Fixed

- **Deploy socket files wiped on webhook restart** — the webhook service's `RuntimeDirectory=fraisier`
  caused systemd to remove and recreate `/run/fraisier/` on service restart, destroying socket
  files created by co-located deploy socket units. Added `RuntimeDirectoryPreserve=restart` so
  `/run/fraisier/` and its contents survive webhook restarts (still cleaned on full stop/reboot).

---

## [0.5.3] - 2026-04-06

### Fixed

- **Webhook service unit not installed by `install.sh`** — `install.sh` installed the
  systemctl helper, sudoers, app units, and deploy socket units, but never the
  `fraisier-{project}-webhook.service` file itself. Upgrades to webhook configuration
  (e.g. `FRAISIER_SYSTEMCTL_SOCKET`, `NoNewPrivileges`, `ReadWritePaths`) were silently
  not applied on re-bootstrap. Now copies the webhook service file and restarts it.
- **`fraisier-{project}-webhook.service` added to `get_install_mapping()`** so
  `scaffold-diff` correctly detects divergence between generated and installed webhook units.

---

## [0.5.2] - 2026-04-06

### Fixed

- **sudoers install rule not generated when `install.user` is defined at fraise level** —
  `_collect_deduplicated_sudoers_rules` only checked `env_config` for the `install` key,
  missing the common pattern where `install` is declared once at the fraise level and
  inherited by all environments. Now falls back to fraise-level install config.

---

## [0.5.1] - 2026-04-06

### Breaking Changes

- Scaffold output changed: regenerate and reinstall all systemd service units after upgrading.
  Generated webhook and deploy-daemon services now use `FRAISIER_SYSTEMCTL_SOCKET` instead
  of `FRAISIER_SYSTEMCTL_WRAPPER`. The generated sudoers fragment no longer contains
  a systemctl-wrapper NOPASSWD entry.

### Added

- **`fraisier-systemctl-helper`** — root-privileged Unix socket helper for service management.
  Replaces the sudo/wrapper mechanism entirely. Runs as root under systemd socket activation,
  validates commands against an allowlist baked into `ExecStart` args, calls `systemctl`
  directly. `NoNewPrivileges=true` on all services including the helper itself.
- New scaffold templates: `systemctl-helper.service.j2`, `systemctl-helper.socket.j2`.
  Generated as `fraisier-{project}-systemctl-helper.{service,socket}`, installed to
  `/etc/systemd/system/` by `install.sh`, socket enabled on first install.
- `FRAISIER_SYSTEMCTL_SOCKET` environment variable in webhook and deploy-daemon service units —
  points to `/run/fraisier/systemctl-{project}.sock`.
- `resolver.py`: new `systemctl_socket` property.

### Fixed

- **#123: `NoNewPrivileges=true` blocked sudo in webhook service** — sudo is no longer used
  for service management; all services keep `NoNewPrivileges=true`.
- **#122: wrapper called without sudo** — the wrapper mechanism is superseded; the new helper
  requires no sudo at all.

### Removed

- systemctl NOPASSWD block from generated `sudoers` fragment (no longer needed).

---

## [0.5.0] - 2026-04-06

### Breaking Changes

- Scaffold output changed: regenerate and reinstall `install.sh` and all systemd service
  units after upgrading. Generated files are now derived from the path ownership manifest
  rather than per-concern template logic.

### Added

- **PathManifest** — declarative, typed registry of every filesystem path fraisier manages,
  with ownership, permissions, and systemd unit bindings. Replaces scattered path logic.
- **ConfigResolver** — single source of truth for environment variable overrides;
  replaces scattered `os.getenv` calls across the codebase.
- `validate-remote` path checks are now manifest-driven; new managed paths get validation
  and repair automatically without handwritten checkers.

### Fixed

- **#121: `.venv` owned by the wrong user** — now deleted and recreated by the install step
  instead of failing with a non-root `chown` error.
- **ReadWritePaths in generated service units** — now complete and derived from the manifest;
  no more per-bug additions.

### Removed

- Runtime `chown` call in `_install_dependencies` (replaced by manifest-driven delete-and-recreate).
- Hardcoded path-ownership checks in `remote_validator.py` (replaced by manifest loop).
- Scattered `os.getenv` calls outside `resolver.py` and `_env.py`.

---

## [0.4.18] - 2026-04-05

### Fixed
- **Cryptic permission error when `deploy-daemon` runs as wrong user** (#67) — instead of a
  bare `PermissionError` on the lock file, fraisier now checks the current user against the
  configured `deploy_user` before acquiring the lock and emits a clear error with a suggested
  `sudo -u <deploy_user>` command.

---

## [0.4.17] - 2026-04-05

### Fixed
- **Per-env nginx configs had wrong filename and were not installed** (#110) — files were named
  `{project}_{fraise}_{env}.conf` instead of `{server_name}.conf`, mismatching nginx conventions.
  The renderer now uses `nginx_config.server_name` as the filename stem. The generated `install.sh`
  now also copies and symlinks each per-env config into `/etc/nginx/sites-available/` and
  `/etc/nginx/sites-enabled/`, then reloads nginx — previously only `gateway.conf` was installed.

---

## [0.4.14] - 2026-04-04

### Fixed
- **Bootstrap double-sudo fails silently for steps 4 and 10** (#104) — when using `--sudo`
  with a non-root SSH user, the outer `sudo -S` consumed the password from stdin, leaving the
  inner `sudo -u <deploy_user>` with no stdin to read from. Steps 3, 4, and 10 now use
  `sudo -n -u` (non-interactive), which prevents any stdin read since root needs no password
  to switch users.
- **Bootstrap step 4 installs wrong fraisier version on server** (#103) — `_install_fraisier`
  read the version from the hardcoded `__init__.__version__` string, which could be stale
  relative to `pyproject.toml`. It now uses `importlib.metadata.version("fraisier")`, which
  always reflects the actually-installed package version.

---

## [0.4.13] - 2026-04-04

### Fixed
- **`validate-setup` and `deploy` used old socket path pattern** — both commands were
  building the socket directory as `/run/fraisier/{project_name}-{environment}` instead of
  using `deploy_socket_name()`, causing them to look in the wrong place after the naming
  overhaul in v0.4.9.
- **`_check_systemd_units` used a completely wrong unit name** — it was constructing
  `fraisier-{project}-{environment}-deploy.socket`, a pattern that never existed. It now
  receives the unit name directly from the caller via `deploy_socket_name()`.

---

## [0.4.12] - 2026-04-04

### Fixed
- **Bootstrap step 4 always pins server-side fraisier to the client version** (#101) — `uv tool install`
  was skipping the install if fraisier was already present on the server, leaving a stale version.
  It now runs `uv tool install --force fraisier==<client_version>` unconditionally, ensuring the
  server and client are always in sync.

---

## [0.4.10] - 2026-04-04

### Fixed
- **Bootstrap step 10 validates each fraise individually** (#98) — `validate-setup` requires
  a positional `FRAISE` argument; the bootstrap command was calling it without one. It now
  iterates all fraises configured for the target environment, calling `validate-setup <fraise>`
  once per fraise and failing fast on the first error.

---

## [0.4.9] - 2026-04-04

### Added
- **`fraisier/naming.py`** — new `deploy_socket_name(env_config, env_key)` helper as the
  single source of truth for deploy socket unit names. Resolves in order:
  1. explicit `systemd_deploy_socket` field in environment config
  2. `fraisier-{env.name}.socket` derived from the environment's `name` field
  3. `fraisier-{env_key}.socket` derived from the environment dict key
- **`systemd_deploy_socket` config field** — optional per-environment override for the
  deploy socket unit name (validated against the same regex as `systemd_service`)

### Changed
- **Deploy socket unit names** now derived from the environment `name` field (e.g.
  `fraisier-api.myapp.io.socket`) instead of the verbose
  `fraisier-{project}-{fraise}-{env}-deploy.socket` pattern
- **`ListenStream` socket path** in generated units updated to match the new naming
  (`/run/fraisier/{socket_stem}/deploy.sock`)
- **`fraisier/scaffold/renderer.py`** — all consumers call `deploy_socket_name()`;
  `socket_unit_name` and `socket_stem` added to socket/service template contexts;
  `deploy_socket_name` registered as a Jinja2 global for use in templates
- **`fraisier/scaffold/diff.py`** — filter logic replaced: pre-computes the set of
  matching deploy unit paths from config instead of parsing filenames with a regex
- **`fraisier/bootstrap.py`** — `_enable_sockets()` now iterates all fraises for the
  target environment and enables each socket; returns a clear error if no fraises match
- **`fraisier/cli/main.py`** — `diagnose` command derives `socket_unit` and `socket_path`
  via `deploy_socket_name()`

### Fixed
- **Bootstrap enabled the wrong socket unit** (#95) — bootstrap was building
  `fraisier-{project}-{env}-deploy.socket`, missing the fraise name, causing
  `systemctl enable` to fail. Fixed as a side effect of centralising naming in #96.

---

## [0.4.3] - 2026-04-03

### Added

#### CLI Enhancements
- **New `fraisier logs <fraise> <env>` command** - Tail systemd journal logs for deploy daemons
  - Supports `--no-follow`, `--lines N`, `--since` options
  - Automatically resolves unit patterns from configuration
  - Uses `os.execvp` for proper signal handling

- **Enhanced `fraisier history` command** - Improved deployment history viewing
  - Positional arguments: `fraisier history <fraise> <env>`
  - `--json` flag for structured output
  - `--since` filtering with relative time parsing (7d, 24h) and ISO dates
  - Enhanced table with SHA (truncated) and "Triggered By" columns
  - Better duration formatting with time units

- **New `fraisier scaffold-diff` command** - Detect infrastructure drift
  - Compare generated scaffold files against installed system files
  - Unified diff output with file-level summaries
  - Supports filtering by fraise/environment
  - `--apply` flag for automatic re-installation (future enhancement)

- **Enhanced `fraisier ship` command** - Post-deployment verification
  - `--wait-deploy` flag polls health endpoint after shipping
  - `--deploy-timeout` option controls verification timeout (default 300s)
  - Shows progress during health polling with elapsed time
  - Supports multiple health endpoint version field names

- **Enhanced `fraisier trigger-deploy` command** - Synchronous execution
  - `--wait` flag blocks until deployment completion
  - `--follow` flag streams deployment logs in real-time
  - JSON result parsing from daemon responses

#### Daemon & Status Improvements
- **Enhanced deployment status display** - Real-time deployment visibility
  - `fraisier status` shows deploying/pending/failed states with elapsed time
  - Status file reading prioritizes over version comparison
  - Added "pending" state to deployment lifecycle

- **Improved daemon error diagnostics** - Better troubleshooting
  - Config search path display when config files not found
  - Project availability hints when fraise not in configuration
  - File existence checks in search locations

- **Rollback enhancements** - Safer rollback operations
  - `--dry-run` shows rollback plan without executing
  - Improved target resolution finds most recent successful deployment
  - Safety limit prevents rollback beyond 10 deployments (unless --force)
  - Better output with current vs target version display

#### Internal Improvements
- **Health polling system** - `fraisier.ship.health_poll` module
  - Robust HTTP polling with configurable timeout/intervals
  - Version extraction from multiple health endpoint field names
  - Progress display during long-running health checks

- **Scaffold drift detection** - `fraisier.scaffold.diff` module
  - File comparison with unified diff generation
  - Install path mapping from scaffold to system locations
  - Support for systemd, nginx, and sudoers file types

- **Enhanced status file management**
  - Daemon writes status updates during deployment lifecycle
  - Atomic status file operations with proper error handling

### Changed
- **Daemon result output** - Now writes JSON results to stdout for socket clients
- **Status computation** - Prefers status file data over version comparison
- **Rollback target selection** - Uses smarter algorithm for finding rollback targets

### Fixed
- **Config validation** - Better error messages for missing fraises/environments
- **Socket communication** - Improved response parsing in trigger-deploy

### Technical Details
- **8 major features** implemented across multiple phases
- **15+ new CLI commands/options** added
- **20+ test files** with comprehensive coverage
- **Zero breaking changes** - Full backward compatibility maintained
- **Enterprise-grade reliability** with proper error handling and timeouts

---

## [1.0.0] - 2026-03-15

Initial release of Fraisier deployment management system.

### Added
- Core deployment functionality for multiple providers (Docker Compose, API, ETL)
- Configuration-driven deployment with `fraises.yaml`
- Systemd socket-activated deployment daemons
- Scaffold generation for infrastructure files
- Version management and git integration
- Database migration support via confiture
- Comprehensive testing framework
- Rich CLI with progress indicators and error handling
