"""Bundle subcommand - direct integration with modelops-bundle CLI."""

try:
    # Import the real Typer app directly from modelops-bundle
    from modelops_bundle.cli import app

except ImportError:
    # Graceful fallback if modelops-bundle not installed
    import typer

    from .display import console, error

    app = typer.Typer(
        name="bundle",
        help="Bundle packaging and registry management (requires modelops-bundle)",
        no_args_is_help=True,
    )

    @app.callback(invoke_without_command=True)
    def bundle_not_installed():
        """Show helpful error if modelops-bundle is missing."""
        error("modelops-bundle is not installed!")
        console.print("\n[yellow]To use bundle commands, install:[/yellow]")
        console.print("  uv pip install 'modelops[full]'")
        console.print("  # or")
        console.print("  uv pip install modelops-bundle")
        raise typer.Exit(1)
