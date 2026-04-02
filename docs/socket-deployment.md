# Socket-Activated Deployments

Fraisier supports socket-activated deployments as an alternative to sudoers-based deployment. This approach eliminates permission issues and provides a cleaner, more secure mechanism for webhook-triggered deployments.

## Overview

Socket activation allows web applications to trigger deployments by writing JSON messages to Unix sockets. The systemd service automatically starts when a socket connection is made, runs the deployment as the correct user, and reports results back.

## Architecture

### Components

1. **Socket Unit**: Listens on `/run/fraisier/{project}-{environment}/deploy.sock`
2. **Service Unit**: Runs the deployment daemon when socket is activated
3. **Trigger Command**: `fraisier trigger-deploy` writes deployment requests to socket
4. **Status Command**: `fraisier deployment-status` reads deployment results

### Socket Path Convention

```
# Per-project socket paths prevent conflicts on multi-project servers
/run/fraisier/{project_name}-{environment}/deploy.sock

# Examples
/run/fraisier/myapp-production/deploy.sock
/run/fraisier/api-staging/deploy.sock
```

### Security Model

- **Socket permissions**: Owned by web user (www-data), readable/writable by web group
- **Service isolation**: Runs as deploy user (fraisier_deploy), not web user
- **Input validation**: Strict JSON schema validation prevents injection attacks

## Setup

### 1. Generate Units

```bash
# Generate systemd socket/service units
fraisier scaffold

# Review generated files
ls scripts/generated/systemd/
# fraisier-myapp-production-deploy.socket
# fraisier-myapp-production-deploy.service
```

### 2. Install Units

```bash
# Install to system locations (requires sudo)
fraisier scaffold-install --yes

# Enable and start socket
sudo systemctl enable fraisier-myapp-production-deploy.socket
sudo systemctl start fraisier-myapp-production-deploy.socket

# Verify socket is listening
sudo systemctl status fraisier-myapp-production-deploy.socket
```

### 3. Test Deployment

```bash
# Trigger deployment
fraisier trigger-deploy myapp production

# Check status
fraisier deployment-status myapp
```

## Webhook Integration

### GitHub Webhook

Configure your GitHub webhook to POST to your web application:

```json
// GitHub webhook payload
{
  "repository": {"name": "myapp"},
  "ref": "refs/heads/main",
  "head_commit": {"id": "abc123"}
}
```

In your web application:

```python
# Extract branch from webhook
branch = payload["ref"].replace("refs/heads/", "")

# Trigger deployment
import subprocess
result = subprocess.run([
    "fraisier", "trigger-deploy",
    "myapp", "production",
    "--branch", branch
], capture_output=True)

if result.returncode == 0:
    return {"status": "deployment_triggered"}
else:
    return {"error": "deployment_failed"}, 500
```

### JSON Protocol

The trigger command sends this JSON to the socket:

```json
{
  "version": 1,
  "project": "myapp",
  "environment": "production",
  "branch": "main",
  "timestamp": "2026-04-02T11:15:23Z",
  "triggered_by": "webhook",
  "options": {
    "force": false,
    "no_cache": false
  },
  "metadata": {
    "github_event": "push",
    "github_sender": "user",
    "webhook_id": "12345"
  }
}
```

## Configuration

Add webhook configuration to `fraises.yaml`:

```yaml
# Global webhook settings
webhook:
  socket_user: www-data
  socket_group: www-data
  concurrency_mode: reject  # or 'queue'
  max_queue_depth: 10
  deployment_timeout: 3600

# Per-environment overrides
fraises:
  myapp:
    environments:
      production:
        webhook:
          socket_user: nginx  # Override global setting
```

## Monitoring

### Status Files

Deployment results are written to persistent status files:

```bash
# Check latest deployment
cat /run/fraisier/myapp-production.last_deployment
```

Status file format:

```json
{
  "version": 1,
  "project": "myapp",
  "environment": "production",
  "status": "success",
  "deployed_version": "abc123",
  "started_at": "2026-04-02T11:15:24Z",
  "completed_at": "2026-04-02T11:17:58Z",
  "duration_seconds": 154,
  "health_check_status": "healthy"
}
```

### Systemd Journal

All deployment logs are available in systemd journal:

