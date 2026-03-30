"""Test database template management commands."""

from __future__ import annotations

from pathlib import Path

import click

from ._helpers import console
from .main import main


@main.group(name="test-db")
def test_db() -> None:
    """Test database template management.

    \b
    Examples:
        fraisier test-db status --env test
        fraisier test-db rebuild --env test
        fraisier test-db clean --env test
    """


@test_db.command(name="status")
@click.option("--env", "-e", default="test", help="Confiture environment name")
@click.option(
    "--project-dir",
    "-d",
    type=click.Path(exists=True),
    default=".",
    help="Project root directory",
)
@click.option(
    "--connection-url",
    envvar="DATABASE_URL",
    help="PostgreSQL connection URL",
)
def test_db_status(env: str, project_dir: str, connection_url: str | None) -> None:
    """Show template database status."""
    from fraisier.testing._manager import TemplateManager

    project = Path(project_dir).resolve()
    manager = TemplateManager(
        env=env,
        project_dir=project,
        confiture_config=project / "confiture.yaml",
        connection_url=connection_url,
    )
    status = manager.status()

    console.print(f"[bold]Template:[/bold] {status.template_name}")
    console.print(f"[bold]Exists:[/bold]   {status.template_exists}")
    console.print(f"[bold]Current hash:[/bold] {status.current_hash[:16]}...")

    if status.stored_hash:
        console.print(f"[bold]Stored hash:[/bold]  {status.stored_hash[:16]}...")
    else:
        console.print("[bold]Stored hash:[/bold]  (none)")

    if status.built_at:
        console.print(f"[bold]Built at:[/bold]    {status.built_at}")
    if status.build_duration_ms is not None:
        console.print(f"[bold]Build time:[/bold]  {status.build_duration_ms} ms")

    if status.needs_rebuild:
        console.print("[yellow]Template needs rebuild[/yellow]")
    else:
        console.print("[green]Template is up to date[/green]")


@test_db.command(name="rebuild")
@click.option("--env", "-e", default="test", help="Confiture environment name")
@click.option(
    "--project-dir",
    "-d",
    type=click.Path(exists=True),
    default=".",
    help="Project root directory",
)
@click.option(
    "--connection-url",
    envvar="DATABASE_URL",
    help="PostgreSQL connection URL",
)
def test_db_rebuild(env: str, project_dir: str, connection_url: str | None) -> None:
    """Force rebuild the template database."""
    from fraisier.testing._manager import TemplateManager

    project = Path(project_dir).resolve()
    manager = TemplateManager(
        env=env,
        project_dir=project,
        confiture_config=project / "confiture.yaml",
        connection_url=connection_url,
    )

    console.print(f"Rebuilding template for env={env}...")
    info = manager.build_template()
    console.print(
        f"[green]Template {info.template_name} rebuilt "
        f"(hash {info.schema_hash[:16]}...)[/green]"
    )
    if info.timing:
        console.print(info.timing.summary())


@test_db.command(name="clean")
@click.option("--env", "-e", default="test", help="Confiture environment name")
@click.option(
    "--project-dir",
    "-d",
    type=click.Path(exists=True),
    default=".",
    help="Project root directory",
)
@click.option(
    "--connection-url",
    envvar="DATABASE_URL",
    help="PostgreSQL connection URL",
)
def test_db_clean(env: str, project_dir: str, connection_url: str | None) -> None:
    """Drop test database templates."""
    from fraisier.testing._manager import TemplateManager

    project = Path(project_dir).resolve()
    manager = TemplateManager(
        env=env,
        project_dir=project,
        confiture_config=project / "confiture.yaml",
        connection_url=connection_url,
    )

    dropped = manager.cleanup()
    if dropped:
        console.print(f"[green]Dropped {dropped} template(s)[/green]")
    else:
        console.print("No templates to clean up")
