"""Tests for fraisier sync command."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
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


# Sentinel SHAs kept for grandfathered ordered-mock tests. Historically this
# answered `git log -1 --pretty=%P HEAD` (two parents); since #268 the
# pre-push guard probes `git merge-base --is-ancestor origin/<tgt> HEAD`
# instead, where only the mocked returncode (0 = contained) matters — the
# stdout is ignored, and the call count is unchanged.
_MERGE_PARENTS = "abcdef0123456789 fedcba9876543210\n"


def _no_source_deletions() -> MagicMock:
    """Mock the post-merge `git diff --filter=D` pre-pass returning nothing.

    Inserted after every `git merge` mock to satisfy `_propagate_source_deletions`,
    which fires once per sync run regardless of whether the merge had conflicts.
    """
    return _mk(stdout="")


def _no_source_reverts() -> MagicMock:
    """Mock the post-merge `git diff --filter=M` pre-pass returning nothing.

    The second half of #290. Sits immediately after `_no_source_deletions()`
    for the same reason: `_propagate_source_reverts` fires once per sync run
    whether or not the merge conflicted, because a source-side revert to base
    content merges cleanly and never reaches the conflict loop.
    """
    return _mk(stdout="")


def _merge_finalize_tail() -> list[MagicMock]:
    """Mock the post-commit pre-push invariant check (#233 Layer 2, #268).

    Sequence:
      1. `git merge-base --is-ancestor origin/<tgt> HEAD` exits 0
         (target contained in HEAD).
      2. `git rev-parse --verify --quiet MERGE_HEAD` exits 1 (merge cleared).
    """
    return [_mk(stdout=_MERGE_PARENTS), _mk(returncode=1)]


def _in_merge() -> MagicMock:
    """Mock the `git rev-parse --verify --quiet MERGE_HEAD` probe used by
    `_commit_merge_or_staged`: returncode 0 means a merge is in progress, so
    the helper proceeds straight to `git commit` (no diff --cached check)."""
    return _mk(returncode=0)


class MockGit:
    """Argv-prefix-matching subprocess.run mock.

    Replaces the legacy ``side_effect=[ordered, list, of, mocks]`` pattern
    that all of the older tests in this file use. Tests declare responses
    by command shape — ``("git", "rev-parse", "--verify", "--quiet",
    "MERGE_HEAD")`` — so a future subprocess call inserted between two
    existing ones doesn't break every test that touches the merge path.

    Matching: **longest prefix wins**, responses consumed FIFO. Commands
    that don't match anything return a successful empty ``_mk()`` — so
    harmless probes (``git status``, ``git rev-parse HEAD``) don't need
    explicit scripting.

    Preferred pattern for new tests. Older tests are grandfathered on the
    ordered-side_effect pattern.

    Usage:

        mg = (
            MockGit()
            .queue("git", "rev-parse", "--abbrev-ref", stdout="main\\n")
            .queue("git", "merge", returncode=1)
            .queue(
                "git", "diff", "--name-only", "--diff-filter=D",
                stdout="legacy.sql\\n",
            )
            .queue("git", "ls-files", "--error-unmatch")
            .queue("git", "diff", "--quiet")  # _target_unchanged_since_base
            .queue("git", "rm")
            .queue("git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD")
            .queue("git", "commit")
            .queue("git", "log", "-1", "--pretty=%P", stdout=_MERGE_PARENTS)
            .queue(
                "git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD",
                returncode=1,
            )
        )
        with patch(_PATCH, side_effect=mg):
            ...
        assert mg.was_called(["git", "rm", "--", "legacy.sql"])
    """

    def __init__(self) -> None:
        self._responses: dict[tuple[str, ...], list[MagicMock]] = {}
        self.calls: list[list[str]] = []

    def queue(
        self,
        *prefix: str,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> MockGit:
        """Append a response for `prefix`. Chainable."""
        key = tuple(prefix)
        self._responses.setdefault(key, []).append(
            _mk(returncode=returncode, stdout=stdout, stderr=stderr)
        )
        return self

    def __call__(self, cmd, *args, **kwargs):
        # Match only on the command argv, but honor `check=True` the way
        # real subprocess.run does: raise CalledProcessError on non-zero
        # exit. Without this, mocks pass `check=True` silently and tests
        # can't tell the difference between success and failure.
        del args
        self.calls.append(list(cmd))
        cmd_tuple = tuple(cmd)
        best_key: tuple[str, ...] | None = None
        for key in self._responses:
            if (
                len(cmd_tuple) >= len(key)
                and cmd_tuple[: len(key)] == key
                and self._responses[key]
            ):
                if best_key is None or len(key) > len(best_key):
                    best_key = key
        result = self._responses[best_key].pop(0) if best_key else _mk()
        if kwargs.get("check") and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                list(cmd),
                output=result.stdout,
                stderr=result.stderr,
            )
        return result

    def was_called(self, expected: list[str]) -> bool:
        """True if any recorded call argv exactly matched `expected`."""
        return expected in self.calls

    def calls_with_prefix(self, *prefix: str) -> list[list[str]]:
        """Return all recorded calls whose argv starts with `prefix`."""
        p = tuple(prefix)
        return [c for c in self.calls if tuple(c[: len(p)]) == p]


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


class TestTargetBlobIsSourceDerived:
    """Replaces TestTargetUnchangedSinceBase.

    `_target_unchanged_since_base` was removed with #290: under squash-merge
    promotion the merge-base never advances, so "unchanged since merge-base"
    was False for nearly every path. The question is now "is target holding
    source-derived content?", answered by walking source's history for the
    path. Behavioural coverage lives in tests/test_sync_deletion_squash.py,
    which uses real repositories rather than an ordered subprocess script;
    what is pinned here is the fail-closed posture.
    """

    def test_returns_false_when_target_blob_cannot_be_read(self):
        from fraisier.cli.sync import _target_blob_is_source_derived

        with patch(_PATCH) as m:
            m.side_effect = [_mk(returncode=128, stdout="")]
            assert _target_blob_is_source_derived("dev", "staging", "x.py") is False

    def test_returns_false_when_rev_list_fails(self):
        from fraisier.cli.sync import _target_blob_is_source_derived

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="blob1\n"),
                _mk(returncode=128, stdout=""),
            ]
            assert _target_blob_is_source_derived("dev", "staging", "x.py") is False

    def test_returns_true_on_blob_match(self):
        from fraisier.cli.sync import _target_blob_is_source_derived

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="blob1\n"),  # target blob
                _mk(stdout="shaA\n"),  # source history for the path
                _mk(stdout="blob1\n"),  # same content on source
            ]
            assert _target_blob_is_source_derived("dev", "staging", "x.py") is True

    def test_returns_false_when_no_source_commit_matches(self):
        from fraisier.cli.sync import _target_blob_is_source_derived

        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout="blob-target\n"),
                _mk(stdout="shaA\n"),
                _mk(stdout="blob-other\n"),
            ]
            assert _target_blob_is_source_derived("dev", "staging", "x.py") is False


class TestSourceDeletedPath:
    """Gate 1: did source's own history remove this path?"""

    def test_true_when_log_reports_a_deleting_commit(self):
        from fraisier.cli.sync import _source_deleted_path

        with patch(_PATCH) as m:
            m.side_effect = [_mk(stdout="shaD\n")]
            assert _source_deleted_path("dev", "gone.py") is True

    def test_false_when_source_never_had_the_path(self):
        from fraisier.cli.sync import _source_deleted_path

        with patch(_PATCH) as m:
            m.side_effect = [_mk(stdout="")]
            assert _source_deleted_path("dev", "target-only.md") is False

    def test_false_on_git_error(self):
        from fraisier.cli.sync import _source_deleted_path

        with patch(_PATCH) as m:
            m.side_effect = [_mk(returncode=128, stdout="")]
            assert _source_deleted_path("dev", "x.py") is False

    def test_excludes_merge_commits(self):
        """--no-merges keeps the answer about a real authored deletion."""
        from fraisier.cli.sync import _source_deleted_path

        with patch(_PATCH) as m:
            m.side_effect = [_mk(stdout="shaD\n")]
            _source_deleted_path("dev", "x.py")
            assert "--no-merges" in m.call_args_list[0][0][0]


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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
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
            _mk(stdout=""),  # status --porcelain: clean worktree (#268)
            _mk(stdout="main\n"),  # git rev-parse HEAD
            _mk(),  # git fetch
            _mk(stdout=self.SHA + "\n"),  # git rev-parse origin/dev
            _mk(stdout=self.MERGE_BASE + "\n"),  # git merge-base
            _mk(returncode=0, stdout='{"version": "1.1.0"}'),  # version dev
            _mk(returncode=0, stdout='{"version": "1.0.0"}'),  # version staging
            _mk(),  # git checkout -b
            _mk(),  # git merge (clean)
            _no_source_deletions(),  # diff --filter=D pre-pass
            _no_source_reverts(),  # diff --filter=M pre-pass
            _in_merge(),  # rev-parse MERGE_HEAD (in merge)
            _mk(),  # git commit pre-merge
            *_merge_finalize_tail(),  # pre-push merge-commit invariant check
            _mk(returncode=2),  # ls-remote: orphan branch absent
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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(),  # git merge (clean)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
                _in_merge(),  # MERGE_HEAD set → commit unconditionally
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(returncode=2),  # ls-remote: orphan branch absent
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


class TestSyncAutoMergeFallback:
    """Regressions for #244: `gh pr merge --auto` fails on PRs in "clean"
    status (target branch has no required checks). Sync must degrade
    gracefully to immediate merge instead of aborting."""

    SHA = "deadbeef" * 5
    MERGE_BASE = "cafe1234" * 5
    PR_URL = "https://github.com/org/repo/pull/77"

    _CLEAN_STATUS_STDERR = (
        "GraphQL: Pull request Pull request is in clean status "
        "(enablePullRequestAutoMerge)\n"
    )

    def test_sync_falls_back_to_immediate_merge_when_pr_is_clean(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])

        mg = (
            MockGit()
            .queue("git", "rev-parse", "HEAD", stdout="main\n")
            .queue("git", "fetch")
            .queue("git", "rev-parse", "origin/dev", stdout=self.SHA + "\n")
            .queue("git", "merge-base", stdout=self.MERGE_BASE + "\n")
            .queue(
                "git",
                "show",
                "origin/dev:version.json",
                returncode=0,
                stdout='{"version": "1.1.0"}',
            )
            .queue(
                "git",
                "show",
                "origin/staging:version.json",
                returncode=0,
                stdout='{"version": "1.0.0"}',
            )
            .queue("git", "checkout", "-b")
            .queue("git", "merge")
            .queue(
                "git", "diff", "--name-only", "--diff-filter=D", stdout=""
            )  # _no_source_deletions
            .queue("git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD")  # _in_merge
            .queue("git", "commit")
            .queue("git", "log", "-1", "--pretty=%P", stdout=_MERGE_PARENTS)
            .queue(
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "MERGE_HEAD",
                returncode=1,
            )
            .queue("git", "ls-remote", returncode=2)  # orphan absent
            .queue("git", "push")
            .queue("gh", "pr", "view", returncode=1)  # no existing PR
            .queue("gh", "pr", "create", stdout=self.PR_URL + "\n")
            # The auto-merge attempt fails with the clean-status stderr…
            .queue(
                "gh",
                "pr",
                "merge",
                "--auto",
                "--squash",
                returncode=1,
                stderr=self._CLEAN_STATUS_STDERR,
            )
            # …and the fallback (no --auto) succeeds.
            .queue("gh", "pr", "merge", "--squash")
            .queue("git", "checkout", "main")
        )

        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])

        assert result.exit_code == 0, result.output
        assert mg.was_called(["gh", "pr", "merge", "--squash", self.PR_URL]), (
            f"expected fallback merge; got calls: {mg.calls}"
        )

    def test_sync_propagates_unrelated_gh_failures(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])

        mg = (
            MockGit()
            .queue("git", "rev-parse", "HEAD", stdout="main\n")
            .queue("git", "fetch")
            .queue("git", "rev-parse", "origin/dev", stdout=self.SHA + "\n")
            .queue("git", "merge-base", stdout=self.MERGE_BASE + "\n")
            .queue(
                "git",
                "show",
                "origin/dev:version.json",
                returncode=0,
                stdout='{"version": "1.1.0"}',
            )
            .queue(
                "git",
                "show",
                "origin/staging:version.json",
                returncode=0,
                stdout='{"version": "1.0.0"}',
            )
            .queue("git", "checkout", "-b")
            .queue("git", "merge")
            .queue("git", "diff", "--name-only", "--diff-filter=D", stdout="")
            .queue("git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD")
            .queue("git", "commit")
            .queue("git", "log", "-1", "--pretty=%P", stdout=_MERGE_PARENTS)
            .queue(
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "MERGE_HEAD",
                returncode=1,
            )
            .queue("git", "push")
            .queue("gh", "pr", "view", returncode=1)
            .queue("gh", "pr", "create", stdout=self.PR_URL + "\n")
            .queue(
                "gh",
                "pr",
                "merge",
                "--auto",
                "--squash",
                returncode=1,
                stderr="HTTP 403: Forbidden\n",
            )
            .queue("git", "checkout", "main")
            .queue("git", "branch", "-D")
        )

        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])

        assert result.exit_code != 0
        # The fallback (no --auto) must NOT have been invoked.
        fallback_calls = [
            c for c in mg.calls if c[:4] == ["gh", "pr", "merge", "--squash"]
        ]
        assert not fallback_calls, (
            f"unrelated gh failure should not trigger fallback; got: {fallback_calls}"
        )


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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
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
                _mk(returncode=2),  # ls-remote: orphan branch absent
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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
                _mk(stdout="version.json\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:version.json (exists)
                _mk(),  # checkout origin/dev -- version.json
                _mk(),  # git add version.json
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set → commit unconditionally
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(returncode=2),  # ls-remote: orphan branch absent
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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
                _mk(stdout="pyproject.toml\n"),  # diff --filter=U
                _mk(returncode=0),  # cat-file origin/dev:pyproject.toml (exists)
                _mk(),  # checkout origin/dev -- pyproject.toml
                _mk(),  # git add pyproject.toml
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set → commit unconditionally
                _mk(),  # git commit (merge commit, even with no tree diff)
                *_merge_finalize_tail(),
                _mk(returncode=2),  # ls-remote: orphan branch absent
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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
                _mk(stdout="old_script.py\n"),  # diff --filter=U (first)
                _mk(
                    returncode=1
                ),  # cat-file origin/dev:old_script.py (not in source → deleted)
                _mk(),  # git rm old_script.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(returncode=2),  # ls-remote: orphan branch absent
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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),
                _mk(returncode=1),
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
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
        assert "fraisier/sync/staging-from-dev" in deletes[0]

    def test_auto_resolves_target_behind_source(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:src/routes.py (exists)
                # Tier 3 now asks "is staging's blob source-derived?" instead
                # of "unchanged since merge-base" — the merge-base is
                # permanently stale under squash promotion (#290).
                _mk(stdout="blob1\n"),  # rev-parse origin/staging:src/routes.py
                _mk(stdout="shaA\n"),  # rev-list dev history for the path
                _mk(stdout="blob1\n"),  # rev-parse shaA:src/routes.py — match
                _mk(),  # checkout origin/dev -- src/routes.py
                _mk(),  # git add src/routes.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _in_merge(),  # MERGE_HEAD set
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(returncode=2),  # ls-remote: orphan branch absent
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
            "Auto-resolved (staging holds source-derived content): src/routes.py"
            in result.output
        )

    def test_prefer_source_flag_resolves_diverged_file(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH) as m:
            m.side_effect = [
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
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
                _mk(returncode=2),  # ls-remote: orphan branch absent
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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
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
                _mk(returncode=2),  # ls-remote: orphan branch absent
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
                _mk(stdout=""),  # status --porcelain: clean worktree (#268)
                _mk(stdout="main\n"),
                _mk(),
                _mk(stdout=self.SHA + "\n"),
                _mk(stdout=self.MERGE_BASE + "\n"),
                _mk(returncode=0, stdout='{"version": "1.1.0"}'),
                _mk(returncode=0, stdout='{"version": "1.0.0"}'),
                _mk(),  # git checkout -b
                _mk(returncode=1),  # git merge (conflict)
                _no_source_deletions(),  # diff --filter=D pre-pass
                _no_source_reverts(),  # diff --filter=M pre-pass
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
                _mk(returncode=2),  # ls-remote: orphan branch absent
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
            _mk(stdout=""),  # status --porcelain: clean worktree (#268)
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
                _no_source_reverts(),  # diff --filter=M pre-pass
                _in_merge(),  # MERGE_HEAD set
                _mk(),  # git commit
                *_merge_finalize_tail(),
                _mk(returncode=2),  # ls-remote: orphan branch absent
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
        assert (
            "git checkout -B fraisier/sync/staging-from-dev origin/dev" in result.output
        )

    def test_dry_run_shows_merge(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert "git merge origin/staging" in result.output

    def test_dry_run_shows_push(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert "git push origin fraisier/sync/staging-from-dev" in result.output

    def test_dry_run_shows_pr_create(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        with patch(_PATCH):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--dry-run"])
        assert "gh pr create" in result.output
        assert "--base staging" in result.output
        assert "--head fraisier/sync/staging-from-dev" in result.output

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
            _mk(stdout=""),  # status --porcelain: clean worktree (#268)
            _mk(stdout="main\n"),  # git rev-parse HEAD
            _mk(),  # git fetch
            _mk(stdout=self.SHA + "\n"),  # git rev-parse origin/dev
            _mk(stdout=self.MERGE_BASE + "\n"),  # git merge-base
            _mk(returncode=0, stdout='{"version": "1.1.0"}'),  # version dev
            _mk(returncode=0, stdout='{"version": "1.0.0"}'),  # version staging
            _mk(),  # git checkout -B
            _mk(),  # git merge (clean)
            _no_source_deletions(),  # diff --filter=D pre-pass
            _no_source_reverts(),  # diff --filter=M pre-pass
            _in_merge(),  # MERGE_HEAD set
            _mk(),  # git commit pre-merge
            *_merge_finalize_tail(),
            _mk(returncode=2),  # ls-remote: orphan branch absent
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
            _mk(stdout=""),  # status --porcelain: clean worktree (#268)
            _mk(stdout="main\n"),  # git rev-parse HEAD
            _mk(),  # git fetch
            _mk(stdout=self.SHA + "\n"),  # git rev-parse origin/dev
            _mk(stdout=self.MERGE_BASE + "\n"),  # git merge-base
            _mk(returncode=0, stdout='{"version": "1.1.0"}'),  # version dev
            _mk(returncode=0, stdout='{"version": "1.0.0"}'),  # version staging
            _mk(),  # git checkout -B
            _mk(),  # git merge (clean)
            _no_source_deletions(),  # diff --filter=D pre-pass
            _no_source_reverts(),  # diff --filter=M pre-pass
            _in_merge(),  # MERGE_HEAD set
            _mk(),  # git commit pre-merge
            *_merge_finalize_tail(),
            _mk(returncode=2),  # ls-remote: orphan branch absent
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
# CLI: source-deletion propagation end-to-end (#235)
# ---------------------------------------------------------------------------


_PROP_SHA = "deadbeef" * 5
_PROP_MERGE_BASE = "cafe1234" * 5
_PROP_PR_URL = "https://github.com/org/repo/pull/42"


def _propagation_scaffold() -> MockGit:
    """Common scaffolding for the two post-merge pre-passes (#235, #290).

    Covers rev-parse, fetch, version reads, checkout, push and the gh PR ops.
    Tests layer on the merge + propagation calls they care about.
    """
    return (
        MockGit()
        .queue("git", "rev-parse", "--abbrev-ref", stdout="main\n")
        .queue("git", "rev-parse", "origin/dev", stdout=_PROP_SHA + "\n")
        .queue("git", "merge-base", stdout=_PROP_MERGE_BASE + "\n")
        .queue("git", "show", "origin/dev:version.json", stdout='{"version": "1.1.0"}')
        .queue(
            "git", "show", "origin/staging:version.json", stdout='{"version": "1.0.0"}'
        )
        .queue("gh", "pr", "view", returncode=1)
        .queue("gh", "pr", "create", stdout=_PROP_PR_URL + "\n")
    )


class TestSyncPropagatesSourceDeletions:
    """End-to-end regression for #235: source deletes a file, target unchanged,
    `git merge` succeeds clean. Pre-pass must `git rm` the file and the sync
    PR must carry the deletion forward.

    Uses the `MockGit` helper instead of the legacy ordered-side_effect
    pattern, so this test is robust to non-relevant subprocess calls being
    added or reordered elsewhere in `sync_cmd`.
    """

    def _scaffold_mockgit(self) -> MockGit:
        return _propagation_scaffold()

    def test_clean_merge_with_source_deletion_propagates(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        mg = (
            self._scaffold_mockgit()
            # Clean merge — source-deleted file silently kept by `git merge`.
            .queue("git", "merge")
            # Pre-pass sees the deletion and propagates it:
            .queue(
                "git",
                "diff",
                "--name-only",
                "-z",
                stdout="db/legacy.sql\0",
            )
            .queue("git", "ls-files", "--error-unmatch")  # in index
            # gate 1: source history shows the deletion
            .queue("git", "log", "--max-count=1", stdout="sha1\n")
            # gate 2: target holds source-derived content
            .queue("git", "rev-parse", stdout="blob1\n")
            .queue("git", "rev-list", stdout="sha1\n")
            .queue("git", "rev-parse", stdout="blob1\n")
            .queue("git", "rm")  # propagation
            # Merge finalize: MERGE_HEAD set → commit; assert sees 2 parents
            # and MERGE_HEAD cleared.
            .queue("git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD")
            .queue("git", "commit")
            .queue("git", "log", "-1", "--pretty=%P", stdout=_MERGE_PARENTS)
            .queue(
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "MERGE_HEAD",
                returncode=1,
            )
            .queue("git", "ls-remote", returncode=2)  # orphan absent
        )
        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 0, result.output
        assert "Auto-resolved (source deletion): db/legacy.sql" in result.output
        assert mg.was_called(["git", "rm", "-f", "--", "db/legacy.sql"])

    def test_rename_on_source_propagates_old_path_deletion(self, tmp_path):
        """Headline use-case from #235: source renames a file (`git mv old new`).
        Under git's default rename detection threshold, the merge-base→source
        diff shows `D old` + `A new`. Target hasn't touched `old`, so the
        pre-pass `git rm`s it; `new` arrives naturally via `git merge`.
        Net result on the sync branch: only `new` survives.
        """
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        mg = (
            self._scaffold_mockgit()
            .queue("git", "merge")
            # Source renamed db/old.sql → db/new.sql; diff --filter=D reports
            # the old-path deletion (the addition arrives via the merge).
            .queue(
                "git",
                "diff",
                "--name-only",
                "-z",
                stdout="db/old.sql\0",
            )
            .queue("git", "ls-files", "--error-unmatch")
            # gate 1: source history shows the deletion
            .queue("git", "log", "--max-count=1", stdout="sha1\n")
            # gate 2: target holds source-derived content
            .queue("git", "rev-parse", stdout="blob1\n")
            .queue("git", "rev-list", stdout="sha1\n")
            .queue("git", "rev-parse", stdout="blob1\n")
            .queue("git", "rm")
            .queue("git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD")
            .queue("git", "commit")
            .queue("git", "log", "-1", "--pretty=%P", stdout=_MERGE_PARENTS)
            .queue(
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "MERGE_HEAD",
                returncode=1,
            )
            .queue("git", "ls-remote", returncode=2)  # orphan absent
        )
        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 0, result.output
        assert "Auto-resolved (source deletion): db/old.sql" in result.output
        assert mg.was_called(["git", "rm", "-f", "--", "db/old.sql"])

    def test_failed_git_rm_does_not_appear_in_propagated_log(self, tmp_path):
        """If `git rm` fails (submodule, sparse-checkout exclusion), the
        operator must NOT see 'Auto-resolved (source deletion): …' — that
        line is a promise, not a wish. Stderr from git is surfaced as a
        warning instead.
        """
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        mg = (
            self._scaffold_mockgit()
            .queue("git", "merge")
            .queue(
                "git",
                "diff",
                "--name-only",
                "-z",
                stdout="submodules/legacy\0",
            )
            .queue("git", "ls-files", "--error-unmatch")
            # gate 1: source history shows the deletion
            .queue("git", "log", "--max-count=1", stdout="sha1\n")
            # gate 2: target holds source-derived content
            .queue("git", "rev-parse", stdout="blob1\n")
            .queue("git", "rev-list", stdout="sha1\n")
            .queue("git", "rev-parse", stdout="blob1\n")
            .queue(
                "git",
                "rm",
                returncode=1,
                stderr="fatal: pathspec is a submodule\n",
            )
            # No MERGE_HEAD → fall through to commit-if-staged.
            # Clean merge here would have nothing staged (the only would-be
            # change failed to apply), but `git merge --no-commit` still
            # leaves MERGE_HEAD set, so the helper commits regardless.
            .queue("git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD")
            .queue("git", "commit")
            .queue("git", "log", "-1", "--pretty=%P", stdout=_MERGE_PARENTS)
            .queue(
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "MERGE_HEAD",
                returncode=1,
            )
            .queue("git", "ls-remote", returncode=2)  # orphan absent
        )
        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 0, result.output
        assert (
            "Auto-resolved (source deletion): submodules/legacy" not in result.output
        ), (
            "must not claim auto-resolved when `git rm` failed; that would "
            "lie to the operator"
        )
        # A warning should appear telling the operator the propagation failed.
        assert (
            "could not propagate deletion" in result.output
            or "submodules/legacy" in result.output
        )


class TestSyncPropagatesSourceReverts:
    """End-to-end for #290's content-revert half: source reverts a promoted
    file back to its base content, so `git merge` resolves it silently in
    target's favour and the conflict loop never sees it.

    Shares `_propagation_scaffold` with the deletion pre-pass above; the
    behaviour under test is the second pre-pass, which runs right after it.
    """

    def test_clean_merge_with_source_revert_restores_source_content(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        mg = (
            _propagation_scaffold()
            # Clean merge — the revert is silently resolved in target's favour.
            .queue("git", "merge")
            # Deletion pre-pass finds nothing...
            .queue("git", "diff", "--name-only", "-z", stdout="")
            # ...then the revert pre-pass reports the modified path. Both share
            # the same argv prefix, so these are consumed FIFO in call order.
            .queue("git", "diff", "--name-only", "-z", stdout="app/shared.py\0")
            # target blob, then the merged index blob — equal, so the merge
            # took target's side wholesale.
            .queue("git", "rev-parse", stdout="blob1\n")
            .queue("git", "rev-parse", stdout="blob1\n")
            # gate 3: target holds source-derived content
            .queue("git", "rev-parse", stdout="blob1\n")
            .queue("git", "rev-list", stdout="sha1\n")
            .queue("git", "rev-parse", stdout="blob1\n")
            .queue("git", "checkout", "origin/dev")  # the restore
            .queue("git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD")
            .queue("git", "commit")
            .queue("git", "log", "-1", "--pretty=%P", stdout=_MERGE_PARENTS)
            .queue(
                "git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD", returncode=1
            )
            .queue("git", "ls-remote", returncode=2)  # orphan absent
        )
        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Auto-resolved (source revert): app/shared.py" in result.output
        assert mg.was_called(["git", "checkout", "origin/dev", "--", "app/shared.py"])

    def test_target_authored_content_is_not_restored(self, tmp_path):
        """Same merge shape, but target's blob never appears in source's
        history — the operator keeps target's version and is told so."""
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        mg = (
            _propagation_scaffold()
            .queue("git", "merge")
            .queue("git", "diff", "--name-only", "-z", stdout="")
            .queue("git", "diff", "--name-only", "-z", stdout="app/hotfix.py\0")
            .queue("git", "rev-parse", stdout="blobT\n")  # target blob
            .queue("git", "rev-parse", stdout="blobT\n")  # merged == target
            # gate 3 fails: source's history of the path never held blobT
            .queue("git", "rev-parse", stdout="blobT\n")
            .queue("git", "rev-list", stdout="sha1\n")
            .queue("git", "rev-parse", stdout="blobOTHER\n")
            .queue("git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD")
            .queue("git", "commit")
            .queue("git", "log", "-1", "--pretty=%P", stdout=_MERGE_PARENTS)
            .queue(
                "git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD", returncode=1
            )
            .queue("git", "ls-remote", returncode=2)
        )
        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Auto-resolved (source revert): app/hotfix.py" not in result.output
        assert not mg.was_called(
            ["git", "checkout", "origin/dev", "--", "app/hotfix.py"]
        )
        assert "not source-derived" in result.output


# ---------------------------------------------------------------------------
# Pre-push merge-commit invariant (#233 Layer 2)
# ---------------------------------------------------------------------------


class TestSyncAssertsMergeFinalized:
    """Defence-in-depth: before pushing, ``origin/<tgt>`` must be an ancestor
    of HEAD and MERGE_HEAD must be cleared — the exact condition under which
    GitHub will NOT mark the sync PR CONFLICTING. A two-parent merge commit
    satisfies it, and so does the "Already up to date" merge where the target
    is strictly behind the source and no merge commit exists (#268).
    """

    def test_passes_when_target_contained_and_merge_cleared(self):
        from fraisier.cli.sync import _assert_merge_finalized

        mg = (
            MockGit()
            .queue("git", "merge-base", "--is-ancestor", returncode=0)
            .queue(
                "git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD", returncode=1
            )
        )
        with patch(_PATCH, side_effect=mg):
            _assert_merge_finalized("staging")  # no raise
        assert mg.was_called(
            ["git", "merge-base", "--is-ancestor", "origin/staging", "HEAD"]
        )

    def test_aborts_when_target_not_ancestor_of_head(self, capsys):
        """The #268 shape: HEAD never absorbed origin/<tgt> (merge never
        started, or the merge parent was dropped). Pushing would produce a
        CONFLICTING PR — abort with diagnostics, since reaching this guard
        now means an unknown fraisier bug."""
        from fraisier.cli.sync import _assert_merge_finalized

        mg = (
            MockGit()
            .queue("git", "merge-base", "--is-ancestor", returncode=1)
            .queue(
                "git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD", returncode=1
            )
            .queue("git", "log", "-1", "--pretty=%P", stdout="only-parent-sha\n")
            .queue("git", "log", "-1", "--oneline", stdout="abc1234 some subject\n")
            .queue("git", "status", "--porcelain", stdout="?? report.txt\n")
        )
        with patch(_PATCH, side_effect=mg), pytest.raises(SystemExit) as exc:
            _assert_merge_finalized("staging")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        # Diagnostics must be actionable: HEAD summary, parents, worktree
        # snapshot — "file an issue with the output above" needs output.
        assert "abc1234" in err
        assert "only-parent-sha" in err
        assert "report.txt" in err
        assert "file an issue" in err

    def test_aborts_when_merge_head_still_set(self):
        from fraisier.cli.sync import _assert_merge_finalized

        mg = (
            MockGit()
            .queue("git", "merge-base", "--is-ancestor", returncode=0)
            .queue(
                "git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD", returncode=0
            )
        )
        with patch(_PATCH, side_effect=mg), pytest.raises(SystemExit) as exc:
            _assert_merge_finalized("staging")
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


# ---------------------------------------------------------------------------
# Issue #248: fraisier/* namespace + orphan-branch reclaim
# ---------------------------------------------------------------------------


class TestSyncBranchName:
    def test_returns_branch_under_fraisier_namespace(self):
        from fraisier.cli.sync import _sync_branch_name

        assert _sync_branch_name("staging", "dev") == "fraisier/sync/staging-from-dev"


class TestReclaimOrphanBranch:
    def test_deletes_when_prior_pr_merged(self):
        from fraisier.cli.sync import _reclaim_orphan_branch_if_safe

        with (
            patch("fraisier.cli.sync._remote_branch_exists", return_value=True),
            patch(
                "fraisier.cli.sync._find_existing_pr",
                return_value={"state": "MERGED", "url": "https://x/pr/1"},
            ),
            patch(_PATCH) as run_mock,
        ):
            run_mock.return_value = _mk()
            result = _reclaim_orphan_branch_if_safe("fraisier/sync/staging-from-dev")

        assert result is True
        delete_calls = [
            c.args[0] for c in run_mock.call_args_list if "--delete" in c.args[0]
        ]
        assert delete_calls == [
            [
                "git",
                "push",
                "origin",
                "--delete",
                "fraisier/sync/staging-from-dev",
            ]
        ]

    def test_skips_when_prior_pr_open(self):
        from fraisier.cli.sync import _reclaim_orphan_branch_if_safe

        with (
            patch("fraisier.cli.sync._remote_branch_exists", return_value=True),
            patch(
                "fraisier.cli.sync._find_existing_pr",
                return_value={"state": "OPEN", "url": "https://x/pr/2"},
            ),
            patch(_PATCH) as run_mock,
        ):
            result = _reclaim_orphan_branch_if_safe("fraisier/sync/staging-from-dev")

        assert result is False
        delete_calls = [c for c in run_mock.call_args_list if "--delete" in c.args[0]]
        assert delete_calls == []

    def test_noop_when_no_remote_branch(self):
        from fraisier.cli.sync import _reclaim_orphan_branch_if_safe

        with (
            patch("fraisier.cli.sync._remote_branch_exists", return_value=False),
            patch("fraisier.cli.sync._find_existing_pr") as pr_mock,
            patch(_PATCH) as run_mock,
        ):
            result = _reclaim_orphan_branch_if_safe("fraisier/sync/staging-from-dev")

        assert result is True
        pr_mock.assert_not_called()
        assert run_mock.call_args_list == []

    def test_deletes_orphan_with_no_pr_history(self):
        from fraisier.cli.sync import _reclaim_orphan_branch_if_safe

        with (
            patch("fraisier.cli.sync._remote_branch_exists", return_value=True),
            patch("fraisier.cli.sync._find_existing_pr", return_value=None),
            patch(_PATCH) as run_mock,
        ):
            run_mock.return_value = _mk()
            result = _reclaim_orphan_branch_if_safe("fraisier/sync/staging-from-dev")

        assert result is True
        delete_calls = [
            c.args[0] for c in run_mock.call_args_list if "--delete" in c.args[0]
        ]
        assert len(delete_calls) == 1

    def test_refuses_non_fraisier_namespace(self):
        from fraisier.cli.sync import _reclaim_orphan_branch_if_safe

        with pytest.raises(ValueError, match="refusing to reclaim"):
            _reclaim_orphan_branch_if_safe("sync/staging-from-dev")

    def test_delete_failure_does_not_raise(self):
        """Race: branch vanishes between exists-check and --delete.

        Plan resolution #1: subprocess.run(..., check=False) on the delete.
        """
        from fraisier.cli.sync import _reclaim_orphan_branch_if_safe

        with (
            patch("fraisier.cli.sync._remote_branch_exists", return_value=True),
            patch("fraisier.cli.sync._find_existing_pr", return_value=None),
            patch(_PATCH) as run_mock,
        ):
            run_mock.return_value = _mk(
                returncode=1, stderr="remote ref does not exist"
            )
            result = _reclaim_orphan_branch_if_safe("fraisier/sync/staging-from-dev")

        assert result is True


class TestNonFastForwardSniffer:
    @pytest.mark.parametrize(
        "stderr",
        [
            "! [rejected] foo (non-fast-forward)\n",
            "hint: Updates were rejected ... fetch first\n",
        ],
    )
    def test_matches_canonical_git_wordings(self, stderr):
        from fraisier.cli.sync import _is_non_fast_forward_rejection

        exc = subprocess.CalledProcessError(
            1, ["git", "push"], output="", stderr=stderr
        )
        assert _is_non_fast_forward_rejection(exc) is True

    def test_does_not_match_unrelated_failures(self):
        from fraisier.cli.sync import _is_non_fast_forward_rejection

        exc = subprocess.CalledProcessError(
            1, ["git", "push"], output="", stderr="fatal: Authentication failed"
        )
        assert _is_non_fast_forward_rejection(exc) is False


class TestPushSyncBranch:
    """Cycle 2: pre-flight + retry behaviour around `git push`."""

    def test_clean_push_after_preflight_reclaim(self):
        """Pre-flight reclaim runs first; push then succeeds without retry."""
        from fraisier.cli.sync import _push_sync_branch

        with (
            patch(
                "fraisier.cli.sync._reclaim_orphan_branch_if_safe",
                return_value=True,
            ) as reclaim_mock,
            patch(_PATCH) as run_mock,
        ):
            run_mock.return_value = _mk(returncode=0)
            _push_sync_branch("fraisier/sync/staging-from-dev")

        reclaim_mock.assert_called_once_with("fraisier/sync/staging-from-dev")
        push_calls = [c.args[0] for c in run_mock.call_args_list]
        assert push_calls == [
            ["git", "push", "origin", "fraisier/sync/staging-from-dev"]
        ]
        # LC_ALL=C set so the sniffer can rely on English stderr.
        env = run_mock.call_args_list[0].kwargs.get("env") or {}
        assert env.get("LC_ALL") == "C"

    def test_retries_after_non_fast_forward_when_pr_now_merged(self):
        """Race: PR was OPEN at pre-flight, merged before push, push rejects.

        The retry path re-runs the reclaim (now sees MERGED), deletes the
        branch, and pushes a second time. This anchors specifically on the
        retry — pre-flight returns False (live PR), so the reclaim only
        bites on the second call.
        """
        from fraisier.cli.sync import _push_sync_branch

        with (
            patch(
                "fraisier.cli.sync._reclaim_orphan_branch_if_safe",
                side_effect=[False, True],
            ) as reclaim_mock,
            patch(_PATCH) as run_mock,
        ):
            run_mock.side_effect = [
                _mk(
                    returncode=1,
                    stderr="! [rejected] foo (non-fast-forward)\n",
                ),
                _mk(returncode=0),
            ]
            _push_sync_branch("fraisier/sync/staging-from-dev")

        assert reclaim_mock.call_count == 2
        push_argvs = [c.args[0] for c in run_mock.call_args_list]
        assert push_argvs == [
            ["git", "push", "origin", "fraisier/sync/staging-from-dev"],
            ["git", "push", "origin", "fraisier/sync/staging-from-dev"],
        ]
        # No force-with-lease on this path — orphan reclaim cleared the way.
        assert not any("--force-with-lease" in argv for argv in push_argvs)

    def test_force_with_lease_only_when_live_pr_blocks_retry(self):
        """If the PR stays OPEN on retry, fall back to --force-with-lease.

        Force-push is scoped to the fraisier-owned namespace and only fires
        on the live-PR-race case — per plan §2.2.
        """
        from fraisier.cli.sync import _push_sync_branch

        with (
            patch(
                "fraisier.cli.sync._reclaim_orphan_branch_if_safe",
                side_effect=[False, False],
            ),
            patch(_PATCH) as run_mock,
        ):
            run_mock.side_effect = [
                _mk(returncode=1, stderr="hint: fetch first\n"),
                _mk(returncode=0),
            ]
            _push_sync_branch("fraisier/sync/staging-from-dev")

        push_argvs = [c.args[0] for c in run_mock.call_args_list]
        assert push_argvs[0] == [
            "git",
            "push",
            "origin",
            "fraisier/sync/staging-from-dev",
        ]
        assert push_argvs[1] == [
            "git",
            "push",
            "--force-with-lease",
            "origin",
            "fraisier/sync/staging-from-dev",
        ]

    def test_does_not_retry_on_unrelated_failure(self):
        """Permission denied / auth failure must propagate, never retry."""
        from fraisier.cli.sync import _push_sync_branch

        with (
            patch(
                "fraisier.cli.sync._reclaim_orphan_branch_if_safe",
                return_value=True,
            ),
            patch(_PATCH) as run_mock,
        ):
            run_mock.return_value = _mk(
                returncode=1, stderr="fatal: Authentication failed"
            )
            with pytest.raises(subprocess.CalledProcessError):
                _push_sync_branch("fraisier/sync/staging-from-dev")

        # Single push attempt, no retry.
        assert run_mock.call_count == 1


# ---------------------------------------------------------------------------
# Issue #268: pre-flight clean-worktree guard
# ---------------------------------------------------------------------------


class TestSyncCleanWorktreeGuard:
    """Sync must refuse to run in a dirty worktree, before touching any
    branch: `git checkout -B` would carry uncommitted modifications onto the
    sync branch (and the pre-merge commit would swallow them into the PR),
    and untracked files can abort `git merge` before it starts (#268).
    fraisier never cleans, stashes, or deletes operator files itself.
    """

    def test_dirty_worktree_aborts_with_file_list(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        mg = MockGit().queue(
            "git", "status", "--porcelain", stdout=" M app.py\n?? junk.txt\n"
        )
        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 1
        assert "app.py" in result.output
        assert "junk.txt" in result.output
        # Nothing was touched: no fetch, no branch creation, no merge.
        assert not mg.calls_with_prefix("git", "fetch")
        assert not mg.calls_with_prefix("git", "checkout")
        assert not mg.calls_with_prefix("git", "merge")

    def test_yes_flag_does_not_bypass_guard(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        mg = MockGit().queue("git", "status", "--porcelain", stdout="?? junk.txt\n")
        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 1

    def test_check_flag_unaffected_by_dirty_worktree(self, tmp_path):
        """--check reads refs only; it must not be blocked by a dirty tree."""
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        mg = (
            MockGit()
            .queue("git", "status", "--porcelain", stdout="?? junk.txt\n")
            .queue(
                "git",
                "show",
                "origin/dev:version.json",
                stdout='{"version": "1.1.0"}',
            )
            .queue(
                "git",
                "show",
                "origin/staging:version.json",
                stdout='{"version": "1.0.0"}',
            )
        )
        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--check"])
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Issue #268: `git merge` failures that never started a merge
# ---------------------------------------------------------------------------


class TestSyncMergeNeverStarted:
    """`git merge` exits non-zero both for conflicts AND when the merge
    refuses to start at all (untracked/modified files would be overwritten).
    The second case leaves no MERGE_HEAD and no unmerged paths; treating it
    as "conflicts to resolve" used to fall through to a silent no-op commit
    and trip the pre-push guard with a misleading "fraisier bug" message,
    with git's actual stderr swallowed (#268). It must abort immediately,
    surfacing git's own diagnostics.

    This is defence-in-depth behind the clean-worktree pre-flight guard:
    races and ignored-but-colliding files can still get here.
    """

    GIT_MERGE_STDERR = (
        "error: The following untracked working tree files would be "
        "overwritten by merge:\n"
        "\treport.txt\n"
        "Please move or remove them before you merge.\n"
        "Aborting\n"
    )
    SHA = "deadbeef" * 5
    MERGE_BASE = "cafe1234" * 5

    def _mockgit(self) -> MockGit:
        return (
            MockGit()
            .queue("git", "rev-parse", "--abbrev-ref", stdout="main\n")
            .queue("git", "rev-parse", "origin/dev", stdout=self.SHA + "\n")
            .queue("git", "merge-base", stdout=self.MERGE_BASE + "\n")
            .queue(
                "git",
                "show",
                "origin/dev:version.json",
                stdout='{"version": "1.1.0"}',
            )
            .queue(
                "git",
                "show",
                "origin/staging:version.json",
                stdout='{"version": "1.0.0"}',
            )
            .queue("git", "merge", returncode=2, stderr=self.GIT_MERGE_STDERR)
            # No source deletions to propagate.
            .queue("git", "diff", "--name-only", "--diff-filter=D", stdout="")
            # No unmerged paths (the merge never started) …
            .queue("git", "diff", "--name-only", "--diff-filter=U", stdout="")
            # … and no MERGE_HEAD.
            .queue(
                "git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD", returncode=1
            )
        )

    def test_aborts_and_surfaces_git_stderr(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        mg = self._mockgit()
        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 1
        # git's own diagnostics — previously swallowed — must be visible.
        assert "report.txt" in result.output
        assert "would be" in result.output and "overwritten" in result.output
        # This is an operator-fixable state, not a fraisier bug.
        assert "file an issue" not in result.output
        assert "not a merge commit" not in result.output

    def test_does_not_commit_or_push(self, tmp_path):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        mg = self._mockgit()
        with patch(_PATCH, side_effect=mg):
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 1
        assert not mg.calls_with_prefix("git", "commit")
        assert not mg.calls_with_prefix("git", "push")
        # Operator is returned to their original branch.
        assert mg.was_called(["git", "checkout", "main"])


# ---------------------------------------------------------------------------
# Issue #268: real-git end-to-end coverage of the pre-merge machinery
# ---------------------------------------------------------------------------


def _git(cwd, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *argv],
        capture_output=True,
        text=True,
        check=True,
    )


class _GhStub:
    """subprocess.run replacement: stubs `gh`, passes everything else
    (real git) through. Lets end-to-end tests exercise fraisier's actual
    merge machinery against real repositories while faking only GitHub.
    """

    def __init__(self) -> None:
        self.gh_calls: list[list[str]] = []
        # Patching "fraisier.cli.sync.subprocess.run" mutates the shared
        # subprocess module, so grab the real callable before the patch
        # is active or pass-through would recurse into the mock.
        self._real_run = subprocess.run

    def __call__(self, cmd, *args, **kwargs):
        if cmd and cmd[0] == "gh":
            self.gh_calls.append(list(cmd))
            if cmd[:3] == ["gh", "pr", "view"]:
                return _mk(returncode=1)  # no existing PR
            if cmd[:3] == ["gh", "pr", "create"]:
                return _mk(stdout="https://github.com/org/repo/pull/68\n")
            return _mk()  # pr merge --auto etc.
        return self._real_run(cmd, *args, **kwargs)


def _make_promotion_repos(
    tmp_path,
    *,
    target_diverged: bool = True,
    target_extra_file: bool = False,
):
    """Bare origin + work clone shaped like #268.

    ``dev`` (source) is ahead of ``staging`` (target). With
    ``target_diverged`` the target also carries its own commit editing
    exactly ``.gitignore`` (tier 4 under --prefer-source) and
    ``.secrets.baseline`` (tier 1, fraisier-owned) — so the pre-merge
    conflicts ONLY in auto-resolvable files, the reported state. With
    ``target_extra_file`` the target also adds a non-conflicting file, so
    the resolved tree differs from the source tree.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", str(origin)],
        capture_output=True,
        check=True,
    )
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "--quiet", "-b", "main", str(work)],
        capture_output=True,
        check=True,
    )
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "remote", "add", "origin", str(origin))

    (work / ".gitignore").write_text("*.pyc\n")
    (work / ".secrets.baseline").write_text("base\n")
    (work / "app.py").write_text("print('v1')\n")
    (work / "version.json").write_text('{"version": "1.0.0"}\n')
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")

    _git(work, "branch", "staging")
    if target_diverged:
        _git(work, "checkout", "-q", "staging")
        (work / ".gitignore").write_text("*.pyc\nstaging-only\n")
        (work / ".secrets.baseline").write_text("staging-baseline\n")
        if target_extra_file:
            (work / "NOTES.md").write_text("kept from staging\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "staging own commit")
        _git(work, "checkout", "-q", "main")

    _git(work, "checkout", "-q", "-b", "dev")
    (work / ".gitignore").write_text("*.pyc\ndev-line\n")
    (work / ".secrets.baseline").write_text("dev-baseline\n")
    (work / "app.py").write_text("print('v2')\n")
    (work / "version.json").write_text('{"version": "1.1.0"}\n')
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "dev work")

    _git(work, "push", "-q", "origin", "dev", "staging")
    _git(work, "checkout", "-q", "main")
    return work


class TestSync268EndToEnd:
    """Reporter scenario from #268, against real git repositories."""

    SYNC_BRANCH = "fraisier/sync/staging-from-dev"

    def _invoke(self, tmp_path, work, *flags):
        cfg = _setup(tmp_path, [{"source": "dev", "target": "staging"}])
        gh = _GhStub()
        cwd = Path.cwd()
        os.chdir(work)
        try:
            with patch(_PATCH, side_effect=gh):
                result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes", *flags])
        finally:
            os.chdir(cwd)
        return result, gh

    def test_prefer_source_with_conflicts_only_in_auto_resolved_files(self, tmp_path):
        """The exact #268 state: diverged branches, pre-merge conflicts
        confined to auto-resolved files, resolution == source side. Must
        produce a two-parent merge commit and reach the push/PR stage —
        not the "HEAD is not a merge commit" abort."""
        work = _make_promotion_repos(tmp_path, target_diverged=True)
        result, gh = self._invoke(tmp_path, work, "--prefer-source")
        assert result.exit_code == 0, result.output
        assert "not a merge commit" not in result.output
        assert "Auto-resolved (prefer-source): .gitignore" in result.output

        parents = _git(work, "log", "-1", "--pretty=%P", self.SYNC_BRANCH)
        assert len(parents.stdout.split()) == 2, "sync tip must be a merge commit"
        # The push-safety invariant GitHub needs:
        _git(work, "merge-base", "--is-ancestor", "origin/staging", self.SYNC_BRANCH)
        # Branch was pushed and the PR flow ran.
        _git(work, "fetch", "-q", "origin", self.SYNC_BRANCH)
        assert any(c[:3] == ["gh", "pr", "create"] for c in gh.gh_calls)

    def test_target_extra_file_survives_the_merge(self, tmp_path):
        """Same state plus a non-conflicting target-side file: the merge
        commit must carry it (this is what distinguishes a real merge from
        blindly resetting to the source tree)."""
        work = _make_promotion_repos(
            tmp_path, target_diverged=True, target_extra_file=True
        )
        result, _ = self._invoke(tmp_path, work, "--prefer-source")
        assert result.exit_code == 0, result.output
        parents = _git(work, "log", "-1", "--pretty=%P", self.SYNC_BRANCH)
        assert len(parents.stdout.split()) == 2
        show = _git(work, "show", f"{self.SYNC_BRANCH}:NOTES.md")
        assert "kept from staging" in show.stdout

    def test_target_strictly_behind_source_is_pushable_without_merge_commit(
        self, tmp_path
    ):
        """Second latent #268 trigger: target has no commits of its own, so
        `git merge` answers "Already up to date" and no merge commit exists.
        origin/<tgt> is already an ancestor of HEAD — pushing is safe and
        must proceed instead of aborting with the old two-parents assert."""
        work = _make_promotion_repos(tmp_path, target_diverged=False)
        result, gh = self._invoke(tmp_path, work, "--prefer-source")
        assert result.exit_code == 0, result.output
        assert "not a merge commit" not in result.output
        sync_sha = _git(work, "rev-parse", self.SYNC_BRANCH).stdout.strip()
        dev_sha = _git(work, "rev-parse", "origin/dev").stdout.strip()
        assert sync_sha == dev_sha, "no merge commit needed — head is origin/dev"
        assert any(c[:3] == ["gh", "pr", "create"] for c in gh.gh_calls)

    def test_dirty_worktree_aborts_before_creating_branches(self, tmp_path):
        work = _make_promotion_repos(tmp_path, target_diverged=True)
        (work / "junk.txt").write_text("uncommitted operator file\n")
        result, gh = self._invoke(tmp_path, work, "--prefer-source")
        assert result.exit_code == 1
        assert "junk.txt" in result.output
        assert gh.gh_calls == []
        # Sync branch was never created; operator still on their branch.
        probe = subprocess.run(
            ["git", "-C", str(work), "rev-parse", "--verify", self.SYNC_BRANCH],
            capture_output=True,
            check=False,
        )
        assert probe.returncode != 0
        branch = _git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert branch == "main"
        # And the operator's file is untouched.
        assert (work / "junk.txt").read_text() == "uncommitted operator file\n"