```bash
# View deployment logs
journalctl -u fraisier-myapp-production-deploy.service -f

# Search by project
journalctl FRAISIER_PROJECT=myapp
```

### Prometheus Metrics (Optional)

Deployments can export metrics for monitoring:

```bash
fraisier metrics  # Starts metrics server on localhost:8001
```

**Available Metrics:**

```prometheus
# Deployment counters
fraisier_deployments_total{project="myapp", environment="production", status="success"} 42
fraisier_deployments_total{project="myapp", environment="production", status="failed"} 3

# Deployment duration histograms
fraisier_deployment_duration_seconds{project="myapp", environment="production", quantile="0.5"} 120
fraisier_deployment_duration_seconds{project="myapp", environment="production", quantile="0.95"} 300

# Current deployment status
fraisier_deployment_status{project="myapp", environment="production"} 1  # 1=success, 0=failed

# Queue metrics (if queue mode enabled)
fraisier_deployment_queue_length{project="myapp", environment="production"} 2
fraisier_deployment_queue_max_depth{project="myapp", environment="production"} 10
```

**Grafana Dashboard Example:**

Create a dashboard with panels for:
- Deployment success rate over time
- Average deployment duration
- Current deployment status by environment
- Queue length (if using queue mode)

### Alerting

Set up alerts for deployment failures:

**Prometheus Alerting Rules:**

```yaml
groups:
  - name: fraisier
    rules:
      - alert: FraisierDeploymentFailed
        expr: increase(fraisier_deployments_total{status="failed"}[5m]) > 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Deployment failed for {{ $labels.project }}/{{ $labels.environment }}"
          description: "Check logs: journalctl -u fraisier-{{ $labels.project }}-{{ $labels.environment }}-deploy.service"

      - alert: FraisierDeploymentStuck
        expr: fraisier_deployment_status == 0
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Deployment stuck in failed state for {{ $labels.project }}/{{ $labels.environment }}"
          description: "Last deployment failed, manual intervention may be required"
```

**Webhook Callbacks:**

Configure callback URLs for external alerting:

```yaml
webhook:
  status_callback_url: https://alerts.example.com/webhook
```

The daemon will POST deployment results to this URL after each deployment.

## Setup

Socket activation is the primary deployment method in Fraisier 0.4.0+.

### Basic Setup

1. **Generate units**: `fraisier scaffold`
2. **Install units**: `fraisier scaffold-install --yes`
3. **Enable sockets**: `sudo systemctl enable fraisier-*-deploy.socket`
4. **Start sockets**: `sudo systemctl start fraisier-*-deploy.socket`

### Webhook Integration

Configure your webhook to trigger deployments:

```bash
# GitHub webhook example
fraisier trigger-deploy myapp production --branch $GITHUB_HEAD_REF
```

### Deployment Workflow

```bash
# Trigger deployment
fraisier trigger-deploy myapp production

# Check status
fraisier deployment-status myapp

# View logs
journalctl -u fraisier-myapp-production-deploy.service
```

## Troubleshooting

### Socket Connection Failed

```bash
# Check if socket exists
ls -la /run/fraisier/myapp-production/deploy.sock

# Check socket service status
systemctl status fraisier-myapp-production-deploy.socket

# Restart socket service
sudo systemctl restart fraisier-myapp-production-deploy.socket
```

### Permission Denied

```bash
# Check socket ownership
ls -la /run/fraisier/myapp-production/deploy.sock
# Should be: srw-rw---- www-data www-data

# Check if web user is in correct group
groups www-data

# Add web user to deploy group if needed
sudo usermod -a -G deploy www-data
```

### Deployment Timeout

```bash
# Check deployment timeout setting
grep deployment_timeout fraises.yaml

# Increase timeout for slow deployments
webhook:
  deployment_timeout: 7200  # 2 hours
```

### Service Not Starting

```bash
# Check systemd service status
systemctl status fraisier-myapp-production-deploy.service

# View service logs
journalctl -u fraisier-myapp-production-deploy.service -n 50
```

## Benefits

- ✅ **No sudo configuration** required
- ✅ **Automatic user isolation** (always runs as correct user)
- ✅ **Concurrent deployment control** (reject or queue)
- ✅ **Better logging** via systemd journal
- ✅ **Webhook-friendly** JSON protocol
- ✅ **Security hardened** service isolation