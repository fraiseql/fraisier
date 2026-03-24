"""Tests for service name validation in DockerComposeProvider."""

import pytest


@pytest.fixture
def compose_provider():
    """Create a DockerComposeProvider instance."""
    from fraisier.providers.docker_compose.provider import DockerComposeProvider

    config = {
        "compose_file": "/tmp/docker-compose.yml",
        "project_name": "test",
    }
    return DockerComposeProvider(config)


INJECTION_NAMES = [
    "web; rm -rf /",
    "api$(whoami)",
    "svc`id`",
    "name|cat /etc/passwd",
    "a&&b",
    "../escape",
]

METHODS_WITH_SERVICE_NAME = [
    "get_service_status",
    "start_service",
    "stop_service",
    "restart_service",
    "pull_image",
    "get_container_logs",
    "get_service_env",
    "scale_service",
]


class TestServiceNameValidation:
    @pytest.mark.parametrize("method_name", METHODS_WITH_SERVICE_NAME)
    @pytest.mark.parametrize("bad_name", INJECTION_NAMES)
    async def test_rejects_injection_attempt(
        self, compose_provider, method_name, bad_name
    ):
        method = getattr(compose_provider, method_name)
        with pytest.raises(ValueError, match="Invalid service name"):
            if method_name == "scale_service":
                await method(bad_name, 2)
            else:
                await method(bad_name)

    @pytest.mark.parametrize("method_name", METHODS_WITH_SERVICE_NAME)
    async def test_accepts_valid_service_name(
        self, compose_provider, method_name, monkeypatch
    ):
        """Valid names should not raise ValueError (may fail on other things)."""
        method = getattr(compose_provider, method_name)

        # Patch execute_command to avoid actually running docker
        async def fake_exec(cmd):
            return (0, "{}", "")

        monkeypatch.setattr(compose_provider, "execute_command", fake_exec)

        # These should not raise ValueError
        try:
            if method_name == "scale_service":
                await method("valid-web_service.1", 2)
            else:
                await method("valid-web_service.1")
        except ValueError:
            pytest.fail(f"{method_name} raised ValueError for valid service name")
        except Exception:
            pass  # Other errors (json parse, etc.) are fine
