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
    )

    @app.callback(invoke_without_command=True)
    def bundle_not_installed(ctx: typer.Context):
        """Show helpful error if modelops-bundle is missing."""
        # Only show error if not asking for help
        if ctx.resilient_parsing:
            return

        error("modelops-bundle is not installed!")
        console.print("\n[yellow]To reinstall ModelOps with bundle support:[/yellow]")
        console.print("  curl -sSL https://raw.githubusercontent.com/institutefordiseasemodeling/modelops/main/install.sh | bash")
        console.print("\n[dim]Or for development:[/dim]")
        console.print("  cd <modelops-repo>")
        console.print("  uv sync --group bundle")
        raise typer.Exit(1)
