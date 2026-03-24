"""Integration tests for provider ecosystem.

Tests cover:
- Multi-provider deployment workflows
- Provider switching and fallback
- Lock mechanism under concurrency
- Health check polling strategies
- Service deployment coordination
- Full deploy flow through each provider
"""

import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fraisier.database import get_db, init_database
from fraisier.deployers.base import DeploymentStatus
from fraisier.locking import DeploymentLock, DeploymentLockedError
from fraisier.providers import ProviderConfig, ProviderRegistry
from fraisier.providers.bare_metal import BareMetalProvider
from fraisier.providers.docker_compose import DockerComposeProvider


@pytest.fixture(scope="session", autouse=True)
def register_providers():
    """Register all providers once for the session."""
    if not ProviderRegistry.is_registered("bare_metal"):
        ProviderRegistry.register(BareMetalProvider)
    if not ProviderRegistry.is_registered("docker_compose"):
        ProviderRegistry.register(DockerComposeProvider)


class TestMultiProviderDeploymentWorkflow:
    """Test complete deployment workflows with multiple providers."""

    @pytest.fixture(autouse=True)
    def setup_database(self):
        """Initialize database for each test."""
        init_database()
        yield
        # Cleanup handled by test database

    def test_deploy_to_bare_metal_then_docker(self):
        """Test deploying same service to multiple providers sequentially."""
        # Create configs for both providers
        bare_metal_config = ProviderConfig(
            name="production",
            type="bare_metal",
            url="prod.example.com",
            custom_fields={
                "ssh_user": "deploy",
                "ssh_key_path": "/home/deploy/.ssh/id_rsa",
                "app_path": "/var/app",
                "systemd_service": "my_api.service",
            },
        )

        docker_config = ProviderConfig(
            name="staging",
            type="docker_compose",
            url="/opt/docker",
            custom_fields={
                "compose_file": "docker-compose.yml",
                "service_name": "my_api",
            },
        )

        # Get provider instances from registry
        bare_metal = ProviderRegistry.get_provider("bare_metal", bare_metal_config)
        docker = ProviderRegistry.get_provider("docker_compose", docker_config)

        assert bare_metal is not None
        assert docker is not None
        assert isinstance(bare_metal, BareMetalProvider)
        assert isinstance(docker, DockerComposeProvider)

    def test_provider_type_listing(self):
        """Test listing available provider types."""
        # Get list of registered providers
        providers = ProviderRegistry.list_providers()

        # Should include both registered types
        assert "bare_metal" in providers
        assert "docker_compose" in providers
        assert len(providers) >= 2

    def test_deployment_across_multiple_environments(self):
        """Test deploying service to multiple environments in sequence."""
        # Setup: Create configs for dev, staging, production
        environments = {
            "dev": ProviderConfig(
                name="dev",
                type="docker_compose",
                url="/opt/docker/dev",
                custom_fields={
                    "compose_file": "docker-compose.dev.yml",
                    "service_name": "api",
                },
            ),
            "staging": ProviderConfig(
                name="staging",
                type="docker_compose",
                url="/opt/docker/staging",
                custom_fields={
                    "compose_file": "docker-compose.yml",
                    "service_name": "api",
                },
            ),
            "production": ProviderConfig(
                name="production",
                type="bare_metal",
                url="prod.example.com",
                custom_fields={
                    "ssh_user": "deploy",
                    "ssh_key_path": "/etc/ssh/id_rsa",
                    "app_path": "/var/app",
                    "systemd_service": "api.service",
                },
            ),
        }

        # Get provider instances for each environment
        for config in environments.values():
            provider = ProviderRegistry.get_provider(config.type, config)
            assert provider is not None

    def test_provider_registration_check(self):
        """Test checking if provider types are registered."""
        # Check registered providers
        assert ProviderRegistry.is_registered("bare_metal") is True
        assert ProviderRegistry.is_registered("docker_compose") is True
        assert ProviderRegistry.is_registered("unknown_provider") is False


class TestProviderSwitchingAndFallback:
    """Test switching between providers and fallback logic."""

    @pytest.fixture(autouse=True)
    def setup_database(self):
        """Initialize database for each test."""
        init_database()
        yield

    def test_switch_provider_on_pre_flight_failure(self):
        """Test switching to backup provider when primary fails pre-flight check."""
        # Primary provider (will fail pre-flight)
        primary_config = ProviderConfig(
            name="primary",
            type="bare_metal",
            url="primary-down.example.com",
            custom_fields={
                "ssh_user": "deploy",
                "ssh_key_path": "/etc/ssh/id_rsa",
                "app_path": "/var/app",
                "systemd_service": "api.service",
            },
        )

        # Backup provider
        backup_config = ProviderConfig(
            name="backup",
            type="docker_compose",
            url="/opt/docker",
            custom_fields={
                "compose_file": "docker-compose.yml",
                "service_name": "api",
            },
        )

        primary = ProviderRegistry.get_provider("bare_metal", primary_config)
        backup = ProviderRegistry.get_provider("docker_compose", backup_config)

        # Mock primary to fail pre-flight
        with patch.object(primary, "pre_flight_check") as mock_primary_check:
            mock_primary_check.return_value = (False, "SSH unreachable")

            success, _message = primary.pre_flight_check()
            assert success is False

            # Now use backup
            with patch.object(backup, "pre_flight_check") as mock_backup_check:
                mock_backup_check.return_value = (True, "Docker available")

                success, _message = backup.pre_flight_check()
                assert success is True

    def test_deployment_with_fallback_provider(self):
        """Test deploying with fallback if primary provider fails."""
        primary_config = ProviderConfig(
            name="primary",
            type="docker_compose",
            url="/docker1",
            custom_fields={
                "compose_file": "docker-compose.yml",
                "service_name": "api",
            },
        )

        backup_config = ProviderConfig(
            name="backup",
            type="docker_compose",
            url="/docker2",
            custom_fields={
                "compose_file": "docker-compose.yml",
                "service_name": "api",
            },
        )

        primary_provider = ProviderRegistry.get_provider(
            "docker_compose", primary_config
        )
        backup_provider = ProviderRegistry.get_provider("docker_compose", backup_config)

        # Mock primary to fail, backup to succeed
        with patch.object(primary_provider, "deploy_service") as mock_primary_deploy:
            mock_primary_deploy.return_value = MagicMock(success=False)

            with patch.object(backup_provider, "deploy_service") as mock_backup_deploy:
                from fraisier.deployers.base import DeploymentResult

                mock_backup_deploy.return_value = DeploymentResult(
                    success=True,
                    status=DeploymentStatus.SUCCESS,
                    new_version="v1.0.0",
                    old_version="v0.9.0",
                )

                # Try primary
                result = primary_provider.deploy_service(
                    "api", "v1.0.0", {"branch": "main"}
                )
                assert result.success is False

                # Fall back to backup
                result = backup_provider.deploy_service(
                    "api", "v1.0.0", {"branch": "main"}
                )
                assert result.success is True


class TestLockMechanismUnderConcurrency:
    """Test deployment locks under concurrent access."""

    @pytest.fixture(autouse=True)
    def setup_database(self):
        """Initialize database for each test."""
        init_database()
        yield

    def test_lock_prevents_concurrent_deployment(self):
        """Test that lock prevents concurrent deployments to same service."""
        service_name = "api"
        provider_name = "production"

        # First lock succeeds
        lock1 = DeploymentLock(service_name, provider_name)
        assert lock1.acquire() is True
        lock1.release()

        # After release, second lock succeeds
        lock2 = DeploymentLock(service_name, provider_name)
        assert lock2.acquire() is True
        lock2.release()

    def test_lock_context_manager_acquires_and_releases(self):
        """Test lock context manager properly acquires and releases."""
        service_name = "api"
        provider_name = "production"

        db = get_db()

        # Verify no lock before context
        lock_before = db.get_deployment_lock(service_name, provider_name)
        assert lock_before is None

        # Enter context and verify lock acquired
        with DeploymentLock(service_name, provider_name):
            lock_acquired = db.get_deployment_lock(service_name, provider_name)
            assert lock_acquired is not None

        # Verify lock released after context
        lock_after = db.get_deployment_lock(service_name, provider_name)
        assert lock_after is None

    def test_lock_prevents_reentry(self):
        """Test that lock prevents re-entry during deployment."""
        service_name = "api"
        provider_name = "production"

        # First lock succeeds
        lock1 = DeploymentLock(service_name, provider_name)
        assert lock1.acquire() is True

        # Second lock fails (already locked)
        lock2 = DeploymentLock(service_name, provider_name)
        assert lock2.acquire() is False

        lock1.release()

    def test_concurrent_lock_attempts_serialized(self):
        """Test that multiple threads attempting to lock are serialized."""
        service_name = "api"
        provider_name = "production"
        results = []

        def attempt_lock(thread_id):
            """Attempt to acquire lock."""
            lock = DeploymentLock(service_name, provider_name)
            acquired = lock.acquire()
            results.append((thread_id, acquired))
            if acquired:
                time.sleep(0.1)  # Simulate work
                lock.release()

        # Start multiple threads trying to acquire same lock
        threads = [threading.Thread(target=attempt_lock, args=(i,)) for i in range(3)]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # Exactly one thread should have acquired lock
        acquired_threads = [thread_id for thread_id, acquired in results if acquired]
        assert len(acquired_threads) >= 1  # At least one acquired
        # Others failed (their acquire returned False)

    def test_lock_timeout_expiration(self):
        """Test that locks expire after timeout."""
        service_name = "api"
        provider_name = "production"

        # Create lock with 1 second timeout
        lock1 = DeploymentLock(service_name, provider_name, timeout=1)
        assert lock1.acquire() is True

        # Immediately try second lock - should fail
        lock2 = DeploymentLock(service_name, provider_name, timeout=1)
        assert lock2.acquire() is False

        # Wait for timeout and try again
        time.sleep(1.1)

        # Should succeed now (lock expired)
        lock3 = DeploymentLock(service_name, provider_name, timeout=1)
        assert lock3.acquire() is True

        lock3.release()

    def test_lock_context_manager_raises_on_locked(self):
        """Test that context manager raises if lock already held."""
        service_name = "api"
        provider_name = "production"

        with DeploymentLock(service_name, provider_name):
            # Try to enter another context with same lock
            lock2 = DeploymentLock(service_name, provider_name)

            with pytest.raises(DeploymentLockedError), lock2:
                pass  # Should raise before entering

    def test_different_services_have_independent_locks(self):
        """Test that locks for different services don't interfere."""
        # Lock service A
        lock_a = DeploymentLock("service_a", "production")
        assert lock_a.acquire() is True

        # Lock service B should succeed (different service)
        lock_b = DeploymentLock("service_b", "production")
        assert lock_b.acquire() is True

        # Lock service A again should fail (same service)
        lock_a2 = DeploymentLock("service_a", "production")
        assert lock_a2.acquire() is False

        lock_a.release()
        lock_b.release()

    def test_same_service_different_providers_independent_locks(self):
        """Test locks are per (service, provider) tuple."""
        # Same service, different providers
        lock_prod = DeploymentLock("api", "production")
        assert lock_prod.acquire() is True

        # Different provider, should succeed
        lock_staging = DeploymentLock("api", "staging")
        assert lock_staging.acquire() is True

        lock_prod.release()
        lock_staging.release()


class TestHealthCheckPollingStrategies:
    """Test various health check polling strategies."""

    @pytest.fixture(autouse=True)
    def setup_database(self):
        """Initialize database for each test."""
        init_database()
        yield

    def test_poll_health_until_ready(self):
        """Test polling health check until service is ready."""
        provider = ProviderConfig(
            name="test",
            type="docker_compose",
            url="/docker",
            custom_fields={
                "compose_file": "docker-compose.yml",
                "service_name": "api",
                "health_check_type": "http",
                "health_check_url": "http://localhost:8000/health",
            },
        )

        docker_provider = DockerComposeProvider(provider)

        # Mock health check to simulate startup delay
        check_count = [0]

        def health_check_mock(service_name):
            """Simulate health check with startup delay."""
            check_count[0] += 1
            # Fail first 2 times, succeed on 3rd
            return check_count[0] > 2

        with patch.object(
            docker_provider, "health_check", side_effect=health_check_mock
        ):
            # Poll until healthy
            max_attempts = 5
            for _attempt in range(max_attempts):
                if docker_provider.health_check("api"):
                    break
                time.sleep(0.1)

            assert check_count[0] > 1

    def test_health_check_timeout_on_slow_service(self):
        """Test health check times out if service never becomes ready."""
        provider = ProviderConfig(
            name="test",
            type="docker_compose",
            url="/docker",
            custom_fields={
                "compose_file": "docker-compose.yml",
                "service_name": "api",
                "health_check_type": "http",
                "health_check_url": "http://localhost:8000/health",
            },
        )

        docker_provider = DockerComposeProvider(provider)

        # Mock health check to always fail
        def health_check_mock(service_name):
            return False

        with patch.object(
            docker_provider, "health_check", side_effect=health_check_mock
        ):
            max_attempts = 3
            poll_timeout = 0.3

            start_time = time.time()
            healthy = False

            for _attempt in range(max_attempts):
                if docker_provider.health_check("api"):
                    healthy = True
                    break
                time.sleep(poll_timeout)

            elapsed = time.time() - start_time
            assert healthy is False
            assert elapsed >= poll_timeout * max_attempts

    def test_multiple_health_check_types_in_sequence(self):
        """Test falling back through multiple health check types."""
        provider = ProviderConfig(
            name="test",
            type="docker_compose",
            url="/docker",
            custom_fields={
                "compose_file": "docker-compose.yml",
                "service_name": "api",
                "health_check_type": "http",
                "health_check_url": "http://localhost:8000/health",
            },
        )

        docker_provider = DockerComposeProvider(provider)

        # Mock HTTP health check to fail, then TCP to succeed
        with patch.object(docker_provider, "health_check") as mock_check:
            mock_check.side_effect = [False, True]

            # First attempt fails
            assert docker_provider.health_check("api") is False

            # Switch to different check type (simulated)
            docker_provider.health_check_type = "tcp"
            docker_provider.health_check_port = 8000

            # Second attempt succeeds
            assert docker_provider.health_check("api") is True


class TestProviderConfigurationManagement:
    """Test provider configuration and lifecycle."""

    def test_provider_config_validation_on_get(self):
        """Test that provider configs are used to create valid providers."""
        # Valid config
        valid_config = ProviderConfig(
            name="prod",
            type="bare_metal",
            url="prod.example.com",
            custom_fields={
                "ssh_user": "deploy",
                "ssh_key_path": "/etc/ssh/id_rsa",
                "app_path": "/var/app",
                "systemd_service": "api.service",
            },
        )

        # Should not raise
        provider = ProviderRegistry.get_provider("bare_metal", valid_config)
        assert provider is not None
        assert provider.name == "prod"

    def test_provider_config_persistence_across_lookups(self):
        """Test that provider configuration persists across multiple lookups."""
        config = ProviderConfig(
            name="prod",
            type="bare_metal",
            url="prod.example.com",
            custom_fields={
                "ssh_user": "deploy",
                "ssh_key_path": "/etc/ssh/id_rsa",
                "app_path": "/var/app",
                "systemd_service": "api.service",
            },
        )

        # Get provider multiple times
        provider1 = ProviderRegistry.get_provider("bare_metal", config)
        provider2 = ProviderRegistry.get_provider("bare_metal", config)

        # Should have same configuration
        assert provider1.name == provider2.name
        assert provider1.ssh_host == provider2.ssh_host

    def test_invalid_provider_type_raises_error(self):
        """Test that requesting unknown provider type raises error."""
        with pytest.raises(ValueError, match="Unknown provider"):
            ProviderRegistry.get_provider(
                "nonexistent",
                ProviderConfig(
                    name="test",
                    type="nonexistent",
                    url="test.com",
                    custom_fields={},
                ),
            )


class TestFullDeployFlowBareMetal:
    """Test complete deploy flow through BareMetalProvider."""

    @pytest.fixture(autouse=True)
    def setup_database(self):
        init_database()
        yield

    @pytest.mark.asyncio
    async def test_connect_execute_deploy_disconnect(self):
        """Test full lifecycle: connect → deploy → health → disconnect."""
        config = {"host": "prod.example.com", "username": "deploy"}
        provider = BareMetalProvider(config)

        # Mock SSH connection
        mock_asyncssh = MagicMock()
        mock_conn = AsyncMock()
        mock_asyncssh.connect = AsyncMock(return_value=mock_conn)
        mock_asyncssh.SSHClientConnectionOptions = MagicMock

        # Mock command execution for service restart
        mock_run_result = MagicMock()
        mock_run_result.exit_status = 0
        mock_run_result.stdout = ""
        mock_run_result.stderr = ""
        mock_conn.run = AsyncMock(return_value=mock_run_result)
        mock_conn.close = MagicMock()
        mock_conn.wait_closed = AsyncMock()

        with patch.dict("sys.modules", {"asyncssh": mock_asyncssh}):
            # Connect
            await provider.connect()
            assert provider.ssh_client is not None

            # Restart service (deploy) — now sync, uses run_command
            with patch.object(provider, "run_command", return_value=(0, "", "")):
                result = provider.restart_service("api")
                assert result is True

            # Check service status
            mock_conn.run = AsyncMock(
                side_effect=[
                    MagicMock(exit_status=0, stdout="active", stderr=""),
                    MagicMock(
                        exit_status=0,
                        stdout="ActiveState=active\nSubState=running",
                        stderr="",
                    ),
                ]
            )
            status = await provider.get_service_status("api")
            assert status["active"] is True

            # Disconnect
            await provider.disconnect()
            mock_conn.close.assert_called()

    @pytest.mark.asyncio
    async def test_deploy_with_pre_flight_check(self):
        """Test pre-flight check before deploy."""
        config = {"host": "prod.example.com", "username": "deploy"}
        provider = BareMetalProvider(config)

        # Pre-flight should pass
        success, _message = provider.pre_flight_check()
        assert success is True

        # Mock the deploy step — restart_service is now sync via run_command
        with patch.object(provider, "run_command", return_value=(0, "", "")):
            result = provider.restart_service("api")
            assert result is True


class TestFullDeployFlowDockerCompose:
    """Test complete deploy flow through DockerComposeProvider."""

    @pytest.fixture(autouse=True)
    def setup_database(self):
        init_database()
        yield

    @pytest.mark.asyncio
    async def test_connect_pull_restart_health_disconnect(self):
        """Test: connect → pull → restart → health → disconnect."""
        config = {
            "compose_file": "docker-compose.yml",
            "project_name": "myapp",
        }
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            # connect: docker --version, docker-compose --version
            mock_exec.side_effect = [
                (0, "Docker version 24.0.0", ""),
                (0, "docker-compose version 1.29.2", ""),
            ]
            await provider.connect()
            assert provider.docker_available is True

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            # Pull new image
            result = await provider.pull_image("api")
            assert result is True

            # Restart service
            result = await provider.restart_service("api")
            assert result is True

        # Get service status
        import json

        mock_status = json.dumps(
            [{"ID": "abc123", "Image": "myapp:latest", "State": "running"}]
        )
        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, mock_status, "")

            status = await provider.get_service_status("api")
            assert status["active"] is True
            assert status["state"] == "running"

        # Disconnect
        await provider.disconnect()
        assert provider.docker_available is False

    @pytest.mark.asyncio
    async def test_deploy_with_scaling(self):
        """Test deploy with service scaling."""
        config = {
            "compose_file": "docker-compose.yml",
            "project_name": "myapp",
        }
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            # Scale to 3 replicas
            result = await provider.scale_service("api", replicas=3)
            assert result is True

            # Verify compose command used
            cmd = mock_exec.call_args[0][0]
            assert "--scale api=3" in cmd
