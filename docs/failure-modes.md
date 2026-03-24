# Failure Modes

What happens when things go wrong during a fraisier deployment.

## Decision Tree

```
Deployment started
  |
  +-- Git pull failed?
  |     -> Status: FAILED
  |     -> No rollback needed (code unchanged)
  |     -> Action: Check git connectivity, SSH keys, disk space
  |
  +-- Migration failed?
  |     -> Git rolled back to previous SHA
  |     -> Service restarted with old code
  |     -> Status: FAILED
  |     -> Action: Fix migration SQL, redeploy
  |
  +-- Service restart failed?
  |     -> Git rolled back to previous SHA
  |     -> Status: FAILED
  |     -> Action: Check systemd logs, fix service config
  |
  +-- Health check failed?
  |     -> Full rollback triggered:
  |     |    1. Migrate down (reverse applied migrations)
  |     |    2. Git checkout previous SHA
  |     |    3. Restart service
  |     |
  |     +-- Rollback succeeded?
  |     |     -> Status: ROLLED_BACK
  |     |     -> Action: Check app logs, fix code, redeploy
  |     |
  |     +-- Rollback failed?
  |           -> Status: ROLLBACK_FAILED
  |           -> Critical notification sent to operators
  |           -> Incident file written to /var/lib/fraisier/incidents/
  |           -> Action: MANUAL INTERVENTION REQUIRED (see below)
  |
  +-- Deployment timed out?
  |     -> Same rollback flow as health check failure
  |     -> Action: Increase timeout or investigate slow startup
  |
  +-- Deploy succeeded
        -> Status: SUCCESS
        -> Notifications sent
```

## Failure Scenarios

### Migration Failure

**What happens**: Code is pulled to the new commit, then `confiture migrate up` fails (bad SQL, connection error, lock contention).

**What fraisier does**:
1. Catches the migration error
2. Rolls git back to the previous SHA (`git checkout -f <old_sha>`)
3. Restarts the service so it runs old code against old schema
4. Returns `FAILED` status with error details

**What you should do**: Fix the migration SQL and redeploy.

### Health Check Failure

**What happens**: Code deployed, migrations applied, service restarted, but the health endpoint doesn't return 2xx/3xx within the retry window.

**What fraisier does**:
1. Triggers full rollback via `deployer.rollback()`
2. Rolls back migrations (`confiture migrate down --steps=N`)
3. Checks out previous git SHA
4. Restarts service
5. Returns `ROLLED_BACK` status

**What you should do**: Check application logs. The new code likely crashes on startup or binds to a different port.

### Double Failure (Rollback Failed)

**What happens**: Health check fails, then the rollback itself fails (e.g., down migration has a bug, git checkout fails).

**What fraisier does**:
1. Logs `CRITICAL` error
2. Sends notification to all configured `on_failure` channels
3. Writes incident file to `/var/lib/fraisier/incidents/`
4. Returns `ROLLBACK_FAILED` status with both errors

**What you should do**:
1. Read the incident file for full context
2. Check database state: `confiture migrate status`
3. Manually run `confiture migrate down` if needed
4. Manually checkout the correct git SHA
5. Restart the service
6. **Do NOT restart the service until the database schema matches the code**

### SSH Drop / Process Kill

**What happens**: The deploy process is interrupted mid-way (SSH connection drops, process killed, server reboot).

**What fraisier does**:
- File lock (`fcntl.flock`) is automatically released when the process dies
- No stale lock cleanup needed

**What you should do**:
- Simply retry the deploy
- Confiture tracks which migrations are applied; `migrate up` only applies pending ones
- The retry is safe and idempotent

### Concurrent Deploy Attempt

**What happens**: Two deploys for the same fraise are triggered simultaneously (e.g., two git pushes in quick succession).

**What fraisier does**:
- Second deploy is blocked by file lock
- Webhook returns `409 Conflict` / `skipped` status
- First deploy continues normally

**What you should do**: Nothing. The second deploy will be picked up on the next webhook or manual trigger.

## Notification Behavior

| Status | Event Type | Routed To |
|--------|-----------|-----------|
| SUCCESS | `success` | `on_success` notifiers |
| ROLLED_BACK | `rollback` | `on_rollback` notifiers |
| ROLLBACK_FAILED | `rollback_failed` | `on_failure` notifiers |
| FAILED | `failure` | `on_failure` notifiers |

`ROLLBACK_FAILED` events are routed to `on_failure` handlers because they represent the most critical state requiring operator attention.
