"""Tests for fraisier.ship.pr module."""

from unittest.mock import MagicMock, call, patch

from fraisier.ship.pr import (
    GitHubPRBackend,
    UnimplementedPRBackend,
    create_pr,
    enable_auto_merge,
    get_pr_backend,
)


class TestGetPRBackend:
    """Test get_pr_backend factory."""

    def test_github_returns_github_backend(self):
        assert isinstance(get_pr_backend("github"), GitHubPRBackend)

    def test_unknown_provider_returns_unsupported(self):
        backend = get_pr_backend("gitlab")
        assert isinstance(backend, UnimplementedPRBackend)

    def test_unsupported_stores_provider_name(self):
        backend = get_pr_backend("gitea")
        assert isinstance(backend, UnimplementedPRBackend)
        assert backend._provider == "gitea"


class TestGitHubPRBackend:
    """Test GitHubPRBackend via the gh CLI."""

    @patch("fraisier.ship.pr.subprocess.run")
    def test_create_pr_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/user/repo/pull/123\n"
        )
        console = MagicMock()
        backend = GitHubPRBackend()
        result = backend.create_pr("1.2.3", "main", console)
        assert result == "https://github.com/user/repo/pull/123"
        console.print.assert_called_once_with(
            "[green]PR created:[/green] https://github.com/user/repo/pull/123"
        )
        mock_run.assert_called_once_with(
            [
                "gh",
                "pr",
                "create",
                "--base",
                "main",
                "--title",
                "release: v1.2.3",
                "--body",
                "Automated release of v1.2.3 via `fraisier ship`.",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    @patch("fraisier.ship.pr.subprocess.run")
    def test_create_pr_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stderr="Error: PR already exists\n"
        )
        console = MagicMock()
        result = GitHubPRBackend().create_pr("1.2.3", "main", console)
        assert result is None
        console.print.assert_called_once_with(
            "[red]PR creation failed:[/red] Error: PR already exists"
        )

    @patch("fraisier.ship.pr.subprocess.run")
    def test_find_current_pr_url_found(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/user/repo/pull/99\n"
        )
        console = MagicMock()
        url = GitHubPRBackend().find_current_pr_url(console)
        assert url == "https://github.com/user/repo/pull/99"
        console.print.assert_not_called()

    @patch("fraisier.ship.pr.subprocess.run")
    def test_find_current_pr_url_not_found_emits_hint(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stderr="no pull requests found\n"
        )
        console = MagicMock()
        url = GitHubPRBackend().find_current_pr_url(console)
        assert url is None
        output = console.print.call_args[0][0]
        assert "[yellow]Auto-merge skipped:[/yellow]" in output
        assert "--pr" in output

    @patch("fraisier.ship.pr.subprocess.run")
    def test_enable_auto_merge_squash(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        console = MagicMock()
        GitHubPRBackend().enable_auto_merge(
            "https://github.com/user/repo/pull/1", "squash", console
        )
        mock_run.assert_called_once_with(
            [
                "gh",
                "pr",
                "merge",
                "--auto",
                "--squash",
                "https://github.com/user/repo/pull/1",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        console.print.assert_called_once_with(
            "[green]Auto-merge enabled (squash):[/green] https://github.com/user/repo/pull/1"
        )

    @patch("fraisier.ship.pr.subprocess.run")
    def test_enable_auto_merge_rebase_method(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        console = MagicMock()
        GitHubPRBackend().enable_auto_merge(
            "https://github.com/user/repo/pull/2", "rebase", console
        )
        cmd = mock_run.call_args[0][0]
        assert "--rebase" in cmd

    @patch("fraisier.ship.pr.subprocess.run")
    def test_enable_auto_merge_failure_warns_non_fatally(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="GraphQL: Auto-merge is not enabled for this repository\n",
        )
        console = MagicMock()
        GitHubPRBackend().enable_auto_merge(
            "https://github.com/user/repo/pull/1", "squash", console
        )
        output = console.print.call_args[0][0]
        assert "[yellow]Auto-merge not enabled:[/yellow]" in output
        assert "Auto-merge is not enabled" in output


class TestUnimplementedPRBackend:
    """Test UnimplementedPRBackend warns gracefully for all operations."""

    def test_create_pr_warns_and_returns_none(self):
        console = MagicMock()
        result = UnimplementedPRBackend("gitlab").create_pr("1.0.0", "main", console)
        assert result is None
        output = console.print.call_args[0][0]
        assert "gitlab" in output

    def test_find_current_pr_url_returns_none_silently(self):
        console = MagicMock()
        result = UnimplementedPRBackend("gitea").find_current_pr_url(console)
        assert result is None
        console.print.assert_not_called()

    def test_enable_auto_merge_warns(self):
        console = MagicMock()
        UnimplementedPRBackend("bitbucket").enable_auto_merge(
            "https://bitbucket.org/x/y/pull-requests/1", "squash", console
        )
        output = console.print.call_args[0][0]
        assert "bitbucket" in output


class TestPublicAPI:
    """Test module-level create_pr / enable_auto_merge with injected backend."""

    def test_create_pr_delegates_to_backend(self):
        backend = MagicMock()
        backend.create_pr.return_value = "https://github.com/x/y/pull/1"
        console = MagicMock()
        result = create_pr("1.0.0", "main", console, backend=backend)
        assert result == "https://github.com/x/y/pull/1"
        backend.create_pr.assert_called_once_with("1.0.0", "main", console)

    def test_enable_auto_merge_with_known_url_skips_detection(self):
        backend = MagicMock()
        console = MagicMock()
        enable_auto_merge(
            "squash",
            console,
            pr_url="https://github.com/x/y/pull/1",
            backend=backend,
        )
        backend.find_current_pr_url.assert_not_called()
        backend.enable_auto_merge.assert_called_once_with(
            "https://github.com/x/y/pull/1", "squash", console
        )

    def test_enable_auto_merge_without_url_detects_from_branch(self):
        backend = MagicMock()
        backend.find_current_pr_url.return_value = "https://github.com/x/y/pull/5"
        console = MagicMock()
        enable_auto_merge("squash", console, backend=backend)
        backend.find_current_pr_url.assert_called_once_with(console)
        backend.enable_auto_merge.assert_called_once_with(
            "https://github.com/x/y/pull/5", "squash", console
        )

    def test_enable_auto_merge_without_url_no_pr_found_is_noop(self):
        backend = MagicMock()
        backend.find_current_pr_url.return_value = None
        console = MagicMock()
        enable_auto_merge("squash", console, backend=backend)
        backend.enable_auto_merge.assert_not_called()
