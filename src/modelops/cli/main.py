"""ModelOps CLI entry point."""

import typer
from pathlib import Path
from .display import console, success, warning, error, info, section, info_dict

# Create main CLI app
app = typer.Typer(
    name="mops",
    help="ModelOps infrastructure orchestration for simulation-based methods",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich"
)

# Import sub-commands
from . import infra, workspace, adaptive, registry, storage, config as config_cli, cleanup, status

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
    storage.app,
    name="storage",
    help="Manage blob storage for bundles and results"
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

app.add_typer(
    cleanup.app,
    name="cleanup",
    help="Clean up Pulumi state and resources"
)

app.add_typer(
    status.app,
    name="status",
    help="Show comprehensive infrastructure status"
)


@app.command()
def version():
    """Show ModelOps version."""
    from .. import __version__
    info(f"ModelOps version: {__version__}")


def main():
    """Main CLI entry point."""
    try:
        app()
    except KeyboardInterrupt:
        warning("\nInterrupted by user")
        raise typer.Exit(1)
    except Exception as e:
        error(f"Error: {e}")
        raise typer.Exit(1)


if __name__ == "__main__":
    main()
