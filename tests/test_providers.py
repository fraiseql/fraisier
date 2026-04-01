"""Tests for deployment providers."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fraisier.providers import (
    BareMetalProvider,
    HealthCheck,
    HealthCheckType,
    ProviderType,
)

try:
    import httpx  # noqa: F401

    _has_httpx = True
except ImportError:
    _has_httpx = False

_skip_no_httpx = pytest.mark.skipif(not _has_httpx, reason="httpx not installed")


class TestBareMetalProvider:
    """Test Bare Metal provider implementation."""

    def test_creation_with_host(self):
        """Test creating provider with required host."""
        config = {
            "host": "prod.example.com",
            "username": "deploy",
            "port": 22,
        }
        provider = BareMetalProvider(config)
        assert provider.host == "prod.example.com"
        assert provider.username == "deploy"
        assert provider.port == 22

    def test_creation_without_host_fails(self):
        """Test that provider requires host."""
        config = {"username": "deploy"}
        with pytest.raises(ValueError):
            BareMetalProvider(config)

    def test_default_values(self):
        """Test provider default values."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)
        assert provider.port == 22
        assert provider.username == "root"

    def test_provider_type(self):
        """Test provider returns correct type."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)
        assert provider._get_provider_type() == ProviderType.BARE_METAL

    @pytest.mark.asyncio
    async def test_connect_without_asyncssh_fails(self):
        """Test connect fails gracefully if asyncssh not available."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with (
            patch.dict("sys.modules", {"asyncssh": None}),
            pytest.raises(ConnectionError),
        ):
            await provider.connect()

    @pytest.mark.asyncio
    async def test_execute_command_not_connected(self):
        """Test execute_command fails if not connected."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with pytest.raises(RuntimeError):
            await provider.execute_command("ls -la")

    @pytest.mark.asyncio
    @_skip_no_httpx
    async def test_health_check_http(self):
        """Test HTTP health check."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)
        provider.ssh_client = MagicMock()

        health_check = HealthCheck(
            type=HealthCheckType.HTTP,
            url="http://localhost:8000/health",
            timeout=5,
            retries=1,
        )

        with patch("httpx.AsyncClient") as mock_client:
            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_client_instance = AsyncMock()
            mock_client_instance.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client_instance.get = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_client_instance

            result = await provider.check_health(health_check)
            assert result is True

    @pytest.mark.asyncio
    async def test_health_check_tcp(self):
        """Test TCP health check."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)
        provider.ssh_client = MagicMock()

        health_check = HealthCheck(
            type=HealthCheckType.TCP,
            port=8000,
            timeout=5,
            retries=1,
        )

        with patch("asyncio.open_connection") as mock_connect:
            mock_reader = AsyncMock()
            mock_writer = AsyncMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_connect.return_value = (mock_reader, mock_writer)

            result = await provider.check_health(health_check)
            assert result is True
            mock_writer.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_service_status_active(self):
        """Test getting status of active service."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)
        provider.ssh_client = MagicMock()

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.side_effect = [
                (0, "active", ""),
                (0, "ActiveState=active\nSubState=running", ""),
            ]

            status = await provider.get_service_status("api")
            assert status["service"] == "api"
            assert status["active"] is True

    @pytest.mark.asyncio
    async def test_get_service_status_inactive(self):
        """Test getting status of inactive service."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)
        provider.ssh_client = MagicMock()

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (3, "", "Unit api.service could not be found")

            status = await provider.get_service_status("api")
            assert status["service"] == "api"
            assert status["active"] is False

    def test_start_service_success(self):
        """Test starting a service."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with patch.object(provider, "run_command", return_value=(0, "", "")):
            result = provider.start_service("api")
            assert result is True

    def test_restart_service_success(self):
        """Test restarting a service."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with patch.object(provider, "run_command", return_value=(0, "", "")):
            result = provider.restart_service("api")
            assert result is True

    def test_enable_service_success(self):
        """Test enabling a service."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with patch.object(provider, "run_command", return_value=(0, "", "")):
            result = provider.enable_service("api")
            assert result is True

    def test_stop_service_success(self):
        """Test stopping a service."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with patch.object(
            provider, "run_command", return_value=(0, "", "")
        ) as mock_run:
            result = provider.stop_service("api")
            assert result is True
            mock_run.assert_called_once_with(
                "sudo systemctl stop api.service", timeout=60
            )

    def test_stop_service_failure(self):
        """Test stop_service returns False on failure."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with patch.object(
            provider, "run_command", return_value=(1, "", "Failed to stop")
        ):
            result = provider.stop_service("api")
            assert result is False

    def test_start_service_failure(self):
        """Test start_service returns False on failure."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with patch.object(
            provider, "run_command", return_value=(1, "", "Unit not found")
        ):
            result = provider.start_service("nonexistent")
            assert result is False

    def test_restart_service_failure(self):
        """Test restart_service returns False on failure."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with patch.object(
            provider, "run_command", return_value=(1, "", "Restart failed")
        ):
            result = provider.restart_service("api")
            assert result is False

    def test_enable_service_failure(self):
        """Test enable_service returns False on failure."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with patch.object(
            provider, "run_command", return_value=(1, "", "Enable failed")
        ):
            result = provider.enable_service("api")
            assert result is False

    def test_service_status_parses_systemctl_output(self):
        """Test service_status correctly parses systemctl is-active output."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with patch.object(provider, "run_command", return_value=(0, "active\n", "")):
            status = provider.service_status("api")
            assert status["service"] == "api"
            assert status["active"] is True
            assert status["state"] == "active"

    def test_service_status_on_exception(self):
        """Test service_status propagates run_command exceptions."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with (
            patch.object(
                provider,
                "run_command",
                side_effect=RuntimeError("SSH disconnected"),
            ),
            pytest.raises(RuntimeError, match="SSH disconnected"),
        ):
            provider.service_status("api")

    def test_service_operations_use_correct_systemctl_commands(self):
        """Test that service operations call correct systemctl commands."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with patch.object(
            provider, "run_command", return_value=(0, "", "")
        ) as mock_run:
            provider.start_service("myapi")
            assert mock_run.call_args[0][0] == "sudo systemctl start myapi.service"

            provider.stop_service("myapi")
            assert mock_run.call_args[0][0] == "sudo systemctl stop myapi.service"

            provider.restart_service("myapi")
            assert mock_run.call_args[0][0] == "sudo systemctl restart myapi.service"

            provider.enable_service("myapi")
            assert mock_run.call_args[0][0] == "sudo systemctl enable myapi.service"

    @pytest.mark.asyncio
    async def test_connect_success_with_mocked_asyncssh(self):
        """Test successful SSH connection with mocked asyncssh."""
        config = {"host": "server.com", "username": "deploy", "port": 22}
        provider = BareMetalProvider(config)

        mock_asyncssh = MagicMock()
        mock_conn = AsyncMock()
        mock_asyncssh.connect = AsyncMock(return_value=mock_conn)
        mock_asyncssh.SSHClientConnectionOptions = MagicMock

        with patch.dict("sys.modules", {"asyncssh": mock_asyncssh}):
            await provider.connect()

        assert provider.ssh_client is mock_conn

    @pytest.mark.asyncio
    async def test_connect_with_key_path(self):
        """Test SSH connection passes key_path to asyncssh."""
        config = {
            "host": "server.com",
            "key_path": "/home/deploy/.ssh/id_rsa",
        }
        provider = BareMetalProvider(config)

        mock_asyncssh = MagicMock()
        mock_conn = AsyncMock()
        mock_asyncssh.connect = AsyncMock(return_value=mock_conn)
        mock_asyncssh.SSHClientConnectionOptions = MagicMock

        with patch.dict("sys.modules", {"asyncssh": mock_asyncssh}):
            await provider.connect()

        assert provider.ssh_client is mock_conn

    @pytest.mark.asyncio
    async def test_connect_ssh_timeout(self):
        """Test SSH connection timeout raises ConnectionError."""
        config = {"host": "unreachable.server.com"}
        provider = BareMetalProvider(config)

        mock_asyncssh = MagicMock()
        mock_asyncssh.connect = AsyncMock(
            side_effect=TimeoutError("Connection timed out")
        )
        mock_asyncssh.SSHClientConnectionOptions = MagicMock

        with (
            patch.dict("sys.modules", {"asyncssh": mock_asyncssh}),
            pytest.raises(ConnectionError, match="Failed to connect"),
        ):
            await provider.connect()

    @pytest.mark.asyncio
    async def test_execute_command_when_connected(self):
        """Test successful command execution over SSH."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        mock_result = MagicMock()
        mock_result.exit_status = 0
        mock_result.stdout = "hello\n"
        mock_result.stderr = ""

        mock_client = MagicMock()
        mock_client.run = AsyncMock(return_value=mock_result)
        provider.ssh_client = mock_client

        exit_code, stdout, stderr = await provider.execute_command("echo hello")
        assert exit_code == 0
        assert stdout == "hello\n"
        assert stderr == ""

    @pytest.mark.asyncio
    async def test_execute_command_timeout(self):
        """Test command execution timeout."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        mock_client = MagicMock()
        mock_client.run = AsyncMock(side_effect=TimeoutError())
        provider.ssh_client = mock_client

        with pytest.raises(RuntimeError, match="Command timed out"):
            await provider.execute_command("sleep 999", timeout=1)

    @pytest.mark.asyncio
    async def test_disconnect_closes_connection(self):
        """Test disconnect properly closes SSH connection."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        mock_client = MagicMock()
        mock_client.close = MagicMock()
        mock_client.wait_closed = AsyncMock()
        provider.ssh_client = mock_client

        await provider.disconnect()
        mock_client.close.assert_called_once()
        mock_client.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self):
        """Test disconnect is safe when not connected."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)
        assert provider.ssh_client is None

        # Should not raise
        await provider.disconnect()

    @pytest.mark.asyncio
    async def test_upload_file_not_connected(self):
        """Test upload_file fails when not connected."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with pytest.raises(RuntimeError, match="Not connected"):
            await provider.upload_file("/local/file.txt", "/remote/file.txt")

    @pytest.mark.asyncio
    async def test_download_file_not_connected(self):
        """Test download_file fails when not connected."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)

        with pytest.raises(RuntimeError, match="Not connected"):
            await provider.download_file("/remote/file.txt", "/local/file.txt")

    def test_connection_timeout_configurable(self):
        """Test connection timeout default and configuration."""
        config = {"host": "server.com"}
        provider = BareMetalProvider(config)
        assert provider._connection_timeout == 10

        # Can be overridden
        provider._connection_timeout = 30
        assert provider._connection_timeout == 30


