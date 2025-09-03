"""Container registry management CLI commands."""

import typer
import yaml
import pulumi.automation as auto
from pathlib import Path
from typing import Optional
from rich.console import Console
from ..core import StackNaming

app = typer.Typer(help="Manage container registries")
console = Console()


@app.command()
def create(
    name: str = typer.Option(
        "modelops-registry",
        "--name", "-n",
        help="Registry name"
    ),
    provider: str = typer.Option(
        "azure",
        "--provider", "-p",
        help="Registry provider (azure, dockerhub, ghcr)"
    ),
    config: Optional[Path] = typer.Option(
        None,
        "--config", "-c",
        help="Registry configuration file (YAML)"
    ),
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    )
):
    """Create a new container registry.
    
    This creates an independent registry stack that can be referenced
    by workspace and other stacks for pulling container images.
    
    Examples:
        mops registry create --name modelops --provider azure
        mops registry create --config registry.yaml
    """
    # Load configuration
    registry_config = {
        "provider": provider,
        "environment": env
    }
    
    if config and config.exists():
        with open(config) as f:
            loaded_config = yaml.safe_load(f)
            registry_config.update(loaded_config)
    
    # Add Azure subscription if provider is azure
    if provider == "azure":
        import os
        # Get from environment or config
        subscription_id = registry_config.get("subscription_id") or \
                         os.environ.get("AZURE_SUBSCRIPTION_ID")
        if not subscription_id:
            console.print("[red]Azure subscription ID required[/red]")
            console.print("Set AZURE_SUBSCRIPTION_ID or provide in config")
            raise typer.Exit(1)
        registry_config["subscription_id"] = subscription_id
        registry_config["location"] = registry_config.get("location", "eastus2")
    
    def pulumi_program():
        """Create ContainerRegistry in registry stack context."""
        import pulumi
        from ..infra.components.registry import ContainerRegistry
        
        # Create the registry component
        registry = ContainerRegistry(name, registry_config)
        
        # Export outputs at stack level for StackReference access
        pulumi.export("login_server", registry.login_server)
        pulumi.export("registry_name", registry.registry_name)
        pulumi.export("provider", pulumi.Output.from_input(provider))
        pulumi.export("requires_auth", registry.requires_auth)
        
        return registry
    
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("registry", env)
    project_name = StackNaming.get_project_name("registry")
    
    # Use the same backend as infrastructure
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
    backend_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path.home() / ".modelops" / "pulumi" / "registry"
    work_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=auto.ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=auto.ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        console.print(f"\n[bold]Creating container registry: {name}[/bold]")
        console.print(f"Provider: {provider}")
        console.print(f"Environment: {env}")
        console.print(f"Stack: {stack_name}\n")
        
        console.print("[yellow]Creating registry resources...[/yellow]")
        result = stack.up(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        outputs = result.outputs
        
        console.print("\n[green]✓ Registry created successfully![/green]")
        console.print(f"  Login server: {outputs.get('login_server', {}).value if outputs.get('login_server') else 'unknown'}")
        console.print(f"  Registry name: {outputs.get('registry_name', {}).value if outputs.get('registry_name') else 'unknown'}")
        
        if provider == "azure":
            registry_name = outputs.get('registry_name', {}).value if outputs.get('registry_name') else 'unknown'
            console.print(f"\n[bold]Login to registry:[/bold]")
            console.print(f"  az acr login --name {registry_name}")
            console.print(f"\n[bold]Build and push images:[/bold]")
            console.print(f"  docker build -t {registry_name}.azurecr.io/dask-worker:latest .")
            console.print(f"  docker push {registry_name}.azurecr.io/dask-worker:latest")
        
    except Exception as e:
        console.print(f"\n[red]Error creating registry: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def destroy(
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    ),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip confirmation prompt"
    )
):
    """Destroy container registry.
    
    Warning: This will delete the registry and all images stored in it.
    """
    if not yes:
        console.print("\n[bold yellow]⚠️  Warning[/bold yellow]")
        console.print(f"This will destroy the container registry in environment: {env}")
        console.print("All images stored in the registry will be permanently deleted.")
        
        confirm = typer.confirm("\nAre you sure you want to destroy the registry?")
        if not confirm:
            console.print("[green]Destruction cancelled[/green]")
            raise typer.Exit(0)
    
    stack_name = StackNaming.get_stack_name("registry", env)
    project_name = StackNaming.get_project_name("registry")
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
    work_dir = Path.home() / ".modelops" / "pulumi" / "registry"
    
    if not work_dir.exists():
        console.print(f"[yellow]No registry found for environment: {env}[/yellow]")
        raise typer.Exit(0)
    
    try:
        def pulumi_program():
            pass
        
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=auto.ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=auto.ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        console.print(f"\n[yellow]Destroying registry: {stack_name}...[/yellow]")
        stack.destroy(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        console.print("\n[green]✓ Registry destroyed successfully[/green]")
        
    except Exception as e:
        console.print(f"\n[red]Error destroying registry: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def status(
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    )
):
    """Show registry status and connection details."""
    
    stack_name = StackNaming.get_stack_name("registry", env)
    project_name = StackNaming.get_project_name("registry")
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
    work_dir = Path.home() / ".modelops" / "pulumi" / "registry"
    
    if not work_dir.exists() or not backend_dir.exists():
        console.print(f"[yellow]No registry found for environment: {env}[/yellow]")
        console.print("\nRun 'mops registry create' to create a registry")
        raise typer.Exit(0)
    
    try:
        # Read the existing stack without creating a new program
        # We need a minimal program that just reads state
        def pulumi_program():
            import pulumi
            # Empty program that just reads existing state
            pass
        
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=auto.ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=auto.ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        # Refresh to ensure we have latest outputs
        stack.refresh(on_output=lambda msg: None)
        outputs = stack.outputs()
        
        if not outputs:
            console.print(f"[yellow]Registry stack exists but has no outputs[/yellow]")
            console.print("The registry may not be fully deployed.")
            raise typer.Exit(0)
        
        console.print(f"\n[bold]Registry Status[/bold]")
        console.print(f"  Environment: {env}")
        console.print(f"  Stack: {stack_name}")
        console.print(f"  Login server: {outputs.get('login_server', {}).value if outputs.get('login_server') else 'unknown'}")
        console.print(f"  Registry name: {outputs.get('registry_name', {}).value if outputs.get('registry_name') else 'unknown'}")
        console.print(f"  Provider: {outputs.get('provider', {}).value if outputs.get('provider') else 'unknown'}")
        console.print(f"  Requires auth: {outputs.get('requires_auth', {}).value if outputs.get('requires_auth') else 'unknown'}")
        
        provider = outputs.get('provider', {}).value if outputs.get('provider') else None
        if provider == "azure":
            registry_name = outputs.get('registry_name', {}).value if outputs.get('registry_name') else 'unknown'
            console.print(f"\n[bold]Usage commands:[/bold]")
            console.print(f"  Login: az acr login --name {registry_name}")
            console.print(f"  List images: az acr repository list --name {registry_name}")
            console.print(f"  Show tags: az acr repository show-tags --name {registry_name} --repository IMAGE")
        
    except Exception as e:
        console.print(f"[red]Error querying registry status: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def env(
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    ),
    format: str = typer.Option(
        "bash",
        "--format", "-f",
        help="Output format (bash, json, make)"
    )
):
    """Output registry configuration as environment variables.
    
    Examples:
        # Set environment variables in current shell
        eval $(mops registry env)
        
        # Generate .env file
        mops registry env > .modelops.env
        
        # Output as JSON
        mops registry env --format json
    """
    stack_name = StackNaming.get_stack_name("registry", env)
    project_name = StackNaming.get_project_name("registry")
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
    work_dir = Path.home() / ".modelops" / "pulumi" / "registry"
    
    if not work_dir.exists() or not backend_dir.exists():
        if format == "json":
            console.print("{}")
        # Silent exit for shell evaluation
        raise typer.Exit(0)
    
    try:
        def pulumi_program():
            import pulumi
            pass
        
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=auto.ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=auto.ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        # Refresh to get latest outputs
        stack.refresh(on_output=lambda msg: None)
        outputs = stack.outputs()
        
        if not outputs:
            if format == "json":
                console.print("{}")
            raise typer.Exit(0)
        
        # Extract values
        login_server = outputs.get('login_server', {}).value if outputs.get('login_server') else None
        registry_name = outputs.get('registry_name', {}).value if outputs.get('registry_name') else None
        provider = outputs.get('provider', {}).value if outputs.get('provider') else None
        
        if format == "bash":
            # Output as shell export statements
            if login_server:
                print(f"export MODELOPS_REGISTRY_SERVER={login_server}")
            if registry_name:
                print(f"export MODELOPS_REGISTRY_NAME={registry_name}")
            if provider:
                print(f"export MODELOPS_REGISTRY_PROVIDER={provider}")
        elif format == "make":
            # Output as Makefile variables
            if login_server:
                print(f"MODELOPS_REGISTRY_SERVER={login_server}")
            if registry_name:
                print(f"MODELOPS_REGISTRY_NAME={registry_name}")
            if provider:
                print(f"MODELOPS_REGISTRY_PROVIDER={provider}")
        elif format == "json":
            # Output as JSON
            import json
            data = {}
            if login_server:
                data["MODELOPS_REGISTRY_SERVER"] = login_server
            if registry_name:
                data["MODELOPS_REGISTRY_NAME"] = registry_name
            if provider:
                data["MODELOPS_REGISTRY_PROVIDER"] = provider
            print(json.dumps(data, indent=2))
        
    except Exception as e:
        if format != "json":
            console.print(f"# Error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def login(
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    )
):
    """Login to container registry."""
    import subprocess
    
    stack_name = StackNaming.get_stack_name("registry", env)
    project_name = StackNaming.get_project_name("registry")
    
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
    work_dir = Path.home() / ".modelops" / "pulumi" / "registry"
    
    if not work_dir.exists():
        console.print(f"[yellow]No registry found for environment: {env}[/yellow]")
        raise typer.Exit(1)
    
    try:
        def pulumi_program():
            import pulumi
            pass
        
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(work_dir),
                project_settings=auto.ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=auto.ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        # Refresh to get latest outputs
        stack.refresh(on_output=lambda msg: None)
        outputs = stack.outputs()
        
        if not outputs:
            console.print("[red]Registry has no outputs[/red]")
            raise typer.Exit(1)
        
        provider = outputs.get('provider', {}).value if outputs.get('provider') else None
        
        if provider == "azure":
            registry_name = outputs.get('registry_name', {}).value
            console.print(f"[yellow]Logging in to Azure Container Registry: {registry_name}[/yellow]")
            
            # Run az acr login
            result = subprocess.run(
                ["az", "acr", "login", "--name", registry_name],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                console.print("[green]✓ Successfully logged in to registry[/green]")
            else:
                console.print(f"[red]Login failed: {result.stderr}[/red]")
                raise typer.Exit(1)
        else:
            console.print(f"[yellow]Manual login required for {provider} registry[/yellow]")
            login_server = outputs.get('login_server', {}).value
            console.print(f"Use: docker login {login_server}")
        
    except Exception as e:
        console.print(f"[red]Error logging in: {e}[/red]")
        raise typer.Exit(1)