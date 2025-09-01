"""Infrastructure management CLI commands.

Provider-agnostic infrastructure provisioning following the spec.
"""

import typer
import yaml
import pulumi
import pulumi.automation as auto
from pulumi.automation import ProjectSettings, ProjectBackend
from pathlib import Path
from typing import Optional
from rich.console import Console

app = typer.Typer(help="Manage infrastructure (Azure, AWS, GCP, local)")
console = Console()


@app.command()
def up(
    config: Path = typer.Option(
        ...,
        "--config", "-c",
        help="Provider configuration file (YAML)",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True
    )
):
    """Create infrastructure from zero based on provider config.
    
    This command reads a YAML configuration file and provisions
    infrastructure accordingly. The provider type is specified
    in the config file.
    
    Example:
        mops infra up --config ~/.modelops/providers/azure.yaml
    """
    # Load configuration
    with open(config) as f:
        provider_config = yaml.safe_load(f)
    
    provider_type = provider_config.get("provider")
    if not provider_type:
        console.print("[red]Error: 'provider' field required in config[/red]")
        raise typer.Exit(1)
    
    console.print(f"[bold]Creating {provider_type} infrastructure from zero...[/bold]")
    console.print(f"Config: {config}")
    
    # Import the appropriate bootstrap module based on provider
    if provider_type == "azure":
        from ..infra.azure_bootstrap import create_azure_infrastructure
        create_func = create_azure_infrastructure
    else:
        console.print(f"[red]Error: Provider '{provider_type}' not yet implemented[/red]")
        console.print("[dim]Supported providers: azure[/dim]")
        raise typer.Exit(1)
    
    def pulumi_program():
        """Pulumi program that creates infrastructure."""
        binding_output = create_func(provider_config)  # This is Output[ClusterBinding]
        
        # Export individual fields from the Output
        pulumi.export("kubeconfig", binding_output.apply(lambda b: pulumi.Output.secret(b.kubeconfig)))
        pulumi.export("cluster_name", binding_output.apply(lambda b: b.cluster_name))
        pulumi.export("resource_group", binding_output.apply(lambda b: b.resource_group))
        pulumi.export("location", binding_output.apply(lambda b: b.location))
        pulumi.export("acr_login_server", binding_output.apply(lambda b: b.acr_login_server if b.acr_login_server else None))
    
    # Create or select Pulumi stack
    stack_name = provider_config.get("stack_name", "modelops-infra")
    project_name = provider_config.get("project_name", "modelops-infra")
    
    # Set up paths for local backend
    pulumi_dir = Path.home() / ".modelops" / "pulumi" / provider_type
    pulumi_dir.mkdir(parents=True, exist_ok=True)
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / provider_type
    backend_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(pulumi_dir),
                project_settings=ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        # Set provider configuration
        if provider_type == "azure":
            if "subscription_id" in provider_config:
                stack.set_config("azure:subscription_id", 
                               auto.ConfigValue(provider_config["subscription_id"]))
            if "location" in provider_config:
                stack.set_config("azure:location", 
                               auto.ConfigValue(provider_config["location"]))
        
        # Run pulumi up
        console.print("\n[yellow]Creating resources (this may take several minutes)...[/yellow]")
        result = stack.up(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        # Extract outputs (don't store sensitive kubeconfig!)
        outputs = result.outputs
        
        # Get values from outputs
        cluster_name = outputs.get("cluster_name").value if outputs.get("cluster_name") else "unknown"
        resource_group = outputs.get("resource_group").value if outputs.get("resource_group") else "unknown"
        location = outputs.get("location").value if outputs.get("location") else "unknown"
        acr_login_server = outputs.get("acr_login_server").value if outputs.get("acr_login_server") else None
        
        # Verify kubeconfig exists but don't store it
        kubeconfig_output = outputs.get("kubeconfig")
        if not kubeconfig_output or not kubeconfig_output.value:
            console.print("[red]Error: No kubeconfig returned from infrastructure creation[/red]")
            raise typer.Exit(1)
        
        # Save only metadata to state (no sensitive data!)
        from ..state.manager import StateManager
        
        state = StateManager()
        state.save_binding("infra", {
            "provider": provider_type,
            "cluster_name": cluster_name,
            "resource_group": resource_group,
            "location": location,
            "acr_login_server": acr_login_server,
            "stack_name": stack_name,
            "project_name": project_name
        })
        
        console.print("\n[green]✓ Infrastructure created successfully![/green]")
        console.print(f"  Provider: {provider_type}")
        console.print(f"  Stack: {stack_name}")
        console.print("  ClusterBinding saved to state")
        console.print("\nNext steps:")
        console.print("  1. Run 'mops workspace up' to deploy Dask")
        console.print("  2. Run 'mops adaptive up' to start optimization")
        
    except Exception as e:
        console.print(f"\n[red]Error creating infrastructure: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def down(
    config: Path = typer.Option(
        ...,
        "--config", "-c",
        help="Provider configuration file (YAML)",
        exists=True
    ),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip confirmation prompt"
    )
):
    """Destroy infrastructure created by 'infra up'.
    
    WARNING: This will delete all resources including data!
    """
    # Load configuration
    with open(config) as f:
        provider_config = yaml.safe_load(f)
    
    provider_type = provider_config.get("provider")
    if not provider_type:
        console.print("[red]Error: 'provider' field required in config[/red]")
        raise typer.Exit(1)
    
    # Confirm destruction
    if not yes:
        console.print("\n[bold red]⚠️  WARNING: Destructive Operation[/bold red]")
        console.print(f"This will destroy ALL {provider_type} infrastructure")
        console.print("including clusters, databases, and all data.")
        
        confirm = typer.confirm("\nAre you sure you want to destroy all infrastructure?")
        if not confirm:
            console.print("[green]Destruction cancelled[/green]")
            raise typer.Exit(0)
    
    stack_name = provider_config.get("stack_name", "modelops-infra")
    project_name = provider_config.get("project_name", "modelops-infra")
    
    # Set up paths for local backend (must match 'up' command)
    pulumi_dir = Path.home() / ".modelops" / "pulumi" / provider_type
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / provider_type
    
    try:
        # We need a minimal program even for destroy
        def pulumi_program():
            pass
        
        # Use create_or_select_stack (not select_stack) for consistency
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name=project_name,
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(pulumi_dir),
                project_settings=ProjectSettings(
                    name=project_name,
                    runtime="python",
                    backend=ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        console.print(f"\n[yellow]Destroying {provider_type} infrastructure...[/yellow]")
        stack.destroy(on_output=lambda msg: console.print(f"[dim]{msg}[/dim]", end=""))
        
        # Clear state
        from ..state.manager import StateManager
        state = StateManager()
        state.remove_binding("infra")
        
        console.print("\n[green]✓ Infrastructure destroyed successfully[/green]")
        
    except Exception as e:
        console.print(f"\n[red]Error destroying infrastructure: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def status():
    """Show current infrastructure status."""
    from ..state.manager import StateManager
    
    state = StateManager()
    binding = state.get_binding("infra")
    
    if not binding:
        console.print("[yellow]No infrastructure found[/yellow]")
        console.print("\nRun 'mops infra up --config <file>' to create infrastructure")
        raise typer.Exit(0)
    
    console.print("\n[bold]Infrastructure Status[/bold]")
    console.print(f"  Provider: {binding.get('provider', 'unknown')}")
    console.print(f"  Cluster: {binding.get('cluster_name', 'unknown')}")
    console.print(f"  Resource Group: {binding.get('resource_group', 'unknown')}")
    console.print(f"  Location: {binding.get('location', 'unknown')}")
    
    if binding.get("kubeconfig"):
        console.print(f"  [green]✓[/green] Kubeconfig available")
    else:
        console.print(f"  [red]✗[/red] Kubeconfig missing")
    
    console.print("\nNext steps:")
    console.print("  1. Run 'mops workspace up' to deploy Dask")
    console.print("  2. Run 'mops adaptive up' to start optimization")