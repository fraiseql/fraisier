# Fraisier Webhook Reference

**Version**: 0.1.0

Fraisier receives webhooks from Git providers (GitHub, GitLab, Gitea, Bitbucket) to trigger automated deployments when code is pushed.

---

## Quick Start

1. Start the webhook server:

```bash
fraisier-webhook
# Listens on 0.0.0.0:8080 by default
```

2. Configure your Git provider to send webhooks to `http://your-server:8080/webhook`.

3. Set a shared secret:

```bash
export FRAISIER_WEBHOOK_SECRET="your-secret-here"
```

4. Push to a branch mapped in `fraises.yaml` — Fraisier deploys automatically.

---

## Supported Providers

| Provider | Auto-detected Header | Signature Method |
|----------|---------------------|------------------|
| GitHub | `X-GitHub-Event` | HMAC-SHA256 via `X-Hub-Signature-256` |
| GitLab | `X-Gitlab-Event` | Shared token via `X-Gitlab-Token` |
| Gitea | `X-Gitea-Event` | HMAC-SHA256 via `X-Gitea-Signature` |
| Bitbucket | `X-Event-Key` | IP allowlist or secret |

The provider is auto-detected from request headers. You can also force it with a query parameter: `/webhook?provider=gitlab`.

---

## Webhook Endpoints

### POST /webhook

Main endpoint. Receives webhooks from any supported provider.

**Flow:**

1. Auto-detect provider from headers (or `?provider=` query param)
2. Verify webhook signature using `FRAISIER_WEBHOOK_SECRET`
3. Parse the event (push, ping, etc.)
4. Record event in the Fraisier database
5. If it's a push to a mapped branch, trigger deployment in background

**Responses:**

```json
// Deployment triggered
{"status": "deployment_triggered", "fraise": "my_api", "environment": "production", "branch": "main"}

// No matching branch
{"status": "ignored", "reason": "No fraise configured for branch 'feature/xyz'"}

// Ping acknowledged
{"status": "pong", "message": "Webhook configured successfully"}
```

### POST /webhook/github

Legacy endpoint for GitHub. Internally redirects to `/webhook?provider=github`.

### GET /health

Returns `{"status": "healthy", "service": "fraisier-webhook"}`.

### GET /fraises

Lists configured fraises and branch mappings.

### GET /providers

Lists supported Git providers and the currently configured one.

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FRAISIER_HOST` | `0.0.0.0` | Listen address |
| `FRAISIER_PORT` | `8080` | Listen port |
| `FRAISIER_WEBHOOK_SECRET` | — | Shared secret for signature verification |
| `FRAISIER_GIT_PROVIDER` | `github` | Default provider (used when auto-detection fails) |
| `FRAISIER_GIT_URL` | — | Base URL for self-hosted Git instances |

### fraises.yaml Git Section

```yaml
git:
  provider: github  # default provider
  github:
    webhook_secret: "override-per-provider"
  gitlab:
    webhook_secret: "gitlab-specific-secret"
```

### Branch Mapping

Deployments are triggered based on branch-to-environment mappings in `fraises.yaml`:

```yaml
fraises:
  my_api:
    type: api
    environments:
      development:
        branch: develop
      staging:
        branch: staging
      production:
        branch: main
```

When a push to `main` is received, Fraisier deploys `my_api` to `production`.

---

## Setting Up Git Provider Webhooks

### GitHub

1. Go to your repository **Settings > Webhooks > Add webhook**
2. **Payload URL**: `https://your-server:8080/webhook`
3. **Content type**: `application/json`
4. **Secret**: same value as `FRAISIER_WEBHOOK_SECRET`
5. **Events**: Select "Just the push event"

### GitLab

1. Go to your project **Settings > Webhooks**
2. **URL**: `https://your-server:8080/webhook`
3. **Secret token**: same value as `FRAISIER_WEBHOOK_SECRET`
4. **Trigger**: Check "Push events"

### Gitea

1. Go to your repository **Settings > Webhooks > Add Webhook > Gitea**
2. **Target URL**: `https://your-server:8080/webhook`
3. **Secret**: same value as `FRAISIER_WEBHOOK_SECRET`
4. **Events**: Select "Push"

### Bitbucket

1. Go to your repository **Settings > Webhooks > Add webhook**
2. **URL**: `https://your-server:8080/webhook?provider=bitbucket`
3. **Triggers**: Select "Repository push"

---

## Viewing Webhook History

Use the CLI to view recent webhook events recorded in the Fraisier database:

```bash
# Show last 10 webhook events (default)
fraisier webhooks

# Show more events
fraisier webhooks --limit 50
```

---

## Troubleshooting

### Webhook Not Triggering Deployments

1. Check that `FRAISIER_WEBHOOK_SECRET` matches the secret in your Git provider
2. Verify the branch is mapped in `fraises.yaml`
3. Check webhook events: `fraisier webhooks`
4. Check server logs for signature verification failures

### Signature Verification Failing

- Ensure the secret is identical on both sides (no trailing whitespace)
- GitHub: the secret goes in the webhook config, not as a header
- GitLab: uses `X-Gitlab-Token` header (plain comparison, not HMAC)

### Provider Not Detected

Add `?provider=github` (or `gitlab`, `gitea`, `bitbucket`) to the webhook URL to skip auto-detection.

---

## See Also

- [api-reference.md](api-reference.md) — Webhook server API endpoints
- [cli-reference.md](cli-reference.md) — CLI commands
