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
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip dependency checks and force destruction"
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
    
    # Check for dependent stacks unless forced
    if not force:
        import subprocess
        from ..core.paths import WORK_DIRS
        
        dependent_stacks = []
        for component in ["workspace", "storage", "adaptive"]:
            if component not in WORK_DIRS:
                continue
            
            stack_name = StackNaming.get_stack_name(component, env)
            work_dir = WORK_DIRS[component]
            
            # Check if stack exists
            cmd = ["pulumi", "stack", "--cwd", str(work_dir), "--stack", stack_name]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                # Stack exists, check if it has resources
                cmd = ["pulumi", "stack", "--cwd", str(work_dir), "--stack", stack_name, "--show-urns"]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if "URN" in result.stdout:
                    dependent_stacks.append(component)
        
        if dependent_stacks:
            warning("\n⚠️  Dependent stacks detected!")
            info("The following stacks depend on this infrastructure:")
            for stack in dependent_stacks:
                info(f"  • {stack}")
            
            info("\nDestroying infrastructure will make these stacks unreachable.")
            info("Consider cleaning them up first:")
            commands([
                ("Recommended", f"mops {' '.join(dependent_stacks)} down"),
                ("Or use", "mops cleanup all")
            ])
            
            if not yes:
                confirm = typer.confirm("\nDestroy infrastructure anyway?")
                if not confirm:
                    success("Destruction cancelled")
                    raise typer.Exit(0)
    
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


@app.command()
def kubeconfig(
    env: Optional[str] = env_option(),
    output: Optional[Path] = typer.Option(
        None,
        "--output", "-o",
        help="Write kubeconfig to file instead of stdout"
    ),
    merge: bool = typer.Option(
        False,
        "--merge",
        help="Merge with existing ~/.kube/config"
    )
):
    """Get kubeconfig from infrastructure state.
    
    Fetches the current kubeconfig from Pulumi state and either
    displays it, saves it to a file, or merges it with existing config.
    
    Examples:
        mops infra kubeconfig                    # Display to stdout
        mops infra kubeconfig -o kubeconfig.yaml # Save to file
        mops infra kubeconfig --merge            # Update ~/.kube/config
    """
    env = resolve_env(env)
    stack_name = StackNaming.get_stack_name("infra", env)
    
    try:
        # Get outputs from infrastructure stack
        outputs = automation.outputs("infra", env, refresh=False)
        
        if not outputs:
            error("No infrastructure found")
            info("Run 'mops infra up' to create infrastructure first")
            raise typer.Exit(1)
        
        kubeconfig_yaml = get_output_value(outputs, "kubeconfig")
        if not kubeconfig_yaml:
            error("No kubeconfig found in infrastructure outputs")
            info("Infrastructure may not be fully deployed")
            raise typer.Exit(1)
        
        cluster_name = get_output_value(outputs, "cluster_name", f"modelops-{env}")
        
        if merge:
            # Merge with existing kubeconfig
            import subprocess
            import tempfile
            import os
            
            # Create temp file with new kubeconfig
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(kubeconfig_yaml)
                temp_path = f.name
            
            try:
                # Backup existing config
                kube_dir = Path.home() / ".kube"
                kube_dir.mkdir(exist_ok=True)
                config_path = kube_dir / "config"
                
                if config_path.exists():
                    backup_path = kube_dir / "config.backup"
                    import shutil
                    shutil.copy(config_path, backup_path)
                    info(f"Backed up existing config to {backup_path}")
                
                # Use kubectl to merge configs
                env_vars = os.environ.copy()
                if config_path.exists():
                    env_vars["KUBECONFIG"] = f"{config_path}:{temp_path}"
                else:
                    env_vars["KUBECONFIG"] = temp_path
                
                result = subprocess.run(
                    ["kubectl", "config", "view", "--flatten"],
                    env=env_vars,
                    capture_output=True,
                    text=True
                )
                
                if result.returncode != 0:
                    error(f"Failed to merge kubeconfig: {result.stderr}")
                    raise typer.Exit(1)
                
                # Write merged config
                config_path.write_text(result.stdout)
                success(f"✓ Merged kubeconfig for cluster '{cluster_name}' into ~/.kube/config")
                
                # Set current context
                subprocess.run(
                    ["kubectl", "config", "use-context", cluster_name],
                    capture_output=True
                )
                info(f"Current context set to: {cluster_name}")
                
            finally:
                # Clean up temp file
                os.unlink(temp_path)
        
        elif output:
            # Write to specified file
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(kubeconfig_yaml)
            success(f"✓ Kubeconfig saved to {output}")
            info(f"\nTo use: export KUBECONFIG={output.absolute()}")
        
        else:
            # Output to stdout
            console.print(kubeconfig_yaml)
    
    except Exception as e:
        error(f"Error getting kubeconfig: {e}")
        handle_pulumi_error(e, "~/.modelops/pulumi/infra", stack_name)
        raise typer.Exit(1)