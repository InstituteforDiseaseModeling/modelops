"""Infrastructure management CLI commands.

Provider-agnostic infrastructure provisioning using ComponentResources.
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
    infrastructure using Pulumi ComponentResources. The provider type
    is specified in the config file.
    
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
    
    def pulumi_program():
        """Pulumi program that creates infrastructure using ComponentResource."""
        import pulumi
        
        if provider_type == "azure":
            from ..infra.components.azure import ModelOpsCluster
            # Single component handles all complexity
            cluster = ModelOpsCluster("modelops", provider_config)
            
            # Export outputs at the stack level for access via StackReference
            pulumi.export("kubeconfig", cluster.kubeconfig)
            pulumi.export("cluster_name", cluster.cluster_name)
            pulumi.export("resource_group", cluster.resource_group)
            pulumi.export("location", cluster.location)
            pulumi.export("acr_login_server", cluster.acr_login_server)
            pulumi.export("provider", pulumi.Output.from_input("azure"))
            
            return cluster
        else:
            raise ValueError(f"Provider '{provider_type}' not yet implemented")
    
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
        
        # Extract outputs from ComponentResource
        outputs = result.outputs
        
        # Verify kubeconfig exists in outputs
        if not outputs.get("kubeconfig"):
            console.print("[red]Error: No kubeconfig returned from infrastructure creation[/red]")
            raise typer.Exit(1)
        
        console.print("\n[green]✓ Infrastructure created successfully![/green]")
        console.print(f"  Provider: {provider_type}")
        console.print(f"  Stack: {stack_name}")
        console.print("\nStack outputs saved. Query with:")
        console.print(f"  pulumi stack output --stack {stack_name} --cwd ~/.modelops/pulumi/{provider_type}")
        console.print("\nGet kubeconfig:")
        console.print(f"  pulumi stack output kubeconfig --show-secrets --stack {stack_name} --cwd ~/.modelops/pulumi/{provider_type}")
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
    delete_rg: bool = typer.Option(
        False,
        "--delete-rg",
        help="Also delete the resource group (dangerous!)"
    ),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip confirmation prompt"
    )
):
    """Destroy infrastructure, optionally keeping resource group.
    
    By default, destroys AKS cluster and ACR but preserves the resource group.
    Use --delete-rg to also delete the resource group.
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
        if delete_rg:
            console.print("\n[bold red]⚠️  WARNING: Complete Destruction[/bold red]")
            console.print(f"This will destroy the ENTIRE resource group and ALL resources")
            console.print("This action cannot be undone!")
        else:
            console.print("\n[yellow]⚠️  Infrastructure Teardown[/yellow]")
            console.print(f"This will destroy {provider_type} resources (AKS, ACR)")
            console.print("but will preserve the resource group for future use.")
        
        confirm = typer.confirm("\nAre you sure you want to proceed?")
        if not confirm:
            console.print("[green]Destruction cancelled[/green]")
            raise typer.Exit(0)
    
    stack_name = provider_config.get("stack_name", "modelops-infra")
    project_name = provider_config.get("project_name", "modelops-infra")
    
    # Set up paths for local backend (must match 'up' command)
    pulumi_dir = Path.home() / ".modelops" / "pulumi" / provider_type
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / provider_type
    
    try:
        if not delete_rg and provider_type == "azure":
            # Special handling to preserve resource group
            def pulumi_program():
                import pulumi_azure_native as azure
                from ..infra.components.azure import ModelOpsCluster
                
                # First, get the username to determine RG name
                import os
                import re
                username = provider_config.get("username")
                if not username:
                    username = os.environ.get("USER") or os.environ.get("USERNAME")
                    if username:
                        username = re.sub(r'[^a-zA-Z0-9-]', '', username).lower()[:20]
                
                if not username:
                    raise ValueError("Cannot determine username for resource group")
                
                base_rg = provider_config.get("resource_group", "modelops-rg")
                rg_name = f"{base_rg}-{username}"
                
                # Import existing resource group to prevent deletion
                rg = azure.resources.ResourceGroup.get(
                    "existing-rg",
                    rg_name,
                    opts=pulumi.ResourceOptions(retain_on_delete=True)
                )
                
                # Export that we're keeping it
                pulumi.export("resource_group_retained", rg.name)
                
                # Return empty - we're just preserving the RG
                return None
        else:
            # Normal destroy - everything including RG if requested
            def pulumi_program():
                if provider_type == "azure":
                    from ..infra.components.azure import ModelOpsCluster
                    # Create component just for destroy operation
                    return ModelOpsCluster("modelops", provider_config)
                else:
                    raise ValueError(f"Provider '{provider_type}' not supported")
        
        # Use same stack configuration as 'up' command
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
        
        if not delete_rg:
            console.print("\n[green]✓ Infrastructure destroyed, resource group retained[/green]")
            console.print("Resource group preserved for future deployments")
        else:
            console.print("\n[green]✓ All infrastructure including resource group destroyed[/green]")
        
    except Exception as e:
        console.print(f"\n[red]Error destroying infrastructure: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def status(
    stack_name: str = typer.Option(
        "modelops-infra",
        "--stack", "-s",
        help="Pulumi stack name"
    )
):
    """Show current infrastructure status from Pulumi stack."""
    import pulumi.automation as auto
    from pathlib import Path
    
    # Set up paths for local backend
    pulumi_dir = Path.home() / ".modelops" / "pulumi" / "azure"
    backend_dir = Path.home() / ".modelops" / "pulumi" / "backend" / "azure"
    
    if not pulumi_dir.exists() or not backend_dir.exists():
        console.print("[yellow]No infrastructure found[/yellow]")
        console.print("\nRun 'mops infra up --config <file>' to create infrastructure")
        raise typer.Exit(0)
    
    try:
        # Need minimal program to query stack (just for outputs)
        def pulumi_program():
            pass
        
        # Get stack to query outputs
        stack = auto.create_or_select_stack(
            stack_name=stack_name,
            project_name="modelops-infra",
            program=pulumi_program,
            opts=auto.LocalWorkspaceOptions(
                work_dir=str(pulumi_dir),
                project_settings=auto.ProjectSettings(
                    name="modelops-infra",
                    runtime="python",
                    backend=auto.ProjectBackend(url=f"file://{backend_dir}")
                )
            )
        )
        
        outputs = stack.outputs()
        
        if not outputs:
            console.print("[yellow]Infrastructure stack exists but has no outputs[/yellow]")
            console.print("The infrastructure may not be fully deployed.")
            raise typer.Exit(0)
        
        console.print("\n[bold]Infrastructure Status[/bold]")
        console.print(f"  Stack: {stack_name}")
        console.print(f"  Cluster: {outputs.get('cluster_name', {}).value if outputs.get('cluster_name') else 'unknown'}")
        console.print(f"  Resource Group: {outputs.get('resource_group', {}).value if outputs.get('resource_group') else 'unknown'}")
        console.print(f"  Location: {outputs.get('location', {}).value if outputs.get('location') else 'unknown'}")
        
        if outputs.get("kubeconfig"):
            console.print(f"  [green]✓[/green] Kubeconfig available")
        else:
            console.print(f"  [red]✗[/red] Kubeconfig missing")
        
        if outputs.get("acr_login_server"):
            console.print(f"  ACR: {outputs.get('acr_login_server', {}).value}")
        
        console.print("\nQuery outputs:")
        console.print(f"  pulumi stack output --stack {stack_name} --cwd ~/.modelops/pulumi/azure")
        console.print("\nNext steps:")
        console.print("  1. Run 'mops workspace up' to deploy Dask")
        console.print("  2. Run 'mops adaptive up' to start optimization")
        
    except Exception as e:
        console.print(f"[red]Error querying infrastructure status: {e}[/red]")
        raise typer.Exit(1)