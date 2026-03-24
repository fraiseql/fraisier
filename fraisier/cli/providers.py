"""Provider management commands."""

from __future__ import annotations

from pathlib import Path

import click
from rich.table import Table

from ._helpers import console
from .main import main


@main.command(name="providers")
@click.pass_context
def providers(_ctx: click.Context) -> None:
    """List all available deployment providers."""
    from fraisier.providers import ProviderRegistry
    from fraisier.providers.bare_metal import BareMetalProvider
    from fraisier.providers.docker_compose import DockerComposeProvider

    # Register built-in providers
    if not ProviderRegistry.is_registered("bare_metal"):
        ProviderRegistry.register(BareMetalProvider)
    if not ProviderRegistry.is_registered("docker_compose"):
        ProviderRegistry.register(DockerComposeProvider)

    providers_list = ProviderRegistry.list_providers()

    if not providers_list:
        console.print("[yellow]No providers registered[/yellow]")
        return

    table = Table(title="Available Deployment Providers")
    table.add_column("Provider Type", style="cyan")
    table.add_column("Description", style="white")

    provider_descriptions = {
        "bare_metal": "SSH/systemd deployments to bare metal servers",
        "docker_compose": "Docker Compose based containerized deployments",
    }

    for provider_type in providers_list:
        description = provider_descriptions.get(provider_type, "Custom provider")
        table.add_row(provider_type, description)

    console.print(table)


@main.command(name="provider-info")
@click.argument("provider_type")
@click.pass_context
def provider_info(_ctx: click.Context, provider_type: str) -> None:
    """Show detailed information about a provider type."""
    from fraisier.providers import ProviderRegistry
    from fraisier.providers.bare_metal import BareMetalProvider
    from fraisier.providers.docker_compose import DockerComposeProvider

    # Register built-in providers
    if not ProviderRegistry.is_registered("bare_metal"):
        ProviderRegistry.register(BareMetalProvider)
    if not ProviderRegistry.is_registered("docker_compose"):
        ProviderRegistry.register(DockerComposeProvider)

    if not ProviderRegistry.is_registered(provider_type):
        console.print(f"[red]Error:[/red] Unknown provider type '{provider_type}'")
        available = ", ".join(ProviderRegistry.list_providers())
        console.print(f"Available providers: {available}")
        raise SystemExit(1)

    provider_info_map = {
        "bare_metal": {
            "name": "Bare Metal",
            "description": "Deploy to bare metal servers via SSH and systemd",
            "config_fields": [
                "url: SSH host (e.g., 'prod.example.com')",
                "ssh_user: SSH username (default: 'deploy')",
                "ssh_key_path: Path to SSH private key",
                "app_path: Application path on remote (e.g., '/var/app')",
                "systemd_service: Systemd service name (e.g., 'api.service')",
                "health_check_type: 'http', 'tcp', or 'none'",
                "health_check_url: HTTP endpoint (if http type)",
                "health_check_port: TCP port (if tcp type)",
            ],
        },
        "docker_compose": {
            "name": "Docker Compose",
            "description": "Deploy services using Docker Compose",
            "config_fields": [
                "url: Path to docker-compose directory",
                "compose_file: Path to docker-compose.yml "
                "(default: 'docker-compose.yml')",
                "service_name: Service name in compose file",
                "health_check_type: 'http', 'tcp', 'exec', 'status', or 'none'",
                "health_check_url: HTTP endpoint (if http type)",
                "health_check_port: TCP port (if tcp type)",
                "health_check_exec: Command to execute (if exec type)",
            ],
        },
    }

    if provider_type not in provider_info_map:
        info = {
            "name": provider_type.replace("_", " ").title(),
            "description": "Custom provider",
            "config_fields": ["(See provider documentation)"],
        }
    else:
        info = provider_info_map[provider_type]

    console.print(f"\n[bold cyan]{info['name']} Provider[/bold cyan]")
    console.print(f"[white]{info['description']}[/white]\n")

    console.print("[bold]Configuration fields:[/bold]")
    for field in info["config_fields"]:
        console.print(f"  \u2022 {field}")
    console.print()


@main.command(name="provider-test")
@click.argument("provider_type")
@click.option(
    "--config-file",
    "-f",
    type=click.Path(exists=True),
    help="Provider configuration file (YAML)",
)
@click.pass_context
def provider_test(
    _ctx: click.Context, provider_type: str, config_file: str | None
) -> None:
    """Run pre-flight checks for a provider."""
    import yaml

    from fraisier.providers import ProviderConfig, ProviderRegistry
    from fraisier.providers.bare_metal import BareMetalProvider
    from fraisier.providers.docker_compose import DockerComposeProvider

    # Register built-in providers
    if not ProviderRegistry.is_registered("bare_metal"):
        ProviderRegistry.register(BareMetalProvider)
    if not ProviderRegistry.is_registered("docker_compose"):
        ProviderRegistry.register(DockerComposeProvider)

    if not ProviderRegistry.is_registered(provider_type):
        console.print(f"[red]Error:[/red] Unknown provider type '{provider_type}'")
        raise SystemExit(1)

    # Load provider config if file provided
    if config_file:
        try:
            with Path(config_file).open() as f:
                config_data = yaml.safe_load(f)
        except Exception as e:
            console.print(f"[red]Error loading config file:[/red] {e}")
            raise SystemExit(1) from e

        if not isinstance(config_data, dict):
            console.print("[red]Error:[/red] Config file must contain a YAML object")
            raise SystemExit(1)

        # Create provider config from file
        try:
            provider_config = ProviderConfig(
                name=config_data.get("name", "test"),
                type=provider_type,
                url=config_data.get("url", ""),
                api_key=config_data.get("api_key"),
                custom_fields=config_data.get("custom_fields", {}),
            )
        except Exception as e:
            console.print(f"[red]Error creating provider config:[/red] {e}")
            raise SystemExit(1) from e
    else:
        # Create minimal test config
        provider_config = ProviderConfig(
            name="test",
            type=provider_type,
            url="localhost",
            custom_fields={},
        )

    # Create provider and run pre-flight check
    try:
        provider = ProviderRegistry.get_provider(provider_type, provider_config)
        console.print(f"[cyan]Testing {provider_type} provider...[/cyan]")
        success, message = provider.pre_flight_check()

        if success:
            console.print("[green]\u2713 Pre-flight check passed[/green]")
            console.print(f"[dim]{message}[/dim]")
        else:
            console.print("[red]\u2717 Pre-flight check failed[/red]")
            console.print(f"[dim]{message}[/dim]")
            raise SystemExit(1)

    except Exception as e:
        console.print(f"[red]Error running pre-flight check:[/red] {e}")
        raise SystemExit(1) from e