class TestHealthCheck:
    """Test health check configuration."""

    def test_health_check_http_defaults(self):
        """Test HTTP health check defaults."""
        hc = HealthCheck(type=HealthCheckType.HTTP, url="http://localhost:8000")
        assert hc.timeout == 30
        assert hc.retries == 3
        assert hc.retry_delay == 2

    def test_health_check_tcp_config(self):
        """Test TCP health check configuration."""
        hc = HealthCheck(type=HealthCheckType.TCP, port=3306, timeout=10)
        assert hc.port == 3306
        assert hc.timeout == 10

    def test_health_check_exec_config(self):
        """Test exec health check configuration."""
        hc = HealthCheck(
            type=HealthCheckType.EXEC,
            command="curl http://localhost:8000/health",
        )
        assert hc.command == "curl http://localhost:8000/health"

    def test_health_check_systemd_config(self):
        """Test systemd health check configuration."""
        hc = HealthCheck(type=HealthCheckType.SYSTEMD, service="api")
        assert hc.service == "api"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestPreFlightChecks:
    """Test pre-flight checks for each provider."""

    def test_bare_metal_pre_flight_missing_key(self):
        """Test BareMetalProvider pre-flight fails with missing SSH key."""
        config = {
            "host": "server.com",
            "key_path": "/nonexistent/id_rsa",
        }
        provider = BareMetalProvider(config)

        success, message = provider.pre_flight_check()
        assert success is False
        assert "key" in message.lower() or "ssh" in message.lower()

    def test_bare_metal_pre_flight_success(self):
        """Test BareMetalProvider pre-flight succeeds with valid config."""
        config = {
            "host": "server.com",
            "username": "deploy",
        }
        provider = BareMetalProvider(config)

        success, message = provider.pre_flight_check()
        assert success is True
        assert "passed" in message.lower()

    def test_bare_metal_pre_flight_with_key(self, tmp_path):
        """Test BareMetalProvider pre-flight with existing key file."""
        key_file = tmp_path / "id_rsa"
        key_file.write_text("fake-key")

        config = {
            "host": "server.com",
            "key_path": str(key_file),
        }
        provider = BareMetalProvider(config)

        success, message = provider.pre_flight_check()
        assert success is True
        assert "key=" in message

    def test_docker_compose_pre_flight_checks_compose_file(self):
        """Test DockerComposeProvider pre-flight validates compose file."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "/nonexistent/docker-compose.yml"}
        provider = DockerComposeProvider(config)

        success, message = provider.pre_flight_check()
        assert success is False
        assert "compose" in message.lower()

    def test_docker_compose_pre_flight_success(self, tmp_path):
        """Test DockerComposeProvider pre-flight with existing file."""
        from fraisier.providers import DockerComposeProvider

        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("version: '3'\nservices:\n  web:\n    image: nginx\n")

        config = {"compose_file": str(compose_file)}
        provider = DockerComposeProvider(config)

        success, _message = provider.pre_flight_check()
        assert success is True


class TestDockerComposeProvider:
    """Test Docker Compose provider implementation."""

    def test_creation_with_defaults(self):
        """Test creating provider with default values."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)
        assert provider.compose_file == "docker-compose.yml"
        assert provider.project_name == "fraisier"
        assert provider.timeout == 300

    def test_creation_with_config(self):
        """Test creating provider with custom config."""
        from fraisier.providers import DockerComposeProvider

        config = {
            "compose_file": "docker-compose.prod.yml",
            "project_name": "my_app",
            "timeout": 600,
        }
        provider = DockerComposeProvider(config)
        assert provider.compose_file == "docker-compose.prod.yml"
        assert provider.project_name == "my_app"
        assert provider.timeout == 600

    def test_provider_type(self):
        """Test provider returns correct type."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)
        assert provider._get_provider_type() == ProviderType.DOCKER_COMPOSE

    @pytest.mark.asyncio
    async def test_connect_without_docker_fails(self):
        """Test connect fails if docker not available."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (1, "", "docker: command not found")
            with pytest.raises(ConnectionError):
                await provider.connect()

    @pytest.mark.asyncio
    async def test_get_service_status(self):
        """Test getting service status."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        # Mock the command execution
        mock_output = json.dumps(
            [
                {
                    "ID": "abc123def456",
                    "Image": "nginx:latest",
                    "State": "running",
                }
            ]
        )

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, mock_output, "")

            status = await provider.get_service_status("web")
            assert status["service"] == "web"
            assert status["active"] is True
            assert status["state"] == "running"

    @pytest.mark.asyncio
    async def test_start_service_success(self):
        """Test starting a service."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            result = await provider.start_service("web")
            assert result is True
            mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_service_success(self):
        """Test stopping a service."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            result = await provider.stop_service("web")
            assert result is True

    @pytest.mark.asyncio
    async def test_restart_service_success(self):
        """Test restarting a service."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            result = await provider.restart_service("web")
            assert result is True

    @pytest.mark.asyncio
    async def test_pull_image_success(self):
        """Test pulling latest image."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "Pulling from nginx...", "")

            result = await provider.pull_image("web")
            assert result is True

    @pytest.mark.asyncio
    async def test_get_container_logs(self):
        """Test retrieving container logs."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        mock_logs = "web_1  | nginx started\nweb_1  | listening on port 80"

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, mock_logs, "")

            logs = await provider.get_container_logs("web", lines=50)
            assert "nginx started" in logs

    @pytest.mark.asyncio
    async def test_scale_service(self):
        """Test scaling a service."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            result = await provider.scale_service("api", replicas=3)
            assert result is True

    @pytest.mark.asyncio
    async def test_validate_compose_file_success(self):
        """Test compose file validation."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            result = await provider.validate_compose_file()
            assert result is True

    @pytest.mark.asyncio
    async def test_get_service_env(self):
        """Test getting service environment variables."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        mock_env = (
            "PATH=/usr/local/sbin:/usr/local/bin\nDATABASE_URL=postgres://localhost"
        )

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, mock_env, "")

            env = await provider.get_service_env("web")
            assert env["DATABASE_URL"] == "postgres://localhost"

    @pytest.mark.asyncio
    async def test_execute_command_success(self):
        """Test command execution."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.communicate = AsyncMock(return_value=(b"output", b""))
            mock_exec.return_value = mock_process

            exit_code, stdout, _stderr = await provider.execute_command("echo hello")
            assert exit_code == 0
            assert stdout == "output"

    @pytest.mark.asyncio
    async def test_execute_command_timeout(self):
        """Test command timeout."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.communicate = AsyncMock(side_effect=TimeoutError())
            mock_process.kill = MagicMock()
            mock_exec.return_value = mock_process

            with pytest.raises(RuntimeError):
                await provider.execute_command("sleep 1000", timeout=1)

    @pytest.mark.asyncio
    async def test_health_check_tcp(self):
        """Test TCP health check for Docker Compose."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)

        health_check = HealthCheck(
            type=HealthCheckType.TCP,
            port=5432,
            timeout=5,
            retries=1,
        )

        with patch("asyncio.open_connection") as mock_connect:
            mock_reader = AsyncMock()
            mock_writer = AsyncMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_connect.return_value = (mock_reader, mock_writer)

            result = await provider.check_health(health_check)
            assert result is True
            mock_writer.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_with_compose_v2(self):
        """Test connect succeeds with docker compose v2 (no hyphen)."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            # docker ok, docker-compose fails, docker compose v2 ok
            mock_exec.side_effect = [
                (0, "Docker version 24.0.0", ""),
                (1, "", "docker-compose: command not found"),
                (0, "Docker Compose version v2.20.0", ""),
            ]

            await provider.connect()
            assert provider.docker_available is True
            assert provider.compose_command == "docker compose"

    @pytest.mark.asyncio
    async def test_connect_prefers_v1_when_available(self):
        """Test connect uses docker-compose (v1) when available."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.side_effect = [
                (0, "Docker version 24.0.0", ""),
                (0, "docker-compose version 1.29.2", ""),
            ]

            await provider.connect()
            assert provider.docker_available is True
            assert provider.compose_command == "docker-compose"

    @pytest.mark.asyncio
    async def test_connect_fails_no_compose_available(self):
        """Test connect fails when neither compose v1 nor v2 available."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.side_effect = [
                (0, "Docker version 24.0.0", ""),
                (1, "", "docker-compose: not found"),
                (1, "", "docker compose: not found"),
            ]

            with pytest.raises(ConnectionError):
                await provider.connect()

    @pytest.mark.asyncio
    async def test_service_commands_use_detected_compose(self):
        """Test service ops use the detected compose command."""
        from fraisier.providers import DockerComposeProvider

        config = {
            "compose_file": "docker-compose.yml",
            "project_name": "test",
        }
        provider = DockerComposeProvider(config)
        provider.compose_command = "docker compose"

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            await provider.start_service("web")
            cmd = mock_exec.call_args[0][0]
            assert cmd.startswith("docker compose")
            assert "-f docker-compose.yml" in cmd

    @pytest.mark.asyncio
    async def test_health_check_exec_success(self):
        """Test exec health check for Docker Compose."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)

        health_check = HealthCheck(
            type=HealthCheckType.EXEC,
            command="docker exec app curl localhost:8000/health",
            timeout=5,
            retries=1,
        )

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "OK", "")

            result = await provider.check_health(health_check)
            assert result is True

    @pytest.mark.asyncio
    async def test_health_check_exec_failure(self):
        """Test exec health check failure."""
        from fraisier.providers import DockerComposeProvider

        config = {}
        provider = DockerComposeProvider(config)

        health_check = HealthCheck(
            type=HealthCheckType.EXEC,
            command="docker exec app curl localhost:8000/health",
            timeout=5,
            retries=1,
        )

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (1, "", "Connection refused")

            result = await provider.check_health(health_check)
            assert result is False

    @pytest.mark.asyncio
    async def test_down_removes_containers(self):
        """Test down() runs docker compose down to remove containers and networks."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            result = await provider.down()
            assert result is True
            cmd = mock_exec.call_args[0][0]
            assert "down" in cmd

    @pytest.mark.asyncio
    async def test_down_failure(self):
        """Test down() returns False on failure."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (1, "", "error stopping containers")

            result = await provider.down()
            assert result is False

    @pytest.mark.asyncio
    async def test_up_builds_and_starts_detached(self):
        """Test up() runs docker compose up -d --build."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            result = await provider.up()
            assert result is True
            cmd = mock_exec.call_args[0][0]
            assert "up" in cmd
            assert "-d" in cmd
            assert "--build" in cmd

    @pytest.mark.asyncio
    async def test_up_failure(self):
        """Test up() returns False on failure."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (1, "", "build failed")

            result = await provider.up()
            assert result is False

    @pytest.mark.asyncio
    async def test_status_returns_all_services(self):
        """Test status() returns status for all services in the stack."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        mock_output = json.dumps(
            [
                {"ID": "abc123", "Service": "web", "State": "running"},
                {"ID": "def456", "Service": "db", "State": "running"},
            ]
        )

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, mock_output, "")

            result = await provider.status()
            assert isinstance(result, list)
            assert len(result) == 2
            assert result[0]["Service"] == "web"

    def test_implements_deployment_provider_interface(self):
        """Test DockerComposeProvider implements DeploymentProvider."""
        from fraisier.providers import DockerComposeProvider
        from fraisier.providers.base import DeploymentProvider

        assert issubclass(DockerComposeProvider, DeploymentProvider)

        required_methods = [
            "connect",
            "disconnect",
            "execute_command",
            "upload_file",
            "download_file",
            "get_service_status",
            "check_health",
        ]
        for method in required_methods:
            assert hasattr(DockerComposeProvider, method), f"Missing method: {method}"

    @pytest.mark.asyncio
    async def test_compose_cmd_uses_project_and_file(self):
        """Test _compose_cmd builds correct command with project and file flags."""
        from fraisier.providers import DockerComposeProvider

        config = {
            "compose_file": "prod.yml",
            "project_name": "myapp",
        }
        provider = DockerComposeProvider(config)
        provider.compose_command = "docker compose"

        cmd = provider._compose_cmd("up -d")
        assert cmd == "docker compose -f prod.yml -p myapp up -d"

    @pytest.mark.asyncio
    async def test_exec_in_container_delegates_to_compose_exec(self):
        """Test exec_in_container runs command via docker compose exec."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)
        provider.compose_command = "docker compose"

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "Applied 3 migrations", "")

            exit_code, stdout, _stderr = await provider.exec_in_container(
                "app", "confiture migrate up -c confiture.yaml"
            )
            assert exit_code == 0
            assert "Applied 3 migrations" in stdout

            cmd = mock_exec.call_args[0][0]
            assert "exec" in cmd
            assert "app" in cmd
            assert "confiture migrate up" in cmd

    @pytest.mark.asyncio
    async def test_exec_in_container_captures_output(self):
        """Test exec_in_container captures both stdout and stderr."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (
                1,
                "Partial output",
                "ERROR: relation already exists",
            )

            exit_code, stdout, stderr = await provider.exec_in_container(
                "app", "confiture migrate up"
            )
            assert exit_code == 1
            assert stdout == "Partial output"
            assert "already exists" in stderr

    @pytest.mark.asyncio
    async def test_exec_in_container_uses_compose_flags(self):
        """Test exec_in_container includes -f and -p compose flags."""
        from fraisier.providers import DockerComposeProvider

        config = {
            "compose_file": "prod.yml",
            "project_name": "myapp",
        }
        provider = DockerComposeProvider(config)
        provider.compose_command = "docker compose"

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            await provider.exec_in_container("web", "ls /app")
            cmd = mock_exec.call_args[0][0]
            assert cmd.startswith("docker compose -f prod.yml -p myapp exec")

    @pytest.mark.asyncio
    async def test_exec_in_container_no_tty(self):
        """Test exec_in_container passes -T flag (no TTY) for non-interactive use."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            await provider.exec_in_container("app", "whoami")
            cmd = mock_exec.call_args[0][0]
            assert "-T" in cmd

    @pytest.mark.asyncio
    async def test_exec_in_container_timeout(self):
        """Test exec_in_container respects timeout parameter."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            await provider.exec_in_container("app", "confiture migrate up", timeout=60)
            assert mock_exec.call_args[1].get("timeout") == 60

    @pytest.mark.asyncio
    async def test_run_migration_in_container(self):
        """Test run_migration convenience method wraps exec_in_container."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "exec_in_container") as mock_exec:
            mock_exec.return_value = (0, "Applied 2 migrations", "")

            result = await provider.run_migration(
                service="app",
                config_path="confiture.yaml",
                direction="up",
            )
            assert result.success is True
            assert result.migration_count == 2
            mock_exec.assert_called_once()
            cmd = mock_exec.call_args[0][1]
            assert "confiture migrate up" in cmd
            assert "-c confiture.yaml" in cmd

    @pytest.mark.asyncio
    async def test_run_migration_failure(self):
        """Test run_migration returns failure result on error."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "exec_in_container") as mock_exec:
            mock_exec.return_value = (
                1,
                "",
                "ERROR: relation 'users' already exists",
            )

            result = await provider.run_migration(
                service="app",
                config_path="confiture.yaml",
            )
            assert result.success is False
            assert result.exit_code == 1
            assert "already exists" in result.error

    @pytest.mark.asyncio
    async def test_run_migration_down(self):
        """Test run_migration supports rollback direction."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "exec_in_container") as mock_exec:
            mock_exec.return_value = (0, "Rolled back 1 migration", "")

            result = await provider.run_migration(
                service="app",
                config_path="confiture.yaml",
                direction="down",
            )
            assert result.success is True
            assert result.migration_count == 1
            cmd = mock_exec.call_args[0][1]
            assert "confiture migrate down" in cmd

    def test_deploy_tag_records_current_and_previous(self):
        """Test deploy_tag stores current tag and shifts previous."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        assert provider.current_tag is None
        assert provider.previous_tag is None

        provider.deploy_tag("v1.0.0")
        assert provider.current_tag == "v1.0.0"
        assert provider.previous_tag is None

        provider.deploy_tag("v1.1.0")
        assert provider.current_tag == "v1.1.0"
        assert provider.previous_tag == "v1.0.0"

        provider.deploy_tag("v1.2.0")
        assert provider.current_tag == "v1.2.0"
        assert provider.previous_tag == "v1.1.0"

    @pytest.mark.asyncio
    async def test_rollback_restores_previous_tag(self):
        """Test rollback pulls and restarts with the previous image tag."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)
        provider.current_tag = "v1.1.0"
        provider.previous_tag = "v1.0.0"

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            result = await provider.rollback()
            assert result is True
            assert provider.current_tag == "v1.0.0"

    @pytest.mark.asyncio
    async def test_rollback_fails_without_previous_tag(self):
        """Test rollback fails when no previous tag is recorded."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)
        provider.current_tag = "v1.0.0"
        provider.previous_tag = None

        result = await provider.rollback()
        assert result is False

    @pytest.mark.asyncio
    async def test_rollback_sets_image_tag_env(self):
        """Test rollback passes IMAGE_TAG env to compose up."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)
        provider.current_tag = "v2.0.0"
        provider.previous_tag = "v1.9.0"

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            await provider.rollback()
            call_kwargs = mock_exec.call_args
            assert call_kwargs.kwargs.get("env") == {"IMAGE_TAG": "v1.9.0"}

    @pytest.mark.asyncio
    async def test_deploy_with_tag_passes_env(self):
        """Test deploy_with_tag runs up with IMAGE_TAG env var."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            result = await provider.deploy_with_tag("v3.0.0")
            assert result is True
            assert provider.current_tag == "v3.0.0"
            call_kwargs = mock_exec.call_args
            assert call_kwargs.kwargs.get("env") == {"IMAGE_TAG": "v3.0.0"}
            cmd = call_kwargs[0][0]
            assert "up" in cmd
            assert "--build" in cmd

    @pytest.mark.asyncio
    async def test_deploy_with_tag_tracks_previous(self):
        """Test deploy_with_tag shifts previous tag correctly."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)
        provider.current_tag = "v1.0.0"

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            await provider.deploy_with_tag("v2.0.0")
            assert provider.previous_tag == "v1.0.0"
            assert provider.current_tag == "v2.0.0"

    @pytest.mark.asyncio
    async def test_deploy_with_tag_failure_does_not_update_tags(self):
        """Test deploy_with_tag does not update tags on failure."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)
        provider.current_tag = "v1.0.0"

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (1, "", "build failed")

            result = await provider.deploy_with_tag("v2.0.0")
            assert result is False
            assert provider.current_tag == "v1.0.0"
            assert provider.previous_tag is None

    @pytest.mark.asyncio
    async def test_rollback_command_failure(self):
        """Test rollback returns False when compose command fails."""
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        provider = DockerComposeProvider(config)
        provider.current_tag = "v2.0.0"
        provider.previous_tag = "v1.0.0"

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (1, "", "error")

            result = await provider.rollback()
            assert result is False
            # Tags should not change on failure
            assert provider.current_tag == "v2.0.0"
            assert provider.previous_tag == "v1.0.0"


class TestDockerComposeWorkflow:
    """Integration tests: full deploy workflow with DockerComposeProvider."""

    def _make_provider(self):
        from fraisier.providers import DockerComposeProvider

        config = {"compose_file": "docker-compose.yml", "project_name": "test"}
        return DockerComposeProvider(config)

    @pytest.mark.asyncio
    async def test_full_migrate_deploy_health_workflow(self):
        """Test backup → migrate (in container) → deploy → health check."""
        provider = self._make_provider()

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "Applied 2 migrations", "")

            # Step 1: Run migration in container
            migrate_result = await provider.run_migration(
                service="app", config_path="confiture.yaml"
            )
            assert migrate_result.success is True
            assert migrate_result.migration_count == 2

            # Step 2: Deploy with tag
            result = await provider.deploy_with_tag("v1.0.0")
            assert result is True
            assert provider.current_tag == "v1.0.0"

            # Step 3: Health check
            health_check = HealthCheck(
                type=HealthCheckType.TCP,
                port=8000,
                timeout=5,
                retries=1,
            )

        with patch("asyncio.open_connection") as mock_connect:
            mock_writer = AsyncMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_connect.return_value = (AsyncMock(), mock_writer)

            healthy = await provider.check_health(health_check)
            assert healthy is True

    @pytest.mark.asyncio
    async def test_migrate_failure_triggers_rollback(self):
        """Test migration failure → rollback to previous tag."""
        provider = self._make_provider()
        provider.current_tag = "v1.0.0"
        provider.previous_tag = None

        # Deploy v2.0.0 first
        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")
            await provider.deploy_with_tag("v2.0.0")

        assert provider.current_tag == "v2.0.0"
        assert provider.previous_tag == "v1.0.0"

        # Migration fails
        with patch.object(provider, "exec_in_container") as mock_exec:
            mock_exec.return_value = (1, "", "ERROR: relation exists")

            migrate_result = await provider.run_migration(service="app")
            assert migrate_result.success is False

        # Rollback
        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            rolled_back = await provider.rollback()
            assert rolled_back is True
            assert provider.current_tag == "v1.0.0"

    @pytest.mark.asyncio
    async def test_health_check_failure_triggers_rollback(self):
        """Test deploy → health check fails → rollback."""
        provider = self._make_provider()

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")
            await provider.deploy_with_tag("v1.0.0")
            await provider.deploy_with_tag("v2.0.0")

        assert provider.current_tag == "v2.0.0"
        assert provider.previous_tag == "v1.0.0"

        # Health check fails
        health_check = HealthCheck(
            type=HealthCheckType.TCP,
            port=8000,
            timeout=1,
            retries=1,
        )
        with patch("asyncio.open_connection", side_effect=ConnectionRefusedError):
            healthy = await provider.check_health(health_check)
            assert healthy is False

        # Rollback after health failure
        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            rolled_back = await provider.rollback()
            assert rolled_back is True
            assert provider.current_tag == "v1.0.0"

    @pytest.mark.asyncio
    async def test_provider_agnostic_interface(self):
        """Test DockerCompose and BareMetalProvider share the same interface."""
        from fraisier.providers import DockerComposeProvider
        from fraisier.providers.base import DeploymentProvider

        shared_methods = [
            "connect",
            "disconnect",
            "execute_command",
            "upload_file",
            "download_file",
            "get_service_status",
            "check_health",
            "pre_flight_check",
        ]

        for method in shared_methods:
            assert hasattr(BareMetalProvider, method), (
                f"BareMetalProvider missing: {method}"
            )
            assert hasattr(DockerComposeProvider, method), (
                f"DockerComposeProvider missing: {method}"
            )

        # Both are DeploymentProvider subclasses
        assert issubclass(BareMetalProvider, DeploymentProvider)
        assert issubclass(DockerComposeProvider, DeploymentProvider)

    @pytest.mark.asyncio
    async def test_full_workflow_with_down_and_up(self):
        """Test down → migrate → up cycle."""
        provider = self._make_provider()

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            # Take stack down for maintenance
            assert await provider.down() is True

        with patch.object(provider, "exec_in_container") as mock_exec:
            mock_exec.return_value = (0, "Applied 1 migration", "")

            # Run migration while stack is down (exec still works on
            # stopped containers with docker compose run)
            result = await provider.run_migration(service="app")
            assert result.success is True

        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")

            # Bring stack back up
            assert await provider.up() is True

    @pytest.mark.asyncio
    async def test_rollback_after_failed_deploy(self):
        """Test deploy failure does not shift tags, then rollback works."""
        provider = self._make_provider()

        # Successful v1 deploy
        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (0, "", "")
            await provider.deploy_with_tag("v1.0.0")

        # Failed v2 deploy — tags should NOT change
        with patch.object(provider, "execute_command") as mock_exec:
            mock_exec.return_value = (1, "", "build error")
            result = await provider.deploy_with_tag("v2.0.0")
            assert result is False

        assert provider.current_tag == "v1.0.0"
        assert provider.previous_tag is None

        # No rollback possible (no previous tag)
        result = await provider.rollback()
        assert result is False
