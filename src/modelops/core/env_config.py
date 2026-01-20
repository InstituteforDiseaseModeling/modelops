"""Environment-specific configuration management.

This module provides caching of infrastructure outputs (registry, storage)
in ~/.modelops/bundle-env/ for fast access by modelops-bundle and other tools.
Unlike the main config.yaml which stores user preferences, these environment
configs cache deployed infrastructure details.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# Import from modelops-contracts
from modelops_contracts.bundle_environment import (
    ENVIRONMENTS_DIR,
    BundleEnvironment,
    RegistryConfig,
    StorageConfig,
)


def get_environments_dir() -> Path:
    """Get the environments directory path.

    Returns:
        Path to ~/.modelops/bundle-env/
    """
    return ENVIRONMENTS_DIR


def save_environment_config(
    env: str,
    registry_outputs: dict[str, Any] | None = None,
    storage_outputs: dict[str, Any] | None = None,
) -> Path:
    """Save environment configuration to ~/.modelops/bundle-env/<env>.yaml.

    Args:
        env: Environment name
        registry_outputs: Registry stack outputs from Pulumi
        storage_outputs: Storage stack outputs from Pulumi

    Returns:
        Path to saved config file
    """
    # Build the config using the contract models
    registry = None
    if registry_outputs:
        # Map azure provider to acr for the contract
        provider = registry_outputs.get("provider", "azure")
        if provider == "azure":
            provider = "acr"

        registry = RegistryConfig(
            provider=provider,
            login_server=registry_outputs.get("login_server", ""),
            username=None,  # Will be fetched from Azure CLI when needed
            password=None,  # Will be fetched from Azure CLI when needed
            requires_auth=registry_outputs.get("requires_auth", True),
        )

    storage = None
    if storage_outputs:
        containers = storage_outputs.get("containers", [])
        # Extract container names if they're dicts
        if containers and isinstance(containers[0], dict):
            container_names = [c.get("name", "unnamed") for c in containers]
        else:
            container_names = containers

        # Use the primary container (bundle-blobs) for the singular container field
        primary_container = (
            "bundle-blobs"
            if "bundle-blobs" in container_names
            else (container_names[0] if container_names else "bundle-blobs")
        )

        storage = StorageConfig(
            provider="azure",  # Default for now
            container=primary_container,
            connection_string=storage_outputs.get("sas_connection_string"),  # Use read-only SAS
            endpoint=storage_outputs.get("primary_endpoint"),
        )

    # BundleEnvironment requires both registry and storage for bundle operations
    # Without both, we cannot push or pull bundles to/from the cloud
    # FAIL LOUDLY - this is an invalid state that must be fixed
    if not registry or not storage:
        missing_components = []
        if not registry:
            missing_components.append("registry")
        if not storage:
            missing_components.append("storage")

        raise RuntimeError(
            f"Cannot create bundle environment for '{env}': "
            f"Missing {' and '.join(missing_components)} outputs. "
            f"Both registry AND storage are required for bundle operations.\n"
            f"Fix by running: mops infra up --component {','.join(missing_components)}"
        )

    # Create the BundleEnvironment
    config = BundleEnvironment(
        environment=env,
        registry=registry,
        storage=storage,
        timestamp=datetime.utcnow().isoformat(),
    )

    # Save using Pydantic's serialization
    config_path = get_environments_dir() / f"{env}.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Use Pydantic's model_dump for clean serialization
    with open(config_path, "w") as f:
        yaml.safe_dump(
            config.model_dump(exclude_none=True),
            f,
            default_flow_style=False,
            sort_keys=False,
        )

    # Restrict permissions for security (connection strings)
    os.chmod(config_path, 0o600)

    return config_path


def load_environment_config(env: str) -> BundleEnvironment | None:
    """Load environment configuration from ~/.modelops/bundle-env/<env>.yaml.

    Args:
        env: Environment name

    Returns:
        BundleEnvironment if found, None otherwise
    """
    config_path = get_environments_dir() / f"{env}.yaml"
    if not config_path.exists():
        return None

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
        return BundleEnvironment(**data)
    except Exception:
        return None


def list_environments() -> list[str]:
    """List available environment configurations.

    Returns:
        List of environment names
    """
    env_dir = get_environments_dir()
    if not env_dir.exists():
        return []

    envs = []
    for yaml_file in env_dir.glob("*.yaml"):
        env_name = yaml_file.stem
        envs.append(env_name)

    return sorted(envs)


class LocalDevConfigError(Exception):
    """Raised when local development configuration is missing required settings."""

    pass


def _require_env(name: str, purpose: str) -> str:
    """Get a required environment variable or raise with helpful message.

    Args:
        name: Environment variable name
        purpose: Human-readable description of what this variable is for

    Returns:
        Environment variable value

    Raises:
        LocalDevConfigError: If variable is not set
    """
    value = os.environ.get(name)
    if not value:
        raise LocalDevConfigError(
            f"Required environment variable {name} is not set.\n"
            f"Purpose: {purpose}\n\n"
            f"For local development with Azurite, set these environment variables:\n"
            f"  export AZURITE_CONNECTION_STRING='DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=<your-key>;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1'\n\n"
            f"Or run 'make start' in the modelops directory to start local services with proper configuration."
        )
    return value


def _get_azurite_connection_string() -> str:
    """Get Azurite connection string from environment.

    Requires AZURITE_CONNECTION_STRING to be explicitly set.
    No hardcoded defaults - configuration must be explicit.

    Returns:
        Connection string for Azurite

    Raises:
        LocalDevConfigError: If connection string is not configured
    """
    return _require_env(
        "AZURITE_CONNECTION_STRING",
        "Azure Storage connection string for local Azurite emulator",
    )


def create_local_dev_config() -> Path:
    """Create local development config with Docker registry and Azurite.

    Requires explicit configuration via environment variables.
    No hardcoded defaults are used.

    Required environment variables:
        - AZURITE_CONNECTION_STRING: Full connection string for Azurite
        - LOCAL_REGISTRY: Registry URL (e.g., localhost:5555)

    Returns:
        Path to created config file

    Raises:
        LocalDevConfigError: If required configuration is missing
    """
    # Require explicit configuration - no hardcoded defaults
    local_registry = _require_env("LOCAL_REGISTRY", "Docker registry URL for local development")
    connection_string = _get_azurite_connection_string()

    # Parse connection string to extract endpoint info
    # Format: DefaultEndpointsProtocol=http;AccountName=X;AccountKey=Y;BlobEndpoint=Z
    endpoint = None
    for part in connection_string.split(";"):
        if part.startswith("BlobEndpoint="):
            endpoint = part.split("=", 1)[1]
            break

    if not endpoint:
        raise LocalDevConfigError(
            "AZURITE_CONNECTION_STRING must contain BlobEndpoint.\n"
            "Expected format: DefaultEndpointsProtocol=http;AccountName=X;AccountKey=Y;BlobEndpoint=http://host:port/account"
        )

    registry = RegistryConfig(provider="docker", login_server=local_registry, requires_auth=False)

    storage = StorageConfig(
        provider="azure",  # Azurite is Azure-compatible
        container="bundle-blobs",
        connection_string=connection_string,
        endpoint=endpoint,
    )

    config = BundleEnvironment(
        environment="local",
        registry=registry,
        storage=storage,
        timestamp=datetime.utcnow().isoformat(),
    )

    config_path = get_environments_dir() / "local.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w") as f:
        yaml.safe_dump(
            config.model_dump(exclude_none=True),
            f,
            default_flow_style=False,
            sort_keys=False,
        )

    os.chmod(config_path, 0o600)

    return config_path


def detect_local_containers() -> bool:
    """Detect if local Docker containers (registry, azurite) are running.

    Returns:
        True if local containers detected
    """
    import subprocess

    try:
        # Check for modelops-bundles-registry container
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=2,
        )

        if result.returncode == 0:
            containers = result.stdout.strip().split("\n")
            has_registry = any("registry" in c.lower() for c in containers)
            has_azurite = any("azurite" in c.lower() for c in containers)
            return has_registry or has_azurite
    except (subprocess.SubprocessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        # Docker not available or command failed - this is expected in many environments
        pass

    return False


def ensure_local_dev_config() -> Path | None:
    """Ensure local dev config exists if Docker containers are running.

    Returns:
        Path to local config if created/exists, None otherwise
    """
    if detect_local_containers():
        local_config = get_environments_dir() / "local.yaml"
        if not local_config.exists():
            return create_local_dev_config()
        return local_config
    return None
