"""Tests for GitIssueClient across all 4 providers."""

from unittest.mock import patch

import httpx
import pytest

from fraisier.notifications.git_issues import GitIssueClient


@pytest.fixture
def mock_httpx():
    """Mock httpx request methods."""
    with (
        patch.object(httpx, "post") as mock_post,
        patch.object(httpx, "get") as mock_get,
        patch.object(httpx, "patch") as mock_patch,
    ):
        for m in (mock_post, mock_get, mock_patch):
            m.return_value.status_code = 200
            m.return_value.raise_for_status = lambda: None
            m.return_value.json.return_value = {"number": 42, "iid": 42, "id": 42}
        yield {"post": mock_post, "get": mock_get, "patch": mock_patch}


class TestGitHubIssueClient:
    def test_open_issue(self, mock_httpx):
        client = GitIssueClient("github", "tok", "owner/repo")
        result = client.open_issue("title", "body", labels=["bug"])
        mock_httpx["post"].assert_called_once()
        call_kwargs = mock_httpx["post"].call_args
        assert "/repos/owner/repo/issues" in call_kwargs.args[0]
        payload = call_kwargs.kwargs["json"]
        assert payload["title"] == "title"
        assert payload["body"] == "body"
        assert payload["labels"] == ["bug"]
        assert result["number"] == 42

    def test_find_open_issue(self, mock_httpx):
        mock_httpx["get"].return_value.json.return_value = {
            "items": [{"number": 7, "title": "test"}]
        }
        client = GitIssueClient("github", "tok", "owner/repo")
        result = client.find_open_issue("deploy failure")
        assert result["number"] == 7

    def test_find_open_issue_none(self, mock_httpx):
        mock_httpx["get"].return_value.json.return_value = {"items": []}
        client = GitIssueClient("github", "tok", "owner/repo")
        assert client.find_open_issue("nothing") is None

    def test_comment_issue(self, mock_httpx):
        client = GitIssueClient("github", "tok", "owner/repo")
        client.comment_issue(42, "update")
        call_kwargs = mock_httpx["post"].call_args
        assert "/issues/42/comments" in call_kwargs.args[0]

    def test_close_issue(self, mock_httpx):
        client = GitIssueClient("github", "tok", "owner/repo")
        client.close_issue(42)
        call_kwargs = mock_httpx["patch"].call_args
        assert call_kwargs.kwargs["json"]["state"] == "closed"

    def test_auth_header(self, mock_httpx):
        client = GitIssueClient("github", "ghp_secret", "owner/repo")
        client.open_issue("t", "b")
        headers = mock_httpx["post"].call_args.kwargs["headers"]
        assert headers["Authorization"] == "token ghp_secret"


class TestGitLabIssueClient:
    def test_open_issue(self, mock_httpx):
        client = GitIssueClient(
            "gitlab", "glpat-xxx", "mygroup/myrepo", api_base="https://gitlab.com"
        )
        client.open_issue("title", "body")
        call_kwargs = mock_httpx["post"].call_args
        assert "/api/v4/projects/" in call_kwargs.args[0]
        payload = call_kwargs.kwargs["json"]
        assert payload["title"] == "title"
        assert payload["description"] == "body"

    def test_close_issue_uses_state_event(self, mock_httpx):
        client = GitIssueClient("gitlab", "tok", "grp/repo")
        client.close_issue(5)
        payload = mock_httpx["patch"].call_args.kwargs["json"]
        assert payload["state_event"] == "close"

    def test_auth_header(self, mock_httpx):
        client = GitIssueClient("gitlab", "glpat-xxx", "grp/repo")
        client.open_issue("t", "b")
        headers = mock_httpx["post"].call_args.kwargs["headers"]
        assert headers["PRIVATE-TOKEN"] == "glpat-xxx"

    def test_find_open_issue(self, mock_httpx):
        mock_httpx["get"].return_value.json.return_value = [
            {"iid": 3, "title": "match"}
        ]
        client = GitIssueClient("gitlab", "tok", "grp/repo")
        result = client.find_open_issue("deploy")
        assert result["iid"] == 3


class TestGiteaIssueClient:
    def test_open_issue(self, mock_httpx):
        client = GitIssueClient(
            "gitea", "tok", "owner/repo", api_base="https://git.example.com"
        )
        client.open_issue("title", "body")
        call_kwargs = mock_httpx["post"].call_args
        assert "/api/v1/repos/owner/repo/issues" in call_kwargs.args[0]

    def test_close_issue(self, mock_httpx):
        client = GitIssueClient("gitea", "tok", "owner/repo")
        client.close_issue(10)
        payload = mock_httpx["patch"].call_args.kwargs["json"]
        assert payload["state"] == "closed"


class TestBitbucketIssueClient:
    def test_open_issue_nested_body(self, mock_httpx):
        client = GitIssueClient("bitbucket", "tok", "owner/repo")
        client.open_issue("title", "body text")
        payload = mock_httpx["post"].call_args.kwargs["json"]
        assert payload["title"] == "title"
        assert payload["content"]["raw"] == "body text"

    def test_close_issue_resolved(self, mock_httpx):
        client = GitIssueClient("bitbucket", "tok", "owner/repo")
        client.close_issue(99)
        payload = mock_httpx["patch"].call_args.kwargs["json"]
        assert payload["state"] == "resolved"

    def test_find_open_issue(self, mock_httpx):
        mock_httpx["get"].return_value.json.return_value = {
            "values": [{"id": 5, "title": "hit"}]
        }
        client = GitIssueClient("bitbucket", "tok", "owner/repo")
        result = client.find_open_issue("deploy")
        assert result["id"] == 5


class TestCustomApiBase:
    def test_github_enterprise(self, mock_httpx):
        client = GitIssueClient(
            "github", "tok", "org/repo", api_base="https://github.corp.com/api/v3"
        )
        client.open_issue("t", "b")
        url = mock_httpx["post"].call_args.args[0]
        assert url.startswith("https://github.corp.com/api/v3")
