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
            _mk(returncode=1),  # git diff --cached (staged)
            _mk(),  # git commit pre-merge
            _mk(),  # git push
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

    def test_no_commit_when_nothing_staged_after_clean_merge(self, tmp_path):
        """When merge is clean and nothing is staged, no commit is made."""
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
                _mk(returncode=0),  # git diff --cached --quiet (nothing staged)
                _mk(),  # git push (no commit)
                _mk(stdout=self.PR_URL + "\n"),
                _mk(),
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0
        commands = [c[0][0] for c in m.call_args_list]
        commit_calls = [c for c in commands if "commit" in c]
        assert commit_calls == []

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
                _mk(stdout="version.json\npyproject.toml\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:version.json (exists)
                _mk(),  # checkout origin/dev -- version.json
                _mk(),  # git add version.json
                _mk(returncode=0),  # cat-file origin/dev:pyproject.toml (exists)
                _mk(),  # checkout origin/dev -- pyproject.toml
                _mk(),  # git add pyproject.toml
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _mk(),  # git commit
                _mk(),  # git push
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0
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
                _mk(stdout="version.json\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:version.json (exists)
                _mk(),  # checkout origin/dev -- version.json
                _mk(),  # git add version.json
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _mk(returncode=1),  # git diff --cached --quiet → something staged
                _mk(),  # git commit
                _mk(),  # git push
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        commit_calls = [c[0][0] for c in m.call_args_list if "commit" in c[0][0]]
        assert commit_calls, "expected at least one commit call"
        assert all("--no-verify" in call for call in commit_calls)

    def test_pre_merge_skips_commit_when_resolution_yields_no_diff(self, tmp_path):
        """Conflict-resolution path skips pre-merge commit when nothing is staged.

        Regression test for #164. When every conflicted file auto-resolves
        back to source HEAD (the sync branch's tip), the index after
        `git add` is byte-identical to HEAD. Git refused the empty commit
        and sync aborted. The fix mirrors the clean-merge path's existing
        `git diff --cached --quiet` guard.
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
                _mk(stdout="pyproject.toml\n"),  # diff --filter=U
                _mk(returncode=0),  # cat-file origin/dev:pyproject.toml (exists)
                _mk(),  # checkout origin/dev -- pyproject.toml
                _mk(),  # git add pyproject.toml
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _mk(returncode=0),  # git diff --cached --quiet → nothing staged
                _mk(),  # git push
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])

        assert result.exit_code == 0, result.output
        commands = [c[0][0] for c in m.call_args_list]
        commit_calls = [c for c in commands if "commit" in c]
        assert commit_calls == [], (
            "no commit should be issued when conflict resolution staged no diff"
        )
        staged_checks = [
            c for c in commands if c == ["git", "diff", "--cached", "--quiet"]
        ]
        assert len(staged_checks) == 1, (
            "conflict-resolution path must guard the pre-merge commit"
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
                _mk(stdout="old_script.py\n"),  # diff --filter=U (first)
                _mk(
                    returncode=1
                ),  # cat-file origin/dev:old_script.py (not in source → deleted)
                _mk(),  # git rm old_script.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _mk(),  # git commit
                _mk(),  # git push
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0
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
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:src/routes.py (exists)
                _mk(
                    returncode=0
                ),  # git diff merge_base origin/staging -- src/routes.py (unchanged)
                _mk(),  # checkout origin/dev -- src/routes.py
                _mk(),  # git add src/routes.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _mk(),  # git commit
                _mk(),  # git push
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0
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
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:src/routes.py (exists)
                _mk(
                    returncode=1
                ),  # git diff merge_base origin/staging -- src/routes.py (changed)
                _mk(),  # checkout origin/dev -- src/routes.py (prefer-source)
                _mk(),  # git add src/routes.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _mk(),  # git commit
                _mk(),  # git push
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 0
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
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:src/routes.py (exists)
                _mk(
                    returncode=1
                ),  # git diff merge_base origin/staging -- src/routes.py (changed)
                _mk(),  # checkout origin/dev -- src/routes.py (prefer-source from config)
                _mk(),  # git add src/routes.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _mk(),  # git commit
                _mk(),  # git push
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0
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
                _mk(stdout="src/routes.py\n"),  # diff --filter=U (first)
                _mk(returncode=0),  # cat-file origin/dev:src/routes.py (exists)
                _mk(
                    returncode=1
                ),  # git diff merge_base origin/staging -- src/routes.py (changed)
                _mk(),  # checkout origin/dev -- src/routes.py (flag overrides config)
                _mk(),  # git add src/routes.py
                _mk(stdout=""),  # diff --filter=U (remaining: clean)
                _mk(),  # git commit
                _mk(),  # git push
                _mk(stdout=self.PR_URL + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(
                main, ["-c", cfg, "sync", "--yes", "--prefer-source"]
            )
        assert result.exit_code == 0
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
                _mk(returncode=1),  # git diff --cached (staged)
                _mk(),  # git commit
                _mk(),  # git push
                _mk(stdout=pr_url + "\n"),  # gh pr create
                _mk(),  # gh pr merge
                _mk(),  # git checkout main
            ]
            result = CliRunner().invoke(main, ["-c", cfg, "sync", "--yes"])
        assert result.exit_code == 0
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
            _mk(returncode=1),  # git diff --cached (staged)
            _mk(),  # git commit pre-merge
            _mk(),  # git push
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
            if len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "checkout" and "-B" in cmd
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
