"""Container registry management CLI commands."""

import typer
import yaml
from pathlib import Path
from typing import Optional
from ..client import RegistryService
from ..core import StackNaming, automation
from .utils import resolve_env, resolve_provider, handle_pulumi_error
from .display import console, success, warning, error, info, section, commands, info_dict
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
    
    # Use RegistryService
    service = RegistryService(env)

    try:
        section(f"Creating container registry: {name}")
        info_dict({
            "Provider": provider,
            "Environment": env,
            "Stack": StackNaming.get_stack_name('registry', env)
        })

        warning("\nCreating registry resources...")
        outputs = service.create(name, registry_config, verbose=False)

        success("\n✓ Registry created successfully!")
        info(f"  Login server: {outputs.get('login_server', 'unknown')}")
        info(f"  Registry name: {outputs.get('registry_name', 'unknown')}")
        
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
    
    # Use RegistryService
    service = RegistryService(env)

    try:
        warning(f"\nDestroying registry: {StackNaming.get_stack_name('registry', env)}...")
        service.destroy(verbose=False)
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
    
    # Use RegistryService
    service = RegistryService(env)

    try:
        status = service.status()

        if not status.deployed:
            warning("Registry not deployed")
            info("Run 'mops registry create' to create a registry")
            raise typer.Exit(0)

        section("Registry Status")
        info_dict({
            "Environment": env,
            "Stack": StackNaming.get_stack_name('registry', env),
            "Login server": status.details.get('login_server', 'unknown'),
            "Registry name": status.details.get('registry_name', 'unknown'),
            "Provider": status.details.get('provider', 'unknown'),
            "Requires auth": status.details.get('requires_auth', 'unknown')
        })

        provider = status.details.get('provider')
        if provider == "azure":
            registry_name = status.details.get('registry_name', 'unknown')
            section("\nUsage commands:")
            commands([
                ("Login", f"az acr login --name {registry_name}"),
                ("List images", f"az acr repository list --name {registry_name}"),
                ("Show tags", f"az acr repository show-tags --name {registry_name} --repository IMAGE")
            ])
        
    except Exception as e:
        error(f"Error querying registry status: {e}")
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
    
    # Use RegistryService
    service = RegistryService(env)

    # Use centralized naming
    registry_stack = StackNaming.get_stack_name("registry", env)
    infra_stack = infra_stack or StackNaming.get_stack_name("infra", env)

    success_result = service.wire_permissions(infra_stack)

    if not success_result:
        # Manual steps will be shown by the service
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
    
    # Use RegistryService
    service = RegistryService(env)

    try:
        env_vars = service.get_env_vars(format)
        print(env_vars)
        
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
    
    # Use RegistryService
    service = RegistryService(env)

    try:
        if service.login():
            success("✓ Successfully logged in to registry")
        else:
            error("Login failed")
            raise typer.Exit(1)
        
    except Exception as e:
        error(f"Error logging in: {e}")
        raise typer.Exit(1)