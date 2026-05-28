"""Tests for fraisier sync command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from fraisier.cli.main import main

# Patch target: subprocess.run as imported inside sync.py
_PATCH = "fraisier.cli.sync.subprocess.run"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# Sentinel SHAs used in mocked `git log -1 --pretty=%P HEAD` output to satisfy
# the pre-push merge-commit invariant (#233 Layer 2): HEAD must have 2 parents.
_MERGE_PARENTS = "abcdef0123456789 fedcba9876543210\n"


def _no_source_deletions() -> MagicMock:
    """Mock the post-merge `git diff --filter=D` pre-pass returning nothing.

    Inserted after every `git merge` mock to satisfy `_propagate_source_deletions`,
    which fires once per sync run regardless of whether the merge had conflicts.
    """
    return _mk(stdout="")


def _merge_finalize_tail() -> list[MagicMock]:
    """Mock the post-commit pre-push invariant check (#233 Layer 2).

    Sequence:
      1. `git log -1 --pretty=%P HEAD` returns two parent SHAs (merge commit).
      2. `git rev-parse --verify --quiet MERGE_HEAD` exits 1 (merge cleared).
    """
    return [_mk(stdout=_MERGE_PARENTS), _mk(returncode=1)]


def _in_merge() -> MagicMock:
    """Mock the `git rev-parse --verify --quiet MERGE_HEAD` probe used by
    `_commit_merge_or_staged`: returncode 0 means a merge is in progress, so
    the helper proceeds straight to `git commit` (no diff --cached check)."""
    return _mk(returncode=0)


def _setup(tmp_path, pairs: list[dict]) -> str:
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        yaml.dump(
            {
                "scaffold": {
                    "output_dir": str(tmp_path / "output"),
                    "sync": pairs,
                },
                "fraises": {
                    "my_api": {
                        "type": "api",
                        "environments": {"production": {"app_path": "/var/www"}},
                    }
                },
            }
        )
    )
    return str(cfg)


# ---------------------------------------------------------------------------
# Unit: _is_auto_resolved
# ---------------------------------------------------------------------------


class TestIsAutoResolved:
    def test_version_json(self):
        from fraisier.cli.sync import _is_auto_resolved

        assert _is_auto_resolved("version.json") is True

    def test_pyproject_toml(self):
        from fraisier.cli.sync import _is_auto_resolved

        assert _is_auto_resolved("pyproject.toml") is True

    def test_uv_lock(self):
        from fraisier.cli.sync import _is_auto_resolved

        assert _is_auto_resolved("uv.lock") is True

    def test_fraises_yaml(self):
        from fraisier.cli.sync import _is_auto_resolved

        assert _is_auto_resolved("fraises.yaml") is True

    def test_scripts_generated_prefix(self):
        from fraisier.cli.sync import _is_auto_resolved

        assert _is_auto_resolved("scripts/generated/nginx/gateway.conf") is True

    def test_scripts_generated_exact(self):
        from fraisier.cli.sync import _is_auto_resolved

        assert _is_auto_resolved("scripts/generated") is True

    def test_source_file_not_auto_resolved(self):
        from fraisier.cli.sync import _is_auto_resolved

        assert _is_auto_resolved("src/api/routes.py") is False

    def test_scripts_non_generated_not_auto_resolved(self):
        from fraisier.cli.sync import _is_auto_resolved

        assert _is_auto_resolved("scripts/deploy.sh") is False


# ---------------------------------------------------------------------------
# Unit: _resolve_pair
# ---------------------------------------------------------------------------


class TestResolvePair:
    def _pairs(self, *specs):
        from fraisier.config.schema import SyncPair

        return [SyncPair(source=s, target=t) for s, t in specs]

    def test_single_pair_no_target(self):
        from fraisier.cli.sync import _resolve_pair

        p = _resolve_pair(None, self._pairs(("dev", "staging")))
        assert p.source == "dev" and p.target == "staging"

    def test_multiple_pairs_no_target_exits(self):
        from fraisier.cli.sync import _resolve_pair

        with pytest.raises(SystemExit) as exc:
            _resolve_pair(None, self._pairs(("dev", "staging"), ("staging", "prod")))
        assert exc.value.code == 1

    def test_multiple_pairs_explicit_target(self):
        from fraisier.cli.sync import _resolve_pair

        p = _resolve_pair("prod", self._pairs(("dev", "staging"), ("staging", "prod")))
        assert p.source == "staging" and p.target == "prod"

    def test_unknown_target_exits(self):
        from fraisier.cli.sync import _resolve_pair

        with pytest.raises(SystemExit) as exc:
            _resolve_pair("nope", self._pairs(("dev", "staging")))
        assert exc.value.code == 1

    def test_empty_pairs_exits(self):
        from fraisier.cli.sync import _resolve_pair

        with pytest.raises(SystemExit) as exc:
            _resolve_pair(None, [])
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Unit: _read_branch_version
# ---------------------------------------------------------------------------


class TestReadBranchVersion:
    def test_reads_version_json(self):
        from fraisier.cli.sync import _read_branch_version

        with patch(_PATCH) as m:
            m.side_effect = [_mk(returncode=0, stdout='{"version": "2.3.1"}')]
            assert _read_branch_version("main") == "2.3.1"

    def test_falls_back_to_pyproject(self):
        from fraisier.cli.sync import _read_branch_version

        pyproject = '[project]\nname = "myapp"\nversion = "1.5.0"\n'
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(returncode=1),
                _mk(returncode=0, stdout=pyproject),
            ]
            assert _read_branch_version("main") == "1.5.0"

    def test_returns_unknown_when_both_missing(self):
        from fraisier.cli.sync import _read_branch_version

        with patch(_PATCH) as m:
            m.side_effect = [_mk(returncode=1), _mk(returncode=1)]
            assert _read_branch_version("main") == "unknown"

    def test_invalid_json_falls_back_to_pyproject(self):
        from fraisier.cli.sync import _read_branch_version

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(returncode=0, stdout="not-json"),
                _mk(returncode=0, stdout='version = "3.0.0"\n'),
            ]
            assert _read_branch_version("branch") == "3.0.0"


# ---------------------------------------------------------------------------
# Unit: _target_unchanged_since_base
# ---------------------------------------------------------------------------


class TestTargetUnchangedSinceBase:
    def test_returns_true_when_git_diff_exits_0(self):
        from fraisier.cli.sync import _target_unchanged_since_base

        with patch(_PATCH) as m:
            m.return_value = _mk(returncode=0)
            assert _target_unchanged_since_base("abc123", "staging", "file.txt") is True

    def test_returns_false_when_git_diff_exits_1(self):
        from fraisier.cli.sync import _target_unchanged_since_base

        with patch(_PATCH) as m:
            m.return_value = _mk(returncode=1)
            assert (
                _target_unchanged_since_base("abc123", "staging", "file.txt") is False
            )

    def test_returns_true_when_file_absent_at_merge_base_and_target(self):
        from fraisier.cli.sync import _target_unchanged_since_base

        with patch(_PATCH) as m:
            m.return_value = _mk(returncode=0)
            assert _target_unchanged_since_base("abc123", "staging", "file.txt") is True

    def test_returns_false_when_git_diff_exits_with_unexpected_error(self):
        from fraisier.cli.sync import _target_unchanged_since_base

        with patch(_PATCH) as m:
            m.return_value = _mk(returncode=128)
            assert (
                _target_unchanged_since_base("abc123", "staging", "file.txt") is False
            )


# ---------------------------------------------------------------------------
# CLI: --list flag
# ---------------------------------------------------------------------------


class TestSyncListFlag:
    def test_list_shows_configured_pairs(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        result = CliRunner().invoke(main, ["-c", cfg, "sync", "--list"])
        assert result.exit_code == 0
        assert "dev" in result.output
        assert "staging" in result.output

    def test_list_multiple_pairs(self, tmp_path):
        cfg = _setup(
            tmp_path,
            [
                {"source": "dev", "target": "staging"},
                {"source": "staging", "target": "prod"},
            ],
        )
        result = CliRunner().invoke(main, ["-c", cfg, "sync", "--list"])
        assert result.exit_code == 0
        assert "staging" in result.output
        assert "prod" in result.output

    def test_list_no_pairs_exits_0(self, tmp_path):
        cfg = _setup(tmp_path, [])
        result = CliRunner().invoke(main, ["-c", cfg, "sync", "--list"])
        assert result.exit_code == 0
        assert "No sync pairs" in result.output


# ---------------------------------------------------------------------------
# CLI: --check flag
# ---------------------------------------------------------------------------


class TestSyncCheckFlag:
    def test_check_shows_version_diff(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(returncode=0, stdout='{"version": "1.2.0"}'),  # dev
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),  # staging
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--check"])
        assert result.exit_code == 0
        assert "Version diff" in result.output
        assert "1.2.0" in result.output
        assert "1.1.0" in result.output

    def test_check_makes_no_git_write_calls(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(returncode=0, stdout='{"version": "1.2.0"}'),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
            ]
            CliRunner().invoke(main, ["-c", cfg, "sync", "--check"])
        commands = [c[0][0] for c in m.call_args_list]
        # Only git show calls should have been made
        assert all("show" in cmd for cmd in commands)

    def test_check_ambiguous_target_exits_1(self, tmp_path):
        cfg = _setup(
            tmp_path,
            [
                {"source": "dev", "target": "staging"},
                {"source": "staging", "target": "prod"},
            ],
        )
        result = CliRunner().invoke(main, ["-c", cfg, "sync", "--check"])
        assert result.exit_code == 1

    def test_check_explicit_target(self, tmp_path):
        cfg = _setup(
            tmp_path,
            [
                {"source": "dev", "target": "staging"},
                {"source": "staging", "target": "prod"},
            ],
        )
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(returncode=0, stdout='{"version": "2.0.0"}'),
                _mk(returncode=0, stdout='{"version": "1.9.0"}'),
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "prod", "--check"])
        assert result.exit_code == 0
        assert "2.0.0" in result.output


# ---------------------------------------------------------------------------
# CLI: already in sync
# ---------------------------------------------------------------------------


class TestSyncAlreadyInSync:
    def test_exits_0_when_already_in_sync(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        sha = "abc123" * 7
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),  # git rev-parse HEAD
                _mk(),  # git fetch
                _mk(stdout=sha + "\n"),  # git rev-parse origin/dev
                _mk(stdout=sha + "\n"),  # git merge-base (== source_sha)
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0
        assert (
            "up to date" in result.output.lower()
            or "nothing to sync" in result.output.lower()
        )

    def test_no_git_write_ops_when_already_in_sync(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        sha = "abc123" * 7
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=sha + "\n"),
                _mk(stdout=sha + "\n"),
            ]
            CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        commands = [c[0][0] for c in m.call_args_list]
        assert not any("checkout" in cmd for cmd in commands)
        assert not any("push" in cmd for cmd in commands)


# ---------------------------------------------------------------------------
# CLI: happy path
# ---------------------------------------------------------------------------


class TestSyncHappyPath:
    """Full successful sync: clean merge, PR created, auto-merge enabled."""

    SHA = "deadbeef" * 5
    MERGE_BASE = "cafe1234" * 5
    PR_URL = "https://github.com/org/repo/pull/42"

    def _side_effects(self):
        return [
            _mk(stdout="main\n"),  # git rev-parse HEAD
            _mk(),  # git fetch
            _mk(stdout=self.SHA + "\n"),  # git rev-parse origin/dev
            _mk(stdout=self.MERGE_BASE + "\n"),  # git merge-base
            _mk(returncode=0, stdout='{"version": "1.1.0"}'),  # version dev
            _mk(returncode=0, stdout='{"version": "1.0.0"}'),  # version staging
            _mk(),  # git checkout -b
            _mk(),  # git merge (clean)
            _no_source_deletions(),  # diff --filter=D pre-pass
            _in_merge(),  # rev-parse MERGE_HEAD (in merge)
            _mk(),  # git commit pre-merge
            *_merge_finalize_tail(),  # pre-push merge-commit invariant check
            _mk(),  # git push
            _mk(returncode=1),  # gh pr view (no existing PR)
            _mk(stdout=self.PR_URL + "\n"),  # gh pr create
            _mk(),  # gh pr merge
            _mk(),  # git checkout main
        ]

    def test_exits_0_and_prints_pr_url(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = self._side_effects()
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0
        assert self.PR_URL in result.output

    def test_returns_to_original_branch(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = self._side_effects()
            CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        last_cmd = m.call_args_list[-1][0][0]
        assert last_cmd == ["git", "checkout", "main"]

    def test_does_not_delete_sync_branch_on_success(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = self._side_effects()
            CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        commands = [c[0][0] for c in m.call_args_list]
        assert not any("branch" in cmd and "-D" in cmd for cmd in commands)

    def test_calls_gh_pr_merge_auto_squash(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = self._side_effects()
            CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        commands = [c[0][0] for c in m.call_args_list]
        assert ["gh", "pr", "merge", "--auto", "--squash", self.PR_URL] in commands

    def test_merge_commit_created_even_when_no_tree_diff(self, tmp_path):
        """Regression for #233: `git merge --no-commit` leaves MERGE_HEAD set
        even on clean merges with no tree diff. The pre-merge commit MUST
        still fire so the push records both parents and GitHub sees the
        branch as merged. Previously `_commit_if_staged` skipped the commit
        when `git diff --cached --quiet` returned 0, leaving the merge
        orphaned and producing a CONFLICTING PR after push."""
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(),  # git merge (clean)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _in_merge(),  # MERGE_HEAD set → commit unconditionally
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view (no existing PR)
                _mk(stdout=self.PR_URL + "\n"),
                _mk(),
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        commands = [c[0][0] for c in m.call_args_list]
        commit_calls = [c for c in commands if "commit" in c]
        assert commit_calls, (
            "expected a merge commit even when resolution yielded no diff; "
            f"commands: {commands}"
        )

    def test_pre_merge_commit_uses_no_verify_clean_merge(self, tmp_path):
        """Pre-merge commit on clean merge path must include --no-verify."""
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = self._side_effects()
            CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        commit_calls = [c[0][0] for c in m.call_args_list if "commit" in c[0][0]]
        assert commit_calls, "expected at least one commit call"
        assert all("--no-verify" in call for call in commit_calls)


# ---------------------------------------------------------------------------
# CLI: conflict resolution
# ---------------------------------------------------------------------------


class TestSyncConflicts:
    SHA = "deadbeef" * 5
    MERGE_BASE = "cafe1234" * 5
    PR_URL = "https://github.com/org/repo/pull/99"

    def test_auto_resolves_owned_files_and_succeeds(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _mk(stdout="version.json\npyproject.toml\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:version.json (exists)
                _mk(),  # checkout origin/dev -- version.json
                _mk(),  # git add version.json
                _mk(returncode=0),  # cat-file origin/dev:pyproject.toml (exists)
                _mk(),  # checkout origin/dev -- pyproject.toml
                _mk(),  # git add pyproject.toml
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set → commit unconditionally
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view (no existing PR)
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        assert self.PR_URL in result.output

    def test_pre_merge_commit_uses_no_verify_conflict_path(self, tmp_path):
        """Pre-merge commit on conflict-resolution path must include --no-verify."""
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _mk(stdout="version.json\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:version.json (exists)
                _mk(),  # checkout origin/dev -- version.json
                _mk(),  # git add version.json
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set → commit unconditionally
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view (no existing PR)
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        commit_calls = [c[0][0] for c in m.call_args_list if "commit" in c[0][0]]
        assert commit_calls, "expected at least one commit call"
        assert all("--no-verify" in call for call in commit_calls)

    def test_pre_merge_commits_when_resolution_yields_no_tree_diff(self, tmp_path):
        """Regression for #233 (supersedes the #164 reading of `_commit_if_staged`).

        When every conflicted file auto-resolves back to source HEAD (the sync
        branch's tip), the index after `git add` is byte-identical to HEAD,
        but MERGE_HEAD is still set. `git commit` is happy to create a merge
        commit with no tree diff in that case — and we MUST create it, because
        pushing without the merge commit drops the second parent and leaves
        the PR in a CONFLICTING state on GitHub.

        The original #164 fix swallowed the commit via `git diff --cached
        --quiet`, which is exactly the #233 bug. `_commit_merge_or_staged`
        now detects MERGE_HEAD and commits unconditionally during a merge.
        """
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _mk(stdout="pyproject.toml\n"),  # diff --filter=U
                _mk(returncode=0),  # cat-file origin/dev:pyproject.toml (exists)
                _mk(),  # checkout origin/dev -- pyproject.toml
                _mk(),  # git add pyproject.toml
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set → commit unconditionally
                _mk(),  # git commit (merge commit, even with no tree diff)
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view (no existing PR)
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])

        assert result.exit_code == 0, result.output
        commands = [c[0][0] for c in m.call_args_list]
        commit_calls = [c for c in commands if "commit" in c]
        assert commit_calls, (
            "a merge commit MUST be created when MERGE_HEAD is set, even "
            "when the resolution yields no tree diff vs HEAD"
        )
        merge_head_probes = [
            c
            for c in commands
            if c[:4] == ["git", "rev-parse", "--verify", "--quiet"]
            and "MERGE_HEAD" in c
        ]
        assert merge_head_probes, (
            "expected `_merge_in_progress` probe before the pre-merge commit"
        )

    def test_source_deletion_auto_resolved(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _mk(stdout="old_script.py\n"),  # diff --filter=U (first)
                _mk(
                    returncode=1
                ),  # cat-file origin/dev:old_script.py (not in source → deleted)
                _mk(),  # git rm old_script.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view (no existing PR)
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Auto-resolved source deletion: old_script.py" in result.output

    def test_non_owned_conflicts_exit_1(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (first)
                _mk(
                    returncode=0
                ),  # cat-file origin/dev:src/routes.py (exists in source)
                _mk(
                    returncode=1
                ),  # git diff merge_base origin/staging -- src/routes.py (changed)
                # src/routes.py exists in source and is not auto-resolved → no checkout/add
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (remaining)
                _mk(),  # git checkout main (cleanup)
                _mk(),  # git branch -D (cleanup)
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 1
        assert "Unresolved conflicts" in result.output

    def test_cleanup_deletes_branch_on_conflict_failure(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),
                _mk(returncode=1),
                _no_source_deletions(),  # diff --filter=D pre-pass
                _mk(stdout="src/routes.py\n"),
                _mk(
                    returncode=0
                ),  # cat-file origin/dev:src/routes.py (exists in source)
                _mk(
                    returncode=1
                ),  # git diff merge_base origin/staging -- src/routes.py (changed)
                _mk(stdout="src/routes.py\n"),
                _mk(),  # git checkout main
                _mk(),  # git branch -D
            ]
            CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        commands = [c[0][0] for c in m.call_args_list]
        deletes = [c for c in commands if "branch" in c and "-D" in c]
        assert len(deletes) == 1
        assert "sync/staging-from-dev" in deletes[0]

    def test_auto_resolves_target_behind_source(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:src/routes.py (exists)
                _mk(
                    returncode=0
                ),  # git diff merge_base origin/staging -- src/routes.py (unchanged)
                _mk(),  # checkout origin/dev -- src/routes.py
                _mk(),  # git add src/routes.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view (no existing PR)
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        assert self.PR_URL in result.output
        assert (
            "Auto-resolved (staging unchanged since merge-base): src/routes.py"
            in result.output
        )

    def test_prefer_source_flag_resolves_diverged_file(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:src/routes.py (exists)
                _mk(
                    returncode=1
                ),  # git diff merge_base origin/staging -- src/routes.py (changed)
                _mk(),  # checkout origin/dev -- src/routes.py (prefer-source)
                _mk(),  # git add src/routes.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view (no existing PR)
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 0, result.output
        assert self.PR_URL in result.output
        assert "Auto-resolved (prefer-source): src/routes.py" in result.output

    def test_prefer_source_pair_config_resolves_diverged_file(self, tmp_path):
        cfg = _setup(
            tmp_path, [{"source": "dev", "target": "staging", "prefer_source": True}]
        )
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:src/routes.py (exists)
                _mk(
                    returncode=1
                ),  # git diff merge_base origin/staging -- src/routes.py (changed)
                _mk(),  # checkout origin/dev -- src/routes.py (prefer-source from config)
                _mk(),  # git add src/routes.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view (no existing PR)
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        assert self.PR_URL in result.output
        assert "Auto-resolved (prefer-source): src/routes.py" in result.output

    def test_prefer_source_flag_takes_precedence_over_pair(self, tmp_path):
        cfg = _setup(
            tmp_path, [{"source": "dev", "target": "staging", "prefer_source": False}]
        )
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:src/routes.py (exists)
                _mk(
                    returncode=1
                ),  # git diff merge_base origin/staging -- src/routes.py (changed)
                _mk(),  # checkout origin/dev -- src/routes.py (flag overrides config)
                _mk(),  # git add src/routes.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view (no existing PR)
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 0, result.output
        assert self.PR_URL in result.output
        assert "Auto-resolved (prefer-source): src/routes.py" in result.output


# ---------------------------------------------------------------------------
# CLI: confirmation prompt
# ---------------------------------------------------------------------------


class TestSyncConfirmation:
    SHA = "deadbeef" * 5
    MERGE_BASE = "cafe1234" * 5

    def _pre_confirm_effects(self):
        return [
            _mk(stdout="main\n"),
            _mk(),
            _mk(stdout=self.SHA + "\n"),
            _mk(stdout=self.MERGE_BASE + "\n"),
            _mk(returncode=0, stdout='{"version": "1.1.0"}'),
            _mk(returncode=0, stdout='{"version": "1.0.0"}'),
        ]

    def test_n_aborts_without_branch_creation(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = self._pre_confirm_effects()
            result = CliRunner().invoke(main, ["-c", cfg, "sync"], input="n\n")
        assert result.exit_code == 0
        assert "Aborted" in result.output
        commands = [c[0][0] for c in m.call_args_list]
        assert not any("checkout" in cmd and "-b" in cmd for cmd in commands)

    def test_yes_flag_skips_confirm(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        pr_url = "https://github.com/org/repo/pull/1"
        with patch(_PATCH) as m:
            m.side_effect = [
                *self._pre_confirm_effects(),
                _mk(),  # git checkout -b
                _mk(),  # git merge (clean)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _in_merge(),  # MERGE_HEAD set
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view (no existing PR)
                _mk(stdout=pr_url + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Proceed" not in result.output


# ---------------------------------------------------------------------------
# CLI: error cases
# ---------------------------------------------------------------------------


class TestSyncErrorCases:
    def test_no_pairs_configured_exits_1(self, tmp_path):
        cfg = _setup(tmp_path, [])
        result = CliRunner().invoke(main, ["-c", cfg, "sync"])
        assert result.exit_code == 1

    def test_ambiguous_target_exits_1(self, tmp_path):
        cfg = _setup(
            tmp_path,
            [
                {"source": "dev", "target": "staging"},
                {"source": "staging", "target": "prod"},
            ],
        )
        result = CliRunner().invoke(main, ["-c", cfg, "sync"])
        assert result.exit_code == 1
        assert "staging" in result.output or "prod" in result.output

    def test_unknown_target_exits_1(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        result = CliRunner().invoke(main, ["-c", cfg, "sync", "nope"])
        assert result.exit_code == 1

    def test_missing_config_file(self, tmp_path):
        result = CliRunner().invoke(main, ["-c", str(tmp_path / "nope.yaml"), "sync"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# CLI: --dry-run
# ---------------------------------------------------------------------------


class TestSyncDryRun:
    def test_dry_run_exits_zero(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert result.exit_code == 0

    def test_dry_run_makes_no_subprocess_calls(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        m.assert_not_called()

    def test_dry_run_shows_dry_run_header(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert "DRY RUN" in result.output
        assert "dev" in result.output
        assert "staging" in result.output

    def test_dry_run_shows_git_fetch(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert "git fetch origin dev staging" in result.output

    def test_dry_run_shows_checkout(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert "git checkout -B sync/staging-from-dev origin/dev" in result.output

    def test_dry_run_shows_merge(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert "git merge origin/staging" in result.output

    def test_dry_run_shows_push(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert "git push origin sync/staging-from-dev" in result.output

    def test_dry_run_shows_pr_create(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert "gh pr create" in result.output
        assert "--base staging" in result.output
        assert "--head sync/staging-from-dev" in result.output

    def test_dry_run_shows_pr_merge(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert "gh pr merge --auto --squash" in result.output

    def test_dry_run_with_explicit_target(self, tmp_path):
        cfg = _setup(
            tmp_path,
            [
                {"source": "dev", "target": "staging"},
                {"source": "staging", "target": "prod"},
            ],
        )
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "prod", "--dry-run"])
        assert result.exit_code == 0
        assert "staging" in result.output
        assert "prod" in result.output
        assert "git fetch origin staging prod" in result.output


# ---------------------------------------------------------------------------
# CLI: branch force-create (issue #213, fragility 1)
# ---------------------------------------------------------------------------


class TestSyncBranchForceCreate:
    """Sync branch is force-created (-B) so a stale local branch from an
    interrupted prior run is silently overwritten on re-invocation."""

    SHA = "deadbeef" * 5
    MERGE_BASE = "cafe1234" * 5
    PR_URL = "https://github.com/org/repo/pull/42"

    def _side_effects(self):
        return [
            _mk(stdout="main\n"),  # git rev-parse HEAD
            _mk(),  # git fetch
            _mk(stdout=self.SHA + "\n"),  # git rev-parse origin/dev
            _mk(stdout=self.MERGE_BASE + "\n"),  # git merge-base
            _mk(returncode=0, stdout='{"version": "1.1.0"}'),  # version dev
            _mk(returncode=0, stdout='{"version": "1.0.0"}'),  # version staging
            _mk(),  # git checkout -B
            _mk(),  # git merge (clean)
            _no_source_deletions(),  # diff --filter=D pre-pass
            _in_merge(),  # MERGE_HEAD set
            _mk(),  # git commit pre-merge
            *_merge_finalize_tail(),
            _mk(),  # git push
            _mk(returncode=1),  # gh pr view (no existing PR)
            _mk(stdout=self.PR_URL + "\n"),  # gh pr create
            _mk(),  # gh pr merge
            _mk(),  # git checkout main
        ]

    def test_uses_capital_B_to_force_create(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = self._side_effects()
            CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        commands = [c[0][0] for c in m.call_args_list]
        checkout_create = [
            cmd
            for cmd in commands
            if len(cmd) >= 2
            and cmd[0] == "git"
            and cmd[1] == "checkout"
            and "-B" in cmd
        ]
        assert checkout_create, (
            "expected a `git checkout -B sync/...` call; got: "
            f"{[c for c in commands if 'checkout' in c]}"
        )

    def test_does_not_use_lowercase_b(self, tmp_path):
        """Regression guard: lowercase -b fails on a stale local branch."""
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = self._side_effects()
            CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        commands = [c[0][0] for c in m.call_args_list]
        lowercase = [
            cmd
            for cmd in commands
            if len(cmd) >= 3 and cmd[:2] == ["git", "checkout"] and "-b" in cmd
        ]
        assert not lowercase, (
            f"sync must not use `git checkout -b` (use -B); offenders: {lowercase}"
        )


# ---------------------------------------------------------------------------
# CLI: existing-PR detection (issue #213, fragility 2)
# ---------------------------------------------------------------------------


class TestSyncExistingPR:
    """After pushing, sync looks up an existing PR for the sync branch via
    `gh pr view`. OPEN → update + re-enable auto-merge + exit; CLOSED or
    MERGED → log the prior URL and open a fresh PR; absent → unchanged
    behavior (open a new PR)."""

    SHA = "deadbeef" * 5
    MERGE_BASE = "cafe1234" * 5
    EXISTING_PR_URL = "https://github.com/org/repo/pull/40"
    NEW_PR_URL = "https://github.com/org/repo/pull/42"

    def _calls_up_to_push(self):
        """Mocked subprocess calls from sync start through `git push`."""
        return [
            _mk(stdout="main\n"),  # git rev-parse HEAD
            _mk(),  # git fetch
            _mk(stdout=self.SHA + "\n"),  # git rev-parse origin/dev
            _mk(stdout=self.MERGE_BASE + "\n"),  # git merge-base
            _mk(returncode=0, stdout='{"version": "1.1.0"}'),  # version dev
            _mk(returncode=0, stdout='{"version": "1.0.0"}'),  # version staging
            _mk(),  # git checkout -B
            _mk(),  # git merge (clean)
            _no_source_deletions(),  # diff --filter=D pre-pass
            _in_merge(),  # MERGE_HEAD set
            _mk(),  # git commit pre-merge
            *_merge_finalize_tail(),
            _mk(),  # git push
        ]

    def _pr_view_open(self):
        return _mk(
            returncode=0,
            stdout=f'{{"url":"{self.EXISTING_PR_URL}","state":"OPEN"}}',
        )

    def _pr_view_closed(self):
        return _mk(
            returncode=0,
            stdout=f'{{"url":"{self.EXISTING_PR_URL}","state":"CLOSED"}}',
        )

    def _pr_view_merged(self):
        return _mk(
            returncode=0,
            stdout=f'{{"url":"{self.EXISTING_PR_URL}","state":"MERGED"}}',
        )

    def _pr_view_absent(self):
        return _mk(returncode=1, stderr="no pull requests found for branch")

    def test_updates_existing_open_pr_instead_of_creating(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                *self._calls_up_to_push(),
                self._pr_view_open(),
                _mk(),  # gh pr merge --auto --squash
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        commands = [c[0][0] for c in m.call_args_list]
        # gh pr create must NOT be called for an existing OPEN PR
        assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in commands), (
            f"gh pr create was called; commands: {commands}"
        )
        # gh pr merge --auto --squash <existing-url> IS called
        assert [
            "gh",
            "pr",
            "merge",
            "--auto",
            "--squash",
            self.EXISTING_PR_URL,
        ] in commands
        assert "updated" in result.output
        assert self.EXISTING_PR_URL in result.output

    def test_creates_new_pr_when_existing_is_closed(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                *self._calls_up_to_push(),
                self._pr_view_closed(),
                _mk(stdout=self.NEW_PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        commands = [c[0][0] for c in m.call_args_list]
        # gh pr create IS called for a CLOSED prior PR
        create_calls = [cmd for cmd in commands if cmd[:3] == ["gh", "pr", "create"]]
        assert len(create_calls) == 1, (
            f"expected 1 gh pr create call; got {create_calls}"
        )
        # New PR URL is the one in the success message
        assert self.NEW_PR_URL in result.output
        # Prior PR URL is surfaced as informational context
        assert self.EXISTING_PR_URL in result.output
        assert "closed" in result.output.lower()

    def test_creates_new_pr_when_existing_is_merged(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                *self._calls_up_to_push(),
                self._pr_view_merged(),
                _mk(stdout=self.NEW_PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        commands = [c[0][0] for c in m.call_args_list]
        assert any(cmd[:3] == ["gh", "pr", "create"] for cmd in commands)
        assert self.NEW_PR_URL in result.output
        assert self.EXISTING_PR_URL in result.output
        assert "merged" in result.output.lower()

    def test_creates_new_pr_when_no_existing_pr(self, tmp_path):
        """No PR exists for the sync branch → behavior unchanged: create one."""
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                *self._calls_up_to_push(),
                self._pr_view_absent(),
                _mk(stdout=self.NEW_PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        commands = [c[0][0] for c in m.call_args_list]
        assert any(cmd[:3] == ["gh", "pr", "create"] for cmd in commands)
        assert self.NEW_PR_URL in result.output
        # No prior PR mentioned
        assert self.EXISTING_PR_URL not in result.output

    def test_existing_open_pr_url_on_final_done_line(self, tmp_path):
        """Operators grep the final `==> Done.` line for the PR URL — it
        must be on the same line as 'Done.' even on the retry path."""
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                *self._calls_up_to_push(),
                self._pr_view_open(),
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        done_lines = [
            line
            for line in result.output.splitlines()
            if "Done." in line and self.EXISTING_PR_URL in line
        ]
        assert done_lines, (
            "expected the existing PR URL on the final `==> Done.` line; "
            f"output:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# Unit: _propagate_source_deletions (#235)
# ---------------------------------------------------------------------------


class TestPropagateSourceDeletions:
    """`git merge` silently keeps target's copy when source deleted a file
    target never touched. The pre-pass detects that case via the merge-base→
    source `--diff-filter=D` list and runs `git rm` to mirror the deletion."""

    def test_propagates_deletion_when_target_unchanged(self):
        from fraisier.cli.sync import _propagate_source_deletions

        with patch(_PATCH) as m:
            m.side_effect = [
                # diff --filter=D base origin/dev: source deleted legacy.sql
                _mk(stdout="db/legacy.sql\n"),
                # ls-files --error-unmatch: still in index
                _mk(returncode=0),
                # _target_unchanged_since_base: target unchanged
                _mk(returncode=0),
                # git rm
                _mk(),
            ]
            propagated = _propagate_source_deletions("base-sha", "dev", "staging")
        assert propagated == ["db/legacy.sql"]
        commands = [c[0][0] for c in m.call_args_list]
        assert ["git", "rm", "--", "db/legacy.sql"] in commands

    def test_does_not_propagate_when_target_modified(self):
        from fraisier.cli.sync import _propagate_source_deletions

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="db/legacy.sql\n"),  # source deleted
                _mk(returncode=0),  # still in index
                _mk(returncode=1),  # target MODIFIED since merge-base
            ]
            propagated = _propagate_source_deletions("base-sha", "dev", "staging")
        assert propagated == []
        commands = [c[0][0] for c in m.call_args_list]
        assert not any(cmd[:2] == ["git", "rm"] for cmd in commands), (
            "must not propagate a deletion when target modified the file — "
            "that's a real conflict for the operator to resolve"
        )

    def test_skips_when_file_no_longer_in_index(self):
        """If the merge already resolved a deletion (e.g. via tier 1 in a
        prior pass), `ls-files --error-unmatch` exits non-zero and we
        skip — no double-rm."""
        from fraisier.cli.sync import _propagate_source_deletions

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="db/legacy.sql\n"),
                _mk(returncode=1),  # not in index
            ]
            propagated = _propagate_source_deletions("base-sha", "dev", "staging")
        assert propagated == []

    def test_empty_when_no_deletions(self):
        from fraisier.cli.sync import _propagate_source_deletions

        with patch(_PATCH) as m:
            m.side_effect = [_mk(stdout="")]
            propagated = _propagate_source_deletions("base-sha", "dev", "staging")
        assert propagated == []

    def test_handles_multiple_deletions(self):
        from fraisier.cli.sync import _propagate_source_deletions

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="a.sql\nb.sql\nc.sql\n"),
                _mk(returncode=0),  # a in index
                _mk(returncode=0),  # a target unchanged
                _mk(),  # rm a
                _mk(returncode=0),  # b in index
                _mk(returncode=1),  # b target MODIFIED
                _mk(returncode=1),  # c NOT in index (already resolved)
            ]
            propagated = _propagate_source_deletions("base-sha", "dev", "staging")
        assert propagated == ["a.sql"]


# ---------------------------------------------------------------------------
# CLI: source-deletion propagation end-to-end (#235)
# ---------------------------------------------------------------------------


class TestSyncPropagatesSourceDeletions:
    """End-to-end regression for #235: source deletes a file, target unchanged,
    `git merge` succeeds clean. Pre-pass must `git rm` the file and the sync
    PR must carry the deletion forward."""

    SHA = "deadbeef" * 5
    MERGE_BASE = "cafe1234" * 5
    PR_URL = "https://github.com/org/repo/pull/42"

    def test_clean_merge_with_source_deletion_propagates(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(),  # git merge (clean — source-deleted file silently kept)
                # _propagate_source_deletions pre-pass:
                _mk(stdout="db/legacy.sql\n"),  # diff --filter=D
                _mk(returncode=0),  # ls-files --error-unmatch (in index)
                _mk(returncode=0),  # target unchanged since merge-base
                _mk(),  # git rm db/legacy.sql
                _in_merge(),  # MERGE_HEAD set
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(),  # git push
                _mk(returncode=1),  # gh pr view
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 0, result.output
        assert "Auto-resolved (source deletion): db/legacy.sql" in result.output
        commands = [c[0][0] for c in m.call_args_list]
        assert ["git", "rm", "--", "db/legacy.sql"] in commands


# ---------------------------------------------------------------------------
# Pre-push merge-commit invariant (#233 Layer 2)
# ---------------------------------------------------------------------------


class TestSyncAssertsMergeFinalized:
    """Defence-in-depth: if any code path manages to skip the merge commit,
    push must abort with a clear error rather than push a non-merge ref
    and produce a CONFLICTING PR on GitHub."""

    def test_aborts_when_head_has_one_parent(self):
        from fraisier.cli.sync import _assert_merge_finalized

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="single-parent-sha\n"),  # log -1 %P → 1 parent
                _mk(returncode=1),  # MERGE_HEAD cleared
            ]
            with pytest.raises(SystemExit) as exc:
                _assert_merge_finalized()
        assert exc.value.code == 1

    def test_aborts_when_merge_head_still_set(self):
        from fraisier.cli.sync import _assert_merge_finalized

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="parent-a parent-b\n"),  # 2 parents
                _mk(returncode=0),  # but MERGE_HEAD still set — corruption!
            ]
            with pytest.raises(SystemExit) as exc:
                _assert_merge_finalized()
        assert exc.value.code == 1

    def test_passes_when_two_parents_and_merge_cleared(self):
        from fraisier.cli.sync import _assert_merge_finalized

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="parent-a parent-b\n"),  # 2 parents (merge commit)
                _mk(returncode=1),  # MERGE_HEAD cleared
            ]
            _assert_merge_finalized()  # no raise

    def test_aborts_when_no_parents(self):
        """Defensive: empty parent list (initial commit edge case) also fails."""
        from fraisier.cli.sync import _assert_merge_finalized

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="\n"),  # no parents
                _mk(returncode=1),
            ]
            with pytest.raises(SystemExit) as exc:
                _assert_merge_finalized()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Unit: _commit_merge_or_staged (#233 Layer 1)
# ---------------------------------------------------------------------------


class TestCommitMergeOrStaged:
    """When MERGE_HEAD is set the helper commits unconditionally — even
    when the resolved tree matches HEAD byte-for-byte. Without a merge
    in progress, it falls back to commit-if-staged."""

    def test_commits_unconditionally_during_merge(self):
        from fraisier.cli.sync import _commit_merge_or_staged

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(returncode=0),  # MERGE_HEAD exists
                _mk(),  # git commit
            ]
            _commit_merge_or_staged("msg")
        commands = [c[0][0] for c in m.call_args_list]
        commits = [c for c in commands if "commit" in c]
        assert commits, "commit must fire when MERGE_HEAD is set"
        assert all("--no-verify" in c for c in commits)
        # Crucially: no `git diff --cached --quiet` probe in the merge path —
        # that probe was the #233 bug source.
        assert not any(c == ["git", "diff", "--cached", "--quiet"] for c in commands)

    def test_skips_commit_outside_merge_with_clean_index(self):
        from fraisier.cli.sync import _commit_merge_or_staged

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(returncode=1),  # no MERGE_HEAD
                _mk(returncode=0),  # diff --cached --quiet → nothing staged
            ]
            _commit_merge_or_staged("msg")
        commands = [c[0][0] for c in m.call_args_list]
        assert not any("commit" in c for c in commands)

    def test_commits_outside_merge_with_staged_changes(self):
        from fraisier.cli.sync import _commit_merge_or_staged

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(returncode=1),  # no MERGE_HEAD
                _mk(returncode=1),  # diff --cached --quiet → something staged
                _mk(),  # git commit
            ]
            _commit_merge_or_staged("msg")
        commands = [c[0][0] for c in m.call_args_list]
        assert any("commit" in c for c in commands)
