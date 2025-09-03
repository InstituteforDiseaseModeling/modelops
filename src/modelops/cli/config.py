"""Configuration management CLI commands."""

import typer
from pathlib import Path
from rich.console import Console
from rich.syntax import Syntax
from ..core.config import ModelOpsConfig
from ..core.paths import CONFIG_FILE

app = typer.Typer(help="Manage ModelOps configuration")
console = Console()


@app.command()
def init(
    interactive: bool = typer.Option(
        True,
        "--interactive/--no-interactive",
        help="Interactive mode with prompts"
    )
):
    """Initialize configuration file.
    
    Creates ~/.modelops/config.yaml with default values or prompts for them.
    """
    config = ModelOpsConfig()
    
    if interactive:
        # Prompt for Pulumi settings
        console.print("[bold]Pulumi Configuration[/bold]")
        backend = typer.prompt(
            "  Backend URL (optional)",
            default="",
            show_default=False
        )
        
        # Prompt for defaults
        console.print("\n[bold]Default Settings[/bold]")
        env = typer.prompt(
            "  Default environment",
            default=config.defaults.environment
        )
        provider = typer.prompt(
            "  Default provider",
            default=config.defaults.provider
        )
        username = typer.prompt(
            "  Username override (optional)",
            default="",
            show_default=False
        )
        
        # Update config
        if backend:
            config.pulumi.backend_url = backend
        config.defaults.environment = env
        config.defaults.provider = provider
        if username:
            config.defaults.username = username
    
    # Check if file exists
    if CONFIG_FILE.exists():
        overwrite = typer.confirm(
            f"\n{CONFIG_FILE} already exists. Overwrite?",
            default=False
        )
        if not overwrite:
            console.print("[yellow]Configuration not saved[/yellow]")
            raise typer.Exit(0)
    
    config.save()
    
    # Reset the cached instance since we created a new config
    ModelOpsConfig.reset()
    
    console.print(f"\n[green]✓ Configuration saved to {CONFIG_FILE}[/green]")
    
    if not interactive:
        console.print("[dim]Use 'mops config set' to customize values[/dim]")


@app.command()
def show():
    """Display current configuration."""
    from .utils import get_config_or_exit
    
    config = get_config_or_exit("config show")
    
    # Display as formatted YAML with syntax highlighting
    yaml_content = config.to_yaml_string()
    syntax = Syntax(yaml_content, "yaml", theme="monokai", line_numbers=False)
    
    console.print(f"\n[bold]Configuration from {CONFIG_FILE}:[/bold]\n")
    console.print(syntax)


@app.command()
def set(
    key: str = typer.Argument(
        ...,
        help="Configuration key (e.g., pulumi.backend_url, defaults.environment)"
    ),
    value: str = typer.Argument(
        ...,
        help="Value to set"
    )
):
    """Set a configuration value.
    
    Examples:
        mops config set pulumi.backend_url s3://my-bucket
        mops config set defaults.environment prod
        mops config set defaults.username alice
    """
    from .utils import get_config_or_exit
    
    config = get_config_or_exit("config set")
    
    # Parse the key path
    parts = key.split(".")
    if len(parts) != 2:
        console.print(f"[red]Invalid key format: {key}[/red]")
        console.print("Use format: section.field (e.g., pulumi.backend_url)")
        raise typer.Exit(1)
    
    section, field = parts
    
    # Validate section
    if not hasattr(config, section):
        console.print(f"[red]Unknown configuration section: {section}[/red]")
        console.print(f"Valid sections: pulumi, defaults")
        raise typer.Exit(1)
    
    # Validate field
    section_obj = getattr(config, section)
    if not hasattr(section_obj, field):
        console.print(f"[red]Unknown field '{field}' in section '{section}'[/red]")
        valid_fields = [f for f in section_obj.model_fields.keys()]
        console.print(f"Valid fields: {', '.join(valid_fields)}")
        raise typer.Exit(1)
    
    # Handle special cases
    if field == "backend_url" and value.lower() in ["none", "null", ""]:
        value = None
    elif field == "username" and value.lower() in ["none", "null", ""]:
        value = None
    
    # Set the value
    setattr(section_obj, field, value)
    config.save()
    
    # Reset the cached instance since we modified it
    ModelOpsConfig.reset()
    
    console.print(f"[green]✓ Set {key} = {value}[/green]")


@app.command(name="list")
def list_settings():
    """List all configuration settings and their current values."""
    from .utils import get_config_or_exit
    
    config = get_config_or_exit("config list")
    
    console.print("\n[bold]Current Configuration:[/bold]\n")
    
    # Pulumi settings
    console.print("[cyan]Pulumi Settings:[/cyan]")
    backend = config.pulumi.backend_url or "[dim]<default local>[/dim]"
    console.print(f"  backend_url: {backend}")
    
    # Default settings
    console.print("\n[cyan]Default Settings:[/cyan]")
    console.print(f"  environment: {config.defaults.environment}")
    console.print(f"  provider: {config.defaults.provider}")
    username = config.defaults.username or "[dim]<system user>[/dim]"
    console.print(f"  username: {username}")
    
    console.print(f"\n[dim]Config file: {CONFIG_FILE}[/dim]")
