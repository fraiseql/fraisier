# Notifications

Fraisier can send notifications on deployment events (failure, rollback, success) via:

- **Git issues** (GitHub, GitLab, Gitea, Bitbucket) with deduplication and auto-close
- **Slack** incoming webhooks
- **Discord** webhooks
- **Generic webhooks** (any HTTP endpoint)

## Configuration

Add a `notifications:` section to your `fraises.yaml`:

```yaml
notifications:
  on_failure:
    - type: slack
      webhook_url: ${SLACK_DEPLOY_URL}
    - type: github_issue
      token: ${GITHUB_TOKEN}
      repo: myorg/myrepo
      labels: [deploy-failure]
      assignees: [oncall]

  on_rollback:
    - type: github_issue
      token: ${GITHUB_TOKEN}
      repo: myorg/myrepo
      labels: [rolled-back]

  on_success: []
```

Environment variables (`${VAR}`) are expanded at runtime.

## Notifier Types

### `slack`

Sends a formatted text message to a Slack incoming webhook.

| Field | Required | Description |
|-------|----------|-------------|
| `webhook_url` | Yes | Slack incoming webhook URL |

### `discord`

Sends a colored embed to a Discord webhook.

| Field | Required | Description |
|-------|----------|-------------|
| `webhook_url` | Yes | Discord webhook URL |

### `webhook`

POSTs the full `DeployEvent` as JSON to any URL.

| Field | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Target URL |
| `headers` | No | Custom HTTP headers |
| `method` | No | HTTP method (default: `POST`) |

### `github_issue` / `gitlab_issue` / `gitea_issue` / `bitbucket_issue`

Creates/updates/closes issues on the corresponding git platform.

| Field | Required | Description |
|-------|----------|-------------|
| `token` | Yes | API access token |
| `repo` | Yes | Repository (`owner/repo`) |
| `api_base` | No | Custom API URL (for self-hosted) |
| `labels` | No | Issue labels |
| `assignees` | No | Issue assignees |

## Issue Deduplication

When a deployment fails, fraisier searches for an existing open issue matching the fraise, environment, and error code. If found, it adds a comment instead of creating a duplicate. On the next successful deploy, the issue is automatically closed with a resolution comment.

## Fire-and-Forget

Notification failures are logged but never block or affect the deployment result. A failed Slack webhook will not cause a successful deployment to be reported as failed.
