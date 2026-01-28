"""ModelOps CLI entry point."""

# Set environment variables before any other imports to suppress gRPC warnings
import os

os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

from pathlib import Path

import typer

from .display import error, info, warning

# Create main CLI app
app = typer.Typer(
    name="mops",
    help="ModelOps infrastructure orchestration for simulation-based methods",
    invoke_without_command=True,
    add_completion=False,
    rich_markup_mode="rich",
)


@app.callback(invoke_without_command=True)
def _root(ctx: typer.Context):
    """Ensure top-level invocation shows help and exits with error."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        typer.echo("\nError: Missing command.", err=True)
        raise typer.Exit(1)

# Import sub-commands
from . import (
    adaptive,
    cleanup,
    cluster,
    dev,
    infra,
    jobs,
    registry,
    results,
    status,
    storage,
    workspace,
)
from . import (
    config as config_cli,
)
from . import (
    init as init_cli,
)

# Register sub-commands


# Top-level initialization
@app.command()
def init(
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help="Interactive mode with prompts for all settings",
    ),
    output: typer.FileTextWrite = typer.Option(
        None,
        "--output",
        "-o",
        help="Custom output path (default: ~/.modelops/modelops.yaml)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing configuration without prompting",
    ),
):
    """Initialize ModelOps with unified configuration.

    Creates a complete configuration file that combines all settings needed
    for ModelOps operation. By default uses smart defaults with minimal prompting.

    Examples:
        mops init                    # Quick setup with defaults
        mops init --interactive      # Customize all settings
        mops init --force           # Overwrite existing config
    """
    output_path = Path(output.name) if output else None
    init_cli.init(interactive=interactive, output=output_path, force=force)


# Primary commands for researchers
app.add_typer(
    infra.app,
    name="infra",
    help=" Infrastructure management - setup, status, teardown (RECOMMENDED)",
)

app.add_typer(adaptive.app, name="job", help=" Run simulation and calibration jobs")

app.add_typer(results.app, name="results", help=" View and manage experiment results")

# Advanced component-specific commands (for power users)
app.add_typer(
    cluster.app,
    name="cluster",
    help="[dim]Manage Kubernetes clusters (advanced)[/dim]",
    hidden=False,  # Still visible but marked as advanced
)

app.add_typer(
    registry.app,
    name="registry",
    help="[dim]Manage container registries (advanced)[/dim]",
    hidden=False,
)

app.add_typer(
    storage.app,
    name="storage",
    help="[dim]Manage blob storage (advanced)[/dim]",
    hidden=False,
)

app.add_typer(
    workspace.app,
    name="workspace",
    help="[dim]Manage Dask workspaces (advanced)[/dim]",
    hidden=False,
)

# Keep adaptive available under its original name for backwards compatibility
app.add_typer(
    adaptive.app,
    name="adaptive",
    help="[dim]Manage infrastructure for adaptive (e.g. calibration) jobs[/dim]",
    hidden=False,
)

# Utility commands
app.add_typer(config_cli.app, name="config", help=" Configure ModelOps settings")

# Developer tools
app.add_typer(dev.app, name="dev", help=" Developer tools and testing utilities")

app.add_typer(
    cleanup.app,
    name="cleanup",
    help="[dim]Clean up Pulumi state and resources (advanced)[/dim]",
    hidden=False,
)

app.add_typer(
    status.app,
    name="status",
    help="[dim]Show infrastructure status (use 'mops infra status' instead)[/dim]",
    hidden=False,
)

app.add_typer(
    jobs.app,
    name="jobs",
    help="[dim]Submit and manage simulation jobs (advanced)[/dim]",
    hidden=False,
)

# Conditionally add bundle subcommand if modelops-bundle is installed
try:
    from . import bundle

    app.add_typer(
        bundle.app,
        name="bundle",
        help="Bundle packaging and registry management",
        hidden=False,
    )
except ImportError:
    # modelops-bundle not installed, skip bundle commands
    pass


@app.command()
def version():
    """Show ModelOps version and build information."""
    from .. import get_version_info

    version_info = get_version_info()
    info(f"ModelOps version: {version_info['full']}")

    if version_info["git_hash"]:
        info(f"  Git commit: {version_info['git_hash']}")

    # Also show key dependency versions for debugging
    try:
        from importlib.metadata import version as pkg_version

        contracts_ver = pkg_version("modelops-contracts")
        info(f"  modelops-contracts: {contracts_ver}")
    except Exception:
        pass


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
