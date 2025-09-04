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
    info(f"ModelOps version: {__version__}")


@app.command()
def status():
    """Show overall ModelOps status."""
    from ..core.config import ModelOpsConfig, ConfigNotFoundError
    from ..core.paths import CONFIG_FILE, MODELOPS_HOME
    
    section("ModelOps Status")
    info_dict({
        "Config file": f"{CONFIG_FILE} {'✓' if CONFIG_FILE.exists() else '✗'}",
        "Home directory": f"{MODELOPS_HOME} {'✓' if MODELOPS_HOME.exists() else '✗'}"
    })
    
    # Try to load config, but handle missing config gracefully
    try:
        config_obj = ModelOpsConfig.get_instance()
        info_dict({
            "Default environment": config_obj.defaults.environment,
            "Default provider": config_obj.defaults.provider
        })
    except ConfigNotFoundError:
        warning("  Configuration: Not initialized")
        warning("\nRun 'mops config init' to create configuration")
        raise typer.Exit(0)
    
    providers_dir = MODELOPS_HOME / "providers"
    if providers_dir.exists():
        providers = list(providers_dir.glob("*.yaml"))
        if providers:
            section("Configured providers")
            for p in providers:
                info(f"  - {p.stem}")
        else:
            warning("\nNo providers configured yet.")
    
    info("\nUse 'mops config show' to see full configuration")


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
