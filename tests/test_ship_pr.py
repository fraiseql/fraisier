"""Tests for fraisier.ship.pr module."""

from unittest.mock import MagicMock, patch

from fraisier.ship.pr import create_pr


class TestCreatePR:
    """Test create_pr function."""

    @patch("fraisier.ship.pr.subprocess.run")
    def test_create_pr_success(self, mock_run):
        """Test successful PR creation."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="https://github.com/user/repo/pull/123\n"
        )
        console = MagicMock()
        result = create_pr("1.2.3", "main", console)
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
        """Test PR creation failure."""
        mock_run.return_value = MagicMock(
            returncode=1, stderr="Error: PR already exists\n"
        )
        console = MagicMock()
        result = create_pr("1.2.3", "main", console)
        assert result is None
        console.print.assert_called_once_with(
            "[red]PR creation failed:[/red] Error: PR already exists"
        )
        # Verify subprocess.run was called (same as above)
