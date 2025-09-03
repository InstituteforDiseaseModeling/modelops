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
from . import infra, workspace, adaptive, registry

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


@app.command()
def version():
    """Show ModelOps version."""
    from .. import __version__
    console.print(f"ModelOps version: {__version__}")


@app.command()
def config():
    """Show configuration paths and status."""
    config_dir = Path.home() / ".modelops"
    providers_dir = config_dir / "providers"
    pulumi_dir = config_dir / "pulumi"
    
    console.print("[bold]ModelOps Configuration:[/bold]")
    console.print(f"  Config directory: {config_dir}")
    console.print(f"  Providers directory: {providers_dir} {'✓' if providers_dir.exists() else '✗'}")
    console.print(f"  Pulumi state directory: {pulumi_dir} {'✓' if pulumi_dir.exists() else '✗'}")
    
    if providers_dir.exists():
        providers = list(providers_dir.glob("*.yaml"))
        if providers:
            console.print("\n[bold]Configured providers:[/bold]")
            for p in providers:
                console.print(f"  - {p.stem}")
        else:
            console.print("\n[yellow]No providers configured yet.[/yellow]")
            console.print("Run 'mops provider init <provider>' to configure a provider.")


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
