"""ModelOps CLI entry point."""

import typer
from rich.console import Console
from pathlib import Path

# Create main CLI app
app = typer.Typer(
    name="mops",
    help="ModelOps infrastructure orchestration for simulation-based methods",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich"
)

console = Console()

# Import sub-commands
from . import infra, workspace, adaptive, registry, config as config_cli

# Register sub-commands
app.add_typer(
    infra.app,
    name="infra",
    help="Manage infrastructure (Azure, AWS, GCP, local)"
)

app.add_typer(
    registry.app,
    name="registry",
    help="Manage container registries"
)

app.add_typer(
    workspace.app,
    name="workspace",
    help="Manage Dask workspaces"
)

app.add_typer(
    adaptive.app,
    name="adaptive",
    help="Manage adaptive optimization runs"
)

app.add_typer(
    config_cli.app,
    name="config",
    help="Manage ModelOps configuration"
)


@app.command()
def version():
    """Show ModelOps version."""
    from .. import __version__
    console.print(f"ModelOps version: {__version__}")


@app.command()
def status():
    """Show overall ModelOps status."""
    from ..core.config import ModelOpsConfig, ConfigNotFoundError
    from ..core.paths import CONFIG_FILE, MODELOPS_HOME
    
    console.print("[bold]ModelOps Status:[/bold]")
    console.print(f"  Config file: {CONFIG_FILE} {'✓' if CONFIG_FILE.exists() else '✗'}")
    console.print(f"  Home directory: {MODELOPS_HOME} {'✓' if MODELOPS_HOME.exists() else '✗'}")
    
    # Try to load config, but handle missing config gracefully
    try:
        config_obj = ModelOpsConfig.get_instance()
        console.print(f"  Default environment: {config_obj.defaults.environment}")
        console.print(f"  Default provider: {config_obj.defaults.provider}")
    except ConfigNotFoundError:
        console.print("[yellow]  Configuration: Not initialized[/yellow]")
        console.print("\n[yellow]Run 'mops config init' to create configuration[/yellow]")
        raise typer.Exit(0)
    
    providers_dir = MODELOPS_HOME / "providers"
    if providers_dir.exists():
        providers = list(providers_dir.glob("*.yaml"))
        if providers:
            console.print("\n[bold]Configured providers:[/bold]")
            for p in providers:
                console.print(f"  - {p.stem}")
        else:
            console.print("\n[yellow]No providers configured yet.[/yellow]")
    
    console.print("\n[dim]Use 'mops config show' to see full configuration[/dim]")


def main():
    """Main CLI entry point."""
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    main()
