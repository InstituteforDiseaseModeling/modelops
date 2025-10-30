"""Bundle subcommand proxy for modelops-bundle integration.

This module provides a seamless interface to modelops-bundle commands
when the package is installed with the [full] extra.
"""

import subprocess
import sys
from typing import List, Optional
import typer
from pathlib import Path
from .display import console, error, warning

# Create the bundle app
app = typer.Typer(
    name="bundle",
    help="Bundle packaging and registry management (requires modelops-bundle)",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    invoke_without_command=False,
    # Important: This allows us to pass through unknown arguments
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)


def check_bundle_installed() -> bool:
    """Check if modelops-bundle is installed."""
    import shutil
    # First check if the command exists
    if shutil.which("modelops-bundle"):
        return True
    # Then check if we can import it
    try:
        import modelops_bundle
        return True
    except ImportError:
        return False


def get_bundle_command() -> Optional[str]:
    """Find the modelops-bundle command in PATH or virtual environment."""
    import shutil

    # Try to find modelops-bundle in PATH
    bundle_cmd = shutil.which("modelops-bundle")
    if bundle_cmd:
        return bundle_cmd

    # Try to find it in the same environment as this script
    venv_path = Path(sys.executable).parent / "modelops-bundle"
    if venv_path.exists():
        return str(venv_path)

    return None


@app.callback(invoke_without_command=True)
def bundle_proxy(ctx: typer.Context):
    """Proxy all bundle commands to modelops-bundle."""

    # Check if modelops-bundle is installed
    if not check_bundle_installed():
        error("modelops-bundle is not installed!")
        console.print("\n[yellow]To install the full ModelOps suite:[/yellow]")
        console.print("  curl -sSL https://raw.githubusercontent.com/institutefordiseasemodeling/modelops/main/install.sh | bash")
        console.print("\n[yellow]Or with uv tool:[/yellow]")
        console.print("  uv tool install 'modelops[full]@git+https://github.com/institutefordiseasemodeling/modelops.git'")
        console.print("\n[yellow]Or if developing ModelOps:[/yellow]")
        console.print("  uv pip install -e '.[full]'")
        raise typer.Exit(1)

    # Find the bundle command
    bundle_cmd = get_bundle_command()
    if not bundle_cmd:
        error("modelops-bundle is installed but command not found in PATH")
        raise typer.Exit(1)

    # Build the command with all arguments
    # We need to get all the arguments including the command
    import sys
    # Find where 'bundle' appears in sys.argv and take everything after it
    try:
        bundle_idx = sys.argv.index('bundle')
        args = sys.argv[bundle_idx + 1:]
    except ValueError:
        # Fallback to ctx.args
        args = ctx.args

    cmd = [bundle_cmd] + args

    # Run the actual modelops-bundle command
    try:
        result = subprocess.run(cmd, check=False)
        raise typer.Exit(result.returncode)
    except FileNotFoundError:
        error(f"Failed to execute: {bundle_cmd}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        # Pass through Ctrl+C gracefully
        raise typer.Exit(130)


# Add common bundle commands as documentation
# These won't actually run - they're just for help text
@app.command("init", add_help_option=False, hidden=True)
def init():
    """Initialize a bundle project in the current directory."""
    pass  # Never reached - handled by callback


@app.command("status", add_help_option=False, hidden=True)
def status():
    """Show bundle status and registered models."""
    pass  # Never reached - handled by callback


@app.command("push", add_help_option=False, hidden=True)
def push():
    """Push bundle to registry."""
    pass  # Never reached - handled by callback


@app.command("register-model", add_help_option=False, hidden=True)
def register_model():
    """Register a model for cloud execution."""
    pass  # Never reached - handled by callback


@app.command("register-target", add_help_option=False, hidden=True)
def register_target():
    """Register calibration targets."""
    pass  # Never reached - handled by callback