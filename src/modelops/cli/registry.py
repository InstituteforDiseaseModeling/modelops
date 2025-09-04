"""Container registry management CLI commands."""

import typer
import yaml
from pathlib import Path
from typing import Optional
from ..core import StackNaming, automation
from .utils import handle_pulumi_error, resolve_env, resolve_provider
from .display import console, success, warning, error, info, section, dim, commands, info_dict
from .common_options import env_option, yes_option

app = typer.Typer(help="Manage container registries")


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
    env: Optional[str] = env_option()
):
    """Create a new container registry.
    
    This creates an independent registry stack that can be referenced
    by workspace and other stacks for pulling container images.
    
    Examples:
        mops registry create --name modelops --provider azure
        mops registry create --config registry.yaml
    """
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
            error("Azure subscription ID required")
            info("Set AZURE_SUBSCRIPTION_ID or provide in config")
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
    
    try:
        section(f"Creating container registry: {name}")
        info_dict({
            "Provider": provider,
            "Environment": env,
            "Stack": StackNaming.get_stack_name('registry', env)
        })
        
        warning("\nCreating registry resources...")
        outputs = automation.up("registry", env, None, pulumi_program, on_output=dim)
        
        success("\n✓ Registry created successfully!")
        info(f"  Login server: {automation.get_output_value(outputs, 'login_server', 'unknown')}")
        info(f"  Registry name: {automation.get_output_value(outputs, 'registry_name', 'unknown')}")
        
        if provider == "azure":
            registry_name = automation.get_output_value(outputs, 'registry_name', 'unknown')
            section("\nLogin to registry:")
            commands([
                ("", f"az acr login --name {registry_name}")
            ])
            section("\nBuild and push images:")
            commands([
                ("Build", f"docker build -t {registry_name}.azurecr.io/dask-worker:latest ."),
                ("Push", f"docker push {registry_name}.azurecr.io/dask-worker:latest")
            ])
        
    except Exception as e:
        error(f"\nError creating registry: {e}")
        handle_pulumi_error(e, "~/.modelops/pulumi/registry", StackNaming.get_stack_name('registry', env))
        raise typer.Exit(1)


@app.command()
def destroy(
    env: Optional[str] = env_option(),
    yes: bool = yes_option()
):
    """Destroy container registry.
    
    Warning: This will delete the registry and all images stored in it.
    """
    env = resolve_env(env)
    
    if not yes:
        warning("\n⚠️  Warning")
        info(f"This will destroy the container registry in environment: {env}")
        info("All images stored in the registry will be permanently deleted.")
        
        confirm = typer.confirm("\nAre you sure you want to destroy the registry?")
        if not confirm:
            success("Destruction cancelled")
            raise typer.Exit(0)
    
    try:
        warning(f"\nDestroying registry: {StackNaming.get_stack_name('registry', env)}...")
        automation.destroy("registry", env, on_output=dim)
        success("\n✓ Registry destroyed successfully")
        
    except Exception as e:
        error(f"\nError destroying registry: {e}")
        handle_pulumi_error(e, "~/.modelops/pulumi/registry", StackNaming.get_stack_name('registry', env))
        raise typer.Exit(1)


@app.command()
def status(
    env: Optional[str] = env_option()
):
    """Show registry status and connection details."""
    env = resolve_env(env)
    
    try:
        outputs = automation.outputs("registry", env)
        
        if not outputs:
            warning("Registry stack exists but has no outputs")
            info("The registry may not be fully deployed.")
            raise typer.Exit(0)
        
        section("Registry Status")
        info_dict({
            "Environment": env,
            "Stack": StackNaming.get_stack_name('registry', env),
            "Login server": automation.get_output_value(outputs, 'login_server', 'unknown'),
            "Registry name": automation.get_output_value(outputs, 'registry_name', 'unknown'),
            "Provider": automation.get_output_value(outputs, 'provider', 'unknown'),
            "Requires auth": automation.get_output_value(outputs, 'requires_auth', 'unknown')
        })
        
        provider = automation.get_output_value(outputs, 'provider')
        if provider == "azure":
            registry_name = automation.get_output_value(outputs, 'registry_name', 'unknown')
            section("\nUsage commands:")
            commands([
                ("Login", f"az acr login --name {registry_name}"),
                ("List images", f"az acr repository list --name {registry_name}"),
                ("Show tags", f"az acr repository show-tags --name {registry_name} --repository IMAGE")
            ])
        
    except Exception as e:
        error(f"Error querying registry status: {e}")
        handle_pulumi_error(e, "~/.modelops/pulumi/registry", StackNaming.get_stack_name('registry', env))
        raise typer.Exit(1)


@app.command()
def wire_permissions(
    env: Optional[str] = env_option(),
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
    env = resolve_env(env)
    
    # Use centralized naming
    registry_stack = StackNaming.get_stack_name("registry", env)
    infra_stack = infra_stack or StackNaming.get_stack_name("infra", env)
    
    section("Wiring registry permissions")
    info(f"  Registry stack: {registry_stack}")
    info(f"  Infrastructure stack: {infra_stack}")
    
    # Security: This grants the AKS cluster's kubelet identity ACR pull permissions
    # Without this, private container images cannot be pulled by the cluster
    warning("\nNote: This grants the AKS cluster pull access to ACR")
    info("This is required for pulling private container images.")
    
    # TODO: Implement the actual permission wiring using Pulumi automation API
    # This would update the registry stack to add the role assignment
    warning("\nManual steps for now:")
    info("1. Get the AKS cluster's kubelet identity:")
    commands([
        ("", "az aks show -n <cluster> -g <rg> --query identityProfile.kubeletidentity.objectId")
    ])
    info("2. Grant ACR pull permissions:")
    commands([
        ("", "az role assignment create --assignee <identity> --role acrpull --scope <acr-id>")
    ])
    
    info("\nAutomated wiring will be implemented in a future update")


@app.command()
def env(
    env: Optional[str] = env_option(),
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
    env = resolve_env(env)
    
    try:
        outputs = automation.outputs("registry", env, refresh=True)
        
        if not outputs:
            if format == "json":
                print("{}")
            raise typer.Exit(0)
        
        # Extract values
        login_server = automation.get_output_value(outputs, 'login_server')
        registry_name = automation.get_output_value(outputs, 'registry_name')
        provider = automation.get_output_value(outputs, 'provider')
        
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
    env: Optional[str] = env_option()
):
    """Login to container registry."""
    env = resolve_env(env)
    
    import subprocess
    
    try:
        outputs = automation.outputs("registry", env)
        
        if not outputs:
            error("Registry has no outputs")
            raise typer.Exit(1)
        
        provider = automation.get_output_value(outputs, 'provider')
        
        if provider == "azure":
            registry_name = automation.get_output_value(outputs, 'registry_name')
            warning(f"Logging in to Azure Container Registry: {registry_name}")
            
            # Run az acr login
            result = subprocess.run(
                ["az", "acr", "login", "--name", registry_name],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                success("✓ Successfully logged in to registry")
            else:
                error(f"Login failed: {result.stderr}")
                raise typer.Exit(1)
        else:
            warning(f"Manual login required for {provider} registry")
            login_server = automation.get_output_value(outputs, 'login_server')
            info(f"Use: docker login {login_server}")
        
    except Exception as e:
        error(f"Error logging in: {e}")
        raise typer.Exit(1)