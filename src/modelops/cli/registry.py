"""Container registry management CLI commands."""

import typer
import yaml
import pulumi.automation as auto
from pathlib import Path
from typing import Optional
from rich.console import Console
from ..core import StackNaming
from ..core.paths import ensure_work_dir, get_backend_url

app = typer.Typer(help="Manage container registries")
console = Console()


@app.command()
def create(
    name: str = typer.Option(
        "modelops-registry",
        "--name", "-n",
        help="Registry name"
    ),
    provider: Optional[str] = typer.Option(
        None,
        "--provider", "-p",
        help="Registry provider (azure, dockerhub, ghcr)"
    ),
    config: Optional[Path] = typer.Option(
        None,
        "--config", "-c",
        help="Registry configuration file (YAML)"
    ),
    env: Optional[str] = typer.Option(
        None,
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
    # Resolve defaults from config if not provided
    from .utils import resolve_env, resolve_provider
    env = resolve_env(env)
    provider = resolve_provider(provider)
    
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
                         None  # Will use Azure CLI auth
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
        
        # Security: Grant AKS cluster ACR pull permissions to access private images
        # Without this, pods fail with ImagePullBackOff for private registry images
        if provider == "azure" and registry_config.get("grant_cluster_pull", True):
            # Try to wire permissions if infrastructure stack exists
            try:
                infra_ref = StackNaming.ref("infra", env)
                role_assignment = registry.setup_cluster_pull_permissions(infra_ref)
                if role_assignment:
                    pulumi.export("cluster_pull_configured", pulumi.Output.from_input(True))
            except Exception:
                # Infrastructure stack doesn't exist yet, that's OK
                pulumi.export("cluster_pull_configured", pulumi.Output.from_input(False))
        
        # Export outputs at stack level for StackReference access
        pulumi.export("login_server", registry.login_server)
        pulumi.export("registry_name", registry.registry_name)
        pulumi.export("provider", pulumi.Output.from_input(provider))
        pulumi.export("requires_auth", registry.requires_auth)
        
        return registry
    
    # Use centralized naming
    stack_name = StackNaming.get_stack_name("registry", env)
    project_name = StackNaming.get_project_name("registry")
    
    # Use paths.py for consistent directory management
    work_dir = ensure_work_dir("registry")
    backend_url = get_backend_url()
    
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
                    backend=auto.ProjectBackend(url=backend_url)
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
    env: Optional[str] = typer.Option(
        None,
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
    # Resolve environment from config if not provided
    from .utils import resolve_env
    env = resolve_env(env)
    
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
    
    work_dir = ensure_work_dir("registry")
    backend_url = get_backend_url()
    
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
                    backend=auto.ProjectBackend(url=backend_url)
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
    env: Optional[str] = typer.Option(
        None,
        "--env", "-e",
        help="Environment name"
    )
):
    """Show registry status and connection details."""
    # Resolve environment from config if not provided
    from .utils import resolve_env
    env = resolve_env(env)
    
    stack_name = StackNaming.get_stack_name("registry", env)
    project_name = StackNaming.get_project_name("registry")
    
    work_dir = ensure_work_dir("registry")
    backend_url = get_backend_url()
    
    if not work_dir.exists():
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
                    backend=auto.ProjectBackend(url=backend_url)
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
def wire_permissions(
    env: Optional[str] = typer.Option(
        None,
        "--env", "-e",
        help="Environment name"
    ),
    infra_stack: str = typer.Option(
        None,
        "--infra-stack",
        help="Infrastructure stack name (defaults to modelops-infra-{env})"
    )
):
    """Wire ACR pull permissions for AKS cluster.
    
    This command connects an existing registry to an existing AKS cluster
    by granting the cluster's managed identity pull permissions on ACR.
    
    Run this after both registry and infrastructure stacks are created.
    """
    # Resolve environment from config if not provided
    from .utils import resolve_env
    env = resolve_env(env)
    
    # Use centralized naming
    registry_stack = StackNaming.get_stack_name("registry", env)
    infra_stack = infra_stack or StackNaming.get_stack_name("infra", env)
    
    console.print(f"[bold]Wiring registry permissions[/bold]")
    console.print(f"  Registry stack: {registry_stack}")
    console.print(f"  Infrastructure stack: {infra_stack}")
    
    # Security: This grants the AKS cluster's kubelet identity ACR pull permissions
    # Without this, private container images cannot be pulled by the cluster
    console.print("\n[yellow]Note: This grants the AKS cluster pull access to ACR[/yellow]")
    console.print("This is required for pulling private container images.")
    
    # TODO: Implement the actual permission wiring using Pulumi automation API
    # This would update the registry stack to add the role assignment
    console.print("\n[yellow]Manual steps for now:[/yellow]")
    console.print("1. Get the AKS cluster's kubelet identity:")
    console.print("   az aks show -n <cluster> -g <rg> --query identityProfile.kubeletidentity.objectId")
    console.print("2. Grant ACR pull permissions:")
    console.print("   az role assignment create --assignee <identity> --role acrpull --scope <acr-id>")
    
    console.print("\n[dim]Automated wiring will be implemented in a future update[/dim]")


@app.command()
def env(
    env: Optional[str] = typer.Option(
        None,
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
    # Resolve environment from config if not provided
    from .utils import resolve_env
    env = resolve_env(env)
    
    stack_name = StackNaming.get_stack_name("registry", env)
    project_name = StackNaming.get_project_name("registry")
    
    work_dir = ensure_work_dir("registry")
    backend_url = get_backend_url()
    
    if not work_dir.exists():
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
                    backend=auto.ProjectBackend(url=backend_url)
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
    env: Optional[str] = typer.Option(
        None,
        "--env", "-e",
        help="Environment name"
    )
):
    """Login to container registry."""
    # Resolve environment from config if not provided
    from .utils import resolve_env
    env = resolve_env(env)
    
    import subprocess
    
    stack_name = StackNaming.get_stack_name("registry", env)
    project_name = StackNaming.get_project_name("registry")
    
    work_dir = ensure_work_dir("registry")
    backend_url = get_backend_url()
    
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
                    backend=auto.ProjectBackend(url=backend_url)
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