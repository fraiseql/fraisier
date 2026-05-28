# Release Strategies

Fraisier supports two release-cadence workflows out of the box. There is no
config flag to pick between them — the difference is purely in how you invoke
`fraisier ship` and (optionally) what other tooling you wire alongside it.

## At a glance

| | Per-PR releases (default) | Bring-your-own batched (release-please) |
|---|---|---|
| Each shipped PR bumps the version | yes | no |
| Changelog cadence | one entry per shipped PR | one entry per release-PR merge |
| Webhook deploy fires on | every merged PR (version changes) | release-PR merge (version changes) |
| Tooling needed | none beyond fraisier | release-please workflow on the release branch |
| `fraisier ship` invocation | `fraisier ship patch \| minor \| major` | `fraisier ship --no-bump` |
| Best when | small team, fast cadence, every PR is shippable | many small PRs per release, you want one cohesive changelog entry |

Both workflows use the same webhook, the same health checks, the same
restore strategies, the same `fraisier sync`. The difference is only who
owns the version bump and when it lands.

---

## Default: per-PR releases

This is fraisier's out-of-the-box behavior. Every shipped PR contains the
code change *and* a version bump. The merge triggers the webhook, the
webhook deploys, every PR is its own release.

```bash
# After committing your code change on a feature branch:
fraisier ship patch
```

`fraisier ship` runs your check pipeline, bumps `pyproject.toml`, commits
the bump, pushes, optionally opens a PR with auto-merge. The merged version
bump triggers the deploy.

No additional tooling required.

### When this works well

- You ship small, frequent PRs and each one is a sensible "release."
- You don't mind a CHANGELOG with one entry per PR.
- You'd rather have N small reverts than one big one when something breaks.

### Where it strains

- Many tiny PRs land in close succession, producing changelog noise.
- Two operators run `fraisier ship` concurrently from the same base —
  both compute the same next version, the second silently produces a
  duplicate-version PR. (As of v0.27.0 fraisier detects this case at push
  time and refuses to push; see [#232](https://github.com/fraiseql/fraisier/issues/232).)

Many fraisier users address the second strain by **bundling**: open a
single PR that lands several fixes together with one version bump
(`release: vX.Y.Z — desc (#N, #M)`). That's a human-driven batched
workflow already; the bring-your-own option below automates the same idea.

---

## Bring-your-own batched releases (release-please)

If you want one CHANGELOG entry per release rather than one per merged
code change, run [release-please](https://github.com/googleapis/release-please)
alongside fraisier. The split is clean:

- **Feature PRs** land code only — no version bump.
  Use `fraisier ship --no-bump`. The webhook does not fire on these
  merges (no version change to detect), so no deploy happens at
  feature-PR merge time.
- **release-please** maintains a single open "release PR" on your release
  branch. It accumulates the next version bump and a generated CHANGELOG
  section based on the conventional commits since the last release.
- **When the release PR merges**, the version bump lands, the webhook
  fires, fraisier deploys.

Fraisier does not need to know which mode you're in. `--no-bump` is a
pre-existing flag; you simply use it on every ship.

### Example release-please workflow

Drop this into `.github/workflows/release-please.yml` in your application
repo (adjust `release-type` and `target-branch` to match your setup):

```yaml
name: release-please

on:
  push:
    branches:
      - main

permissions:
  contents: write
  pull-requests: write

jobs:
  release-please:
    runs-on: ubuntu-latest
    steps:
      - uses: googleapis/release-please-action@v4
        with:
          release-type: python
          target-branch: main
          # release-please writes pyproject.toml; fraisier's webhook
          # picks the version change up automatically.
```

Per-fraise conventions to keep in mind:

- Write conventional commits (`feat:`, `fix:`, `chore:` …) on your feature
  PRs. release-please reads these to decide patch/minor/major.
- Your feature PRs target the same `target-branch` as release-please.
- `fraisier ship --no-bump` on every feature PR.

### When this works well

- You ship many small PRs per release and the per-PR changelog feels noisy.
- You want a single PR to land all the changes that constitute a release
  (easier to revert as a group, easier to release-note).
- You're already using conventional commits.

### Where it strains

- Hotfixes have a slower path: either you cut a release PR by hand for the
  one fix, or you temporarily run `fraisier ship patch` to bypass the
  batched workflow for one PR. The latter is a footgun (release-please
  reconciles it on the next release PR, but the bump is now out of
  sequence with the batched CHANGELOG).
- Rollback semantics: rolling back to the previous version reverts a
  *batch* of code changes, not a single PR.

---

## Interaction with `--wait-deploy`

`fraisier ship --wait-deploy` polls your health endpoint after the deploy.
Behavior differs by workflow:

**Per-PR**: polls for the bumped version. When the deployed service
reports the new version, the poll succeeds.

**Bring-your-own batched** (`--no-bump --wait-deploy`): the deployer still
runs (the working tree is redeployed), but no version label changed.
fraisier prints an explicit note:

```
--no-bump: no version change — polling v1.2.3 to confirm the current
redeploy stays healthy. A later release-PR merge (if any) produces a
separate deploy.
```

The poll then verifies that the redeployed service continues to answer
healthily at its existing version. If you were expecting the wait to
block until release-please's PR merges and produces a *new* deploy, it
won't — that deploy happens separately when its PR merges.

---

## Interaction with `fraisier sync`

`fraisier sync` propagates changes between environment branches
(typically `dev → staging → main`). It auto-resolves a small set of
fraisier-owned files (`pyproject.toml`, `version.json`, `uv.lock`,
`.secrets.baseline`, `fraises.yaml`, `scripts/generated`) by taking the
source branch's version on conflict.

Under both workflows this is the right behavior in practice:

- **Per-PR**: the source branch always contains the freshest bump.
  Auto-resolving from source moves the bump forward.
- **Bring-your-own batched**: source-side feature PRs don't modify
  `pyproject.toml` at all. When you sync the bumped release-PR-merged
  state from target back into source's history, three-way merge
  semantics handle it without ever entering the auto-resolve path
  (git keeps the target-side bump because source didn't touch the file).

If you do hit a divergent both-bumped case (rare; typically caused by
direct hotfix bumps on the target branch), the sync auto-resolve takes
source. Inspect the resulting sync PR before merging.

---

## Switching workflows

Switching is a one-time operation, not a config flag:

**Per-PR → bring-your-own batched.** Land any in-flight `fraisier ship`
PRs first, then merge a release-please workflow file. From the next
feature PR onwards, use `fraisier ship --no-bump`.

**Bring-your-own → per-PR.** Disable or delete the release-please
workflow. Resume using `fraisier ship patch | minor | major` directly.
The next bump will be computed against the current `pyproject.toml`
version, so any release-please-managed version stays the floor.

---

## Why no `release_strategy` config field

Fraisier deliberately does not gate this on a `release_strategy` yaml
field. The two workflows are distinguishable by ship invocation
(`patch|minor|major` vs `--no-bump`) and by whether you've wired up
release-please. Adding a config knob would commit a public-API surface
to a distinction that the existing flags already express. See
the [#234 design memo](https://github.com/fraiseql/fraisier/issues/234)
for the longer reasoning.
