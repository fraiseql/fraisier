# Fraisier Webhook Server API Reference

**Version**: 0.1.0
**Base URL**: `http://localhost:8080`

The Fraisier webhook server receives Git provider webhooks and triggers deployments automatically. It also exposes health and configuration endpoints.

---

## Endpoints

### POST /webhook

Receive a webhook from any supported Git provider. The provider is auto-detected from request headers, or can be specified via query parameter.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `provider` | string | Force a specific provider (`github`, `gitlab`, `gitea`, `bitbucket`). If omitted, auto-detected from headers. |

**Auto-detection** uses these headers:

| Header | Provider |
|--------|----------|
| `X-GitHub-Event` | GitHub |
| `X-Gitlab-Event` | GitLab |
| `X-Gitea-Event` | Gitea |
| `X-Event-Key` | Bitbucket |

**Signature verification** uses each provider's native mechanism:

- **GitHub**: `X-Hub-Signature-256` (HMAC-SHA256)
- **GitLab**: `X-Gitlab-Token` (shared secret)
- **Gitea**: `X-Gitea-Signature` (HMAC-SHA256)
- **Bitbucket**: IP allowlist or webhook secret

Set the webhook secret via `FRAISIER_WEBHOOK_SECRET` environment variable.

**Responses:**

| Status | Meaning |
|--------|---------|
| 200 | Event processed (see `status` field in response body) |
| 400 | Invalid provider or malformed JSON |
| 401 | Signature verification failed |

**Response body** (200):

```json
{
  "status": "deployment_triggered",
  "fraise": "my_api",
  "environment": "production",
  "branch": "main",
  "provider": "github",
  "webhook_id": 42
}
```

Possible `status` values:

- `deployment_triggered` — deployment started in background
- `ignored` — event not actionable (no matching branch, non-push event)
- `pong` — ping event acknowledged

---

### POST /webhook/github

Legacy GitHub-specific endpoint. Redirects internally to `/webhook?provider=github`. Prefer using `/webhook` directly.

---

### GET /health

Health check endpoint for load balancers and monitoring.

**Response** (200):

```json
{
  "status": "healthy",
  "service": "fraisier-webhook"
}
```

---

### GET /fraises

List all fraises configured in `fraises.yaml` with their branch mappings.

**Response** (200):

```json
{
  "fraises": [
    {
      "name": "my_api",
      "type": "api",
      "environments": ["development", "staging", "production"]
    }
  ],
  "branch_mapping": {
    "develop": {"fraise": "my_api", "environment": "development"},
    "main": {"fraise": "my_api", "environment": "production"}
  }
}
```

---

### GET /providers

List supported Git providers and the currently configured one.

**Response** (200):

```json
{
  "providers": ["github", "gitlab", "gitea", "bitbucket"],
  "configured": "github"
}
```

---

## Configuration

The webhook server is configured via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `FRAISIER_HOST` | `0.0.0.0` | Listen address |
| `FRAISIER_PORT` | `8080` | Listen port |
| `FRAISIER_WEBHOOK_SECRET` | — | Shared secret for webhook signature verification |
| `FRAISIER_GIT_PROVIDER` | `github` | Default Git provider |
| `FRAISIER_GIT_URL` | — | Git provider base URL (for self-hosted instances) |
| `FRAISIER_LOG_LEVEL` | `INFO` | Log level |

Git provider settings can also be set in `fraises.yaml` under the `git:` key.

---

## Running

```bash
# Via entry point
fraisier-webhook

# Via Docker
docker run -p 8080:8080 \
  -e FRAISIER_WEBHOOK_SECRET=mysecret \
  -v ./fraises.yaml:/app/fraises.yaml \
  fraisier
```
