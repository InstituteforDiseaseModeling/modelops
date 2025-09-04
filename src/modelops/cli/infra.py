"""Infrastructure management CLI commands.

Provider-agnostic infrastructure provisioning using ComponentResources.
"""

import typer
import pulumi
from pathlib import Path
from typing import Optional
from ..core import StackNaming, automation
from ..core.automation import get_output_value
from ..components import AzureProviderConfig
from .utils import handle_pulumi_error, resolve_env
from .display import (
    console, success, warning, error, info, section, dim,
    commands, info_dict
)
from .common_options import env_option, yes_option, config_option

app = typer.Typer(help="Manage infrastructure (Azure, AWS, GCP, local)")


@app.command()
def up(
    config: Path = config_option(help_text="Provider configuration file (YAML)"),
    env: Optional[str] = env_option()
):
    """Create infrastructure from zero based on provider config.
    
    This command reads a YAML configuration file and provisions
    infrastructure using Pulumi ComponentResources. The provider type
    is specified in the config file.
    
    Example:
        mops infra up --config ~/.modelops/providers/azure.yaml
    """
    env = resolve_env(env)
    
    # Load and validate configuration
    provider_config = AzureProviderConfig.from_yaml(config)
    
    section(f"Creating {provider_config.provider} infrastructure from zero...")
    info_dict({
        "Config": str(config),
        "Environment": env,
        "Resource Group": f"{provider_config.resource_group}-{env}-rg-{provider_config.username}"
    })
    
    def pulumi_program():
        """Pulumi program that creates infrastructure using ComponentResource."""
        import pulumi
        
        if provider_config.provider == "azure":
            from ..infra.components.azure import ModelOpsCluster
            # Pass validated config dict to component with environment
            config_dict = provider_config.to_pulumi_config()
            config_dict["environment"] = env
            cluster = ModelOpsCluster("modelops", config_dict)
            
            # Export outputs at the stack level for access via StackReference
            pulumi.export("kubeconfig", cluster.kubeconfig)
            pulumi.export("cluster_name", cluster.cluster_name)
            pulumi.export("resource_group", cluster.resource_group)
            pulumi.export("location", cluster.location)
            pulumi.export("provider", pulumi.Output.from_input("azure"))
            
            return cluster
        else:
            raise ValueError(f"Provider '{provider_config.provider}' not yet implemented")
    
    try:
        warning("\nCreating resources (this may take several minutes)...")
        outputs = automation.up("infra", env, None, pulumi_program, on_output=dim)
        
        # Verify kubeconfig exists in outputs
        if not get_output_value(outputs, "kubeconfig"):
            error("No kubeconfig returned from infrastructure creation")
            raise typer.Exit(1)
        
        success("\n✓ Infrastructure created successfully!")
        info_dict({
            "Provider": provider_config.provider,
            "Stack": StackNaming.get_stack_name('infra', env)
        })
        
        section("Stack outputs saved. Query with:")
        commands([
            ("", f"pulumi stack output --stack {StackNaming.get_stack_name('infra', env)} --cwd ~/.modelops/pulumi/infra")
        ])
        
        section("Get kubeconfig:")
        commands([
            ("", f"pulumi stack output kubeconfig --show-secrets --stack {StackNaming.get_stack_name('infra', env)} --cwd ~/.modelops/pulumi/infra")
        ])
        
        section("Next steps:")
        info("  1. Run 'mops workspace up' to deploy Dask")
        info("  2. Run 'mops adaptive up' to start optimization")
        
    except Exception as e:
        error(f"\nError creating infrastructure: {e}")
        handle_pulumi_error(e, "~/.modelops/pulumi/infra", StackNaming.get_stack_name('infra', env))
        raise typer.Exit(1)


@app.command()
def down(
    config: Path = config_option(help_text="Provider configuration file (YAML)"),
    env: Optional[str] = env_option(),
    delete_rg: bool = typer.Option(
        False,
        "--delete-rg",
        help="Also delete the resource group (dangerous!)"
    ),
    yes: bool = yes_option()
):
    """Destroy infrastructure, optionally keeping resource group.
    
    By default, destroys AKS cluster and ACR but preserves the resource group.
    Use --delete-rg to also delete the resource group.
    """
    env = resolve_env(env)
    
    # Load and validate configuration
    provider_config = AzureProviderConfig.from_yaml(config)
    
    # Confirm destruction
    if not yes:
        if delete_rg:
            error("\n⚠️  WARNING: Complete Destruction")
            info("This will destroy the ENTIRE resource group and ALL resources")
            info("This action cannot be undone!")
        else:
            warning("\n⚠️  Infrastructure Teardown")
            info(f"This will destroy {provider_config.provider} resources (AKS, ACR)")
            info("but will preserve the resource group for future use.")
        
        confirm = typer.confirm("\nAre you sure you want to proceed?")
        if not confirm:
            success("Destruction cancelled")
            raise typer.Exit(0)
    
    try:
        warning(f"\nDestroying {provider_config.provider} infrastructure...")
        
        if not delete_rg:
            info("Note: Resource group is protected and will be retained.")
        
        # Destroy using automation helper
        automation.destroy("infra", env, on_output=dim)
        
        if delete_rg and provider_config.provider == "azure":
            # Use centralized naming to compute RG name
            import subprocess
            rg_name = StackNaming.get_resource_group_name(env, provider_config.username)
            
            warning(f"\nDeleting resource group '{rg_name}'...")
            # Use Azure CLI to delete the retained RG
            subprocess.run(["az", "group", "delete", "-n", rg_name, "--yes", "--no-wait"], check=False)
            success("\n✓ Infrastructure destroyed; resource group deletion initiated")
        else:
            success("\n✓ Infrastructure destroyed; resource group retained")
            info("Resource group preserved for future deployments")
        
    except Exception as e:
        error(f"\nError destroying infrastructure: {e}")
        handle_pulumi_error(e, "~/.modelops/pulumi/infra", StackNaming.get_stack_name('infra', env))
        raise typer.Exit(1)


@app.command()
def status(
    env: Optional[str] = env_option(),
    provider: Optional[str] = typer.Option(
        None,
        "--provider", "-p",
        help="Cloud provider (azure, aws, gcp)"
    )
):
    """Show current infrastructure status from Pulumi stack."""
    from .utils import resolve_provider
    
    env = resolve_env(env)
    provider = resolve_provider(provider)
    
    stack_name = StackNaming.get_stack_name("infra", env)
    
    try:
        outputs = automation.outputs("infra", env)
        
        if not outputs:
            warning("Infrastructure stack exists but has no outputs")
            info("The infrastructure may not be fully deployed.")
            raise typer.Exit(0)
        
        section("Infrastructure Status")
        info_dict({
            "Stack": stack_name,
            "Cluster": get_output_value(outputs, 'cluster_name', 'unknown'),
            "Resource Group": get_output_value(outputs, 'resource_group', 'unknown'),
            "Location": get_output_value(outputs, 'location', 'unknown')
        })
        
        if get_output_value(outputs, "kubeconfig"):
            success("  ✓ Kubeconfig available")
        else:
            error("  ✗ Kubeconfig missing")
        
        section("Query outputs:")
        commands([
            ("", f"pulumi stack output --stack {stack_name} --cwd ~/.modelops/pulumi/infra")
        ])
        
        section("Next steps:")
        info("  1. Run 'mops workspace up' to deploy Dask")
        info("  2. Run 'mops adaptive up' to start optimization")
        
    except Exception as e:
        error(f"Error querying infrastructure status: {e}")
        handle_pulumi_error(e, "~/.modelops/pulumi/infra", stack_name)
        raise typer.Exit(1)