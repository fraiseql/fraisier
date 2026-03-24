"""Git issue management for deployment notifications.

Provides a data-driven GitIssueClient that supports creating, searching,
commenting, and closing issues on GitHub, GitLab, Gitea, and Bitbucket.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


@dataclass(frozen=True)
class IssueConfig:
    """Provider-specific issue API configuration."""

    # API base URL (e.g., "https://api.github.com")
    api_base: str
    # Auth header name and value prefix
    auth_header: str
    auth_prefix: str
    # Endpoint patterns (use {owner}, {repo}, {project_id}, {issue_id})
    create_endpoint: str
    search_endpoint: str
    comment_endpoint: str
    close_endpoint: str
    # Field names in request/response bodies
    title_field: str = "title"
    body_field: str = "body"
    state_field: str = "state"
    closed_value: str = "closed"
    labels_field: str = "labels"
    assignees_field: str = "assignees"
    # Search result path
    search_items_path: str = "items"
    issue_id_field: str = "number"


GITHUB_ISSUE_CONFIG = IssueConfig(
    api_base="https://api.github.com",
    auth_header="Authorization",
    auth_prefix="token ",
    create_endpoint="/repos/{owner}/{repo}/issues",
    search_endpoint="/search/issues?q={query}+repo:{owner}/{repo}+is:issue+is:open",
    comment_endpoint="/repos/{owner}/{repo}/issues/{issue_id}/comments",
    close_endpoint="/repos/{owner}/{repo}/issues/{issue_id}",
    search_items_path="items",
    issue_id_field="number",
)

GITLAB_ISSUE_CONFIG = IssueConfig(
    api_base="https://gitlab.com",
    auth_header="PRIVATE-TOKEN",
    auth_prefix="",
    create_endpoint="/api/v4/projects/{project_id}/issues",
    search_endpoint="/api/v4/projects/{project_id}/issues?search={query}&state=opened",
    comment_endpoint="/api/v4/projects/{project_id}/issues/{issue_id}/notes",
    close_endpoint="/api/v4/projects/{project_id}/issues/{issue_id}",
    body_field="description",
    state_field="state_event",
    closed_value="close",
    search_items_path="",
    issue_id_field="iid",
)

GITEA_ISSUE_CONFIG = IssueConfig(
    api_base="https://gitea.example.com",
    auth_header="Authorization",
    auth_prefix="token ",
    create_endpoint="/api/v1/repos/{owner}/{repo}/issues",
    search_endpoint="/api/v1/repos/{owner}/{repo}/issues?type=issues&state=open&q={query}",
    comment_endpoint="/api/v1/repos/{owner}/{repo}/issues/{issue_id}/comments",
    close_endpoint="/api/v1/repos/{owner}/{repo}/issues/{issue_id}",
    state_field="state",
    closed_value="closed",
    search_items_path="",
    issue_id_field="number",
)

BITBUCKET_ISSUE_CONFIG = IssueConfig(
    api_base="https://api.bitbucket.org",
    auth_header="Authorization",
    auth_prefix="Bearer ",
    create_endpoint="/2.0/repositories/{owner}/{repo}/issues",
    search_endpoint=(
        '/2.0/repositories/{owner}/{repo}/issues?q=title~"{query}" AND state="open"'
    ),
    comment_endpoint="/2.0/repositories/{owner}/{repo}/issues/{issue_id}/comments",
    close_endpoint="/2.0/repositories/{owner}/{repo}/issues/{issue_id}",
    body_field="content.raw",
    state_field="state",
    closed_value="resolved",
    search_items_path="values",
    issue_id_field="id",
)

PROVIDER_CONFIGS: dict[str, IssueConfig] = {
    "github": GITHUB_ISSUE_CONFIG,
    "gitlab": GITLAB_ISSUE_CONFIG,
    "gitea": GITEA_ISSUE_CONFIG,
    "bitbucket": BITBUCKET_ISSUE_CONFIG,
}


class GitIssueClient:
    """HTTP client for git issue operations across providers."""

    def __init__(
        self,
        provider: str,
        token: str,
        repo: str,
        api_base: str | None = None,
    ):
        self.provider = provider
        self.token = token
        self.repo = repo
        self.cfg = PROVIDER_CONFIGS[provider]
        if api_base:
            self.cfg = IssueConfig(
                api_base=api_base,
                auth_header=self.cfg.auth_header,
                auth_prefix=self.cfg.auth_prefix,
                create_endpoint=self.cfg.create_endpoint,
                search_endpoint=self.cfg.search_endpoint,
                comment_endpoint=self.cfg.comment_endpoint,
                close_endpoint=self.cfg.close_endpoint,
                title_field=self.cfg.title_field,
                body_field=self.cfg.body_field,
                state_field=self.cfg.state_field,
                closed_value=self.cfg.closed_value,
                labels_field=self.cfg.labels_field,
                assignees_field=self.cfg.assignees_field,
                search_items_path=self.cfg.search_items_path,
                issue_id_field=self.cfg.issue_id_field,
            )

    def _headers(self) -> dict[str, str]:
        return {
            self.cfg.auth_header: f"{self.cfg.auth_prefix}{self.token}",
            "Accept": "application/json",
        }

    def _repo_params(self) -> dict[str, str]:
        """Extract owner/repo or project_id from self.repo."""
        parts = self.repo.split("/", 1)
        if len(parts) == 2:
            return {"owner": parts[0], "repo": parts[1], "project_id": self.repo}
        return {"owner": self.repo, "repo": self.repo, "project_id": self.repo}

    def _url(self, endpoint_template: str, **extra: str) -> str:
        params = {**self._repo_params(), **extra}
        return self.cfg.api_base + endpoint_template.format(**params)

    def open_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new issue. Returns the issue data dict."""
        url = self._url(self.cfg.create_endpoint)
        payload: dict[str, Any] = {
            self.cfg.title_field: title,
        }
        # Handle nested body field (e.g., "content.raw" for Bitbucket)
        _set_nested(payload, self.cfg.body_field, body)

        if labels:
            payload[self.cfg.labels_field] = labels
        if assignees:
            payload[self.cfg.assignees_field] = assignees

        resp = httpx.post(url, json=payload, headers=self._headers(), timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def find_open_issue(self, query: str) -> dict[str, Any] | None:
        """Search for an open issue matching query. Returns first match or None."""
        url = self._url(self.cfg.search_endpoint, query=query)
        resp = httpx.get(url, headers=self._headers(), timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if self.cfg.search_items_path:
            items = data.get(self.cfg.search_items_path, [])
        else:
            items = data if isinstance(data, list) else []

        return items[0] if items else None

    def comment_issue(self, issue_id: int | str, body: str) -> dict[str, Any]:
        """Add a comment to an existing issue."""
        url = self._url(self.cfg.comment_endpoint, issue_id=str(issue_id))
        payload: dict[str, Any] = {}
        _set_nested(payload, self.cfg.body_field, body)
        resp = httpx.post(url, json=payload, headers=self._headers(), timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def close_issue(self, issue_id: int | str) -> dict[str, Any]:
        """Close an issue."""
        url = self._url(self.cfg.close_endpoint, issue_id=str(issue_id))
        payload = {self.cfg.state_field: self.cfg.closed_value}
        resp = httpx.patch(url, json=payload, headers=self._headers(), timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()


def _set_nested(d: dict[str, Any], path: str, value: Any) -> None:
    """Set a nested dict key from a dotted path (e.g., 'content.raw')."""
    keys = path.split(".")
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value
