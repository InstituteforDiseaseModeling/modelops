"""ModelOps CLI entry point."""

# Set environment variables before any other imports to suppress gRPC warnings
import os
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

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
from . import infra, cluster, workspace, adaptive, registry, storage, config as config_cli, cleanup, status, results, jobs

# Register sub-commands
# Primary commands for researchers
app.add_typer(
    infra.app,
    name="infra",
    help="🚀 Infrastructure management - setup, status, teardown (RECOMMENDED)"
)

app.add_typer(
    adaptive.app,
    name="run",
    help="🔬 Run experiments and simulations"
)

app.add_typer(
    results.app,
    name="results",
    help="📊 View and manage experiment results"
)

# Advanced component-specific commands (for power users)
app.add_typer(
    cluster.app,
    name="cluster",
    help="[dim]Manage Kubernetes clusters (advanced)[/dim]",
    hidden=False  # Still visible but marked as advanced
)

app.add_typer(
    registry.app,
    name="registry",
    help="[dim]Manage container registries (advanced)[/dim]",
    hidden=False
)

app.add_typer(
    storage.app,
    name="storage",
    help="[dim]Manage blob storage (advanced)[/dim]",
    hidden=False
)

app.add_typer(
    workspace.app,
    name="workspace",
    help="[dim]Manage Dask workspaces (advanced)[/dim]",
    hidden=False
)

# Keep adaptive available under its original name for backwards compatibility
app.add_typer(
    adaptive.app,
    name="adaptive",
    help="[dim]Manage adaptive optimization runs (alias: run)[/dim]",
    hidden=False
)

# Utility commands
app.add_typer(
    config_cli.app,
    name="config",
    help="⚙️ Configure ModelOps settings"
)

app.add_typer(
    cleanup.app,
    name="cleanup",
    help="[dim]Clean up Pulumi state and resources (advanced)[/dim]",
    hidden=False
)

app.add_typer(
    status.app,
    name="status",
    help="[dim]Show infrastructure status (use 'mops infra status' instead)[/dim]",
    hidden=False
)

app.add_typer(
    jobs.app,
    name="jobs",
    help="[dim]Submit and manage simulation jobs (advanced)[/dim]",
    hidden=False
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
