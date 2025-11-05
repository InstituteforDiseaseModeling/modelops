"""Configuration management CLI commands."""

import typer
from rich.syntax import Syntax

from ..core.config import ModelOpsConfig
from ..core.paths import CONFIG_FILE
from .display import console, error, info, info_dict, section, success, warning

app = typer.Typer(help="Manage ModelOps configuration")


@app.command(hidden=True)  # Hidden: use 'mops init' instead
def init(
    interactive: bool = typer.Option(
        False, "--interactive/--no-interactive", help="Interactive mode with prompts"
    ),
):
    """Initialize configuration file.

    Creates ~/.modelops/config.yaml with sensible defaults.
    Uses non-interactive mode by default for automation.
    """
    import getpass

    # Start with sensible defaults
    config = ModelOpsConfig()
    config.pulumi.organization = "institutefordiseasemodeling"
    config.defaults.environment = "dev"
    config.defaults.provider = "azure"
    # Always set username - default to system user
    config.defaults.username = getpass.getuser()

    if interactive:
        # Prompt for Pulumi settings
        section("Pulumi Configuration")
        backend = typer.prompt("  Backend URL (optional)", default="", show_default=False)
        org = typer.prompt("  Organization name", default=config.pulumi.organization)

        # Prompt for defaults
        section("Default Settings")
        env = typer.prompt("  Default environment", default=config.defaults.environment)
        provider = typer.prompt("  Default provider", default=config.defaults.provider)
        username = typer.prompt(
            "  Username (for resource naming)",
            default=getpass.getuser(),
            show_default=True,
        )

        # Update config
        if backend:
            config.pulumi.backend_url = backend
        config.pulumi.organization = org
        config.defaults.environment = env
        config.defaults.provider = provider
        # Always set username (from prompt or default)
        config.defaults.username = username

    # Check if file exists
    if CONFIG_FILE.exists():
        overwrite = typer.confirm(f"\n{CONFIG_FILE} already exists. Overwrite?", default=False)
        if not overwrite:
            warning("Configuration not saved")
            raise typer.Exit(0)

    config.save()

    # Reset the cached instance since we created a new config
    ModelOpsConfig.reset()

    success(f"\n✓ Configuration saved to {CONFIG_FILE}")

    if not interactive:
        info("Use 'mops config set' to customize values")


@app.command()
def show():
    """Display current configuration."""
    from .utils import get_config_or_exit

    config = get_config_or_exit("config show")

    # Display as formatted YAML with syntax highlighting
    yaml_content = config.to_yaml_string()
    syntax = Syntax(yaml_content, "yaml", theme="monokai", line_numbers=False)

    section(f"Configuration from {CONFIG_FILE}")
    console.print(syntax)


@app.command()
def set(
    key: str = typer.Argument(
        ..., help="Configuration key (e.g., pulumi.backend_url, defaults.environment)"
    ),
    value: str = typer.Argument(..., help="Value to set"),
):
    """Set a configuration value.

    Examples:
        mops config set pulumi.backend_url azblob://my-container
        mops config set pulumi.organization myorg
        mops config set defaults.environment prod
        mops config set defaults.username alice
    """
    from .utils import get_config_or_exit

    config = get_config_or_exit("config set")

    # Parse the key path
    parts = key.split(".")
    if len(parts) != 2:
        error(f"Invalid key format: {key}")
        info("Use format: section.field (e.g., pulumi.backend_url)")
        raise typer.Exit(1)

    section_name, field = parts

    # Validate section
    if not hasattr(config, section_name):
        error(f"Unknown configuration section: {section_name}")
        info("Valid sections: pulumi, defaults")
        raise typer.Exit(1)

    # Validate field
    section_obj = getattr(config, section_name)
    if not hasattr(section_obj, field):
        error(f"Unknown field '{field}' in section '{section_name}'")
        valid_fields = [f for f in section_obj.model_fields.keys()]
        info(f"Valid fields: {', '.join(valid_fields)}")
        raise typer.Exit(1)

    # Handle special cases
    if (
        field == "backend_url"
        and value.lower() in ["none", "null", ""]
        or field == "username"
        and value.lower() in ["none", "null", ""]
    ):
        value = None

    # Set the value
    setattr(section_obj, field, value)
    config.save()

    # Reset the cached instance since we modified it
    ModelOpsConfig.reset()

    success(f"✓ Set {key} = {value}")


@app.command(name="list")
def list_settings():
    """List all configuration settings and their current values."""
    from .utils import get_config_or_exit

    config = get_config_or_exit("config list")

    section("Current Configuration")

    # Pulumi settings
    info("\nPulumi Settings:")
    backend = config.pulumi.backend_url or "<default local>"
    info_dict(
        {"backend_url": backend, "organization": config.pulumi.organization},
        indent="  ",
    )

    # Default settings
    info("\nDefault Settings:")
    username = config.defaults.username or "<system user>"
    info_dict(
        {
            "environment": config.defaults.environment,
            "provider": config.defaults.provider,
            "username": username,
        },
        indent="  ",
    )

    info(f"\nConfig file: {CONFIG_FILE}")
