"""Environment-specific configuration management.

This module provides caching of infrastructure outputs (registry, storage)
in ~/.modelops/bundle-env/ for fast access by modelops-bundle and other tools.
Unlike the main config.yaml which stores user preferences, these environment
configs cache deployed infrastructure details.
"""

from typing import Optional, Dict, Any, List
from pathlib import Path
from datetime import datetime
import yaml
import os

# Import from modelops-contracts
from modelops_contracts.bundle_environment import (
    BundleEnvironment,
    RegistryConfig,
    StorageConfig,
    ENVIRONMENTS_DIR
)


def get_environments_dir() -> Path:
    """Get the environments directory path.

    Returns:
        Path to ~/.modelops/bundle-env/
    """
    return ENVIRONMENTS_DIR


def save_environment_config(
    env: str,
    registry_outputs: Optional[Dict[str, Any]] = None,
    storage_outputs: Optional[Dict[str, Any]] = None
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
            requires_auth=registry_outputs.get("requires_auth", True)
        )

    storage = None
    if storage_outputs:
        containers = storage_outputs.get("containers", [])
        # Extract container names if they're dicts
        if containers and isinstance(containers[0], dict):
            container_names = [c.get("name", "unnamed") for c in containers]
        else:
            container_names = containers

        # Use the primary container (bundles) for the singular container field
        primary_container = "bundles" if "bundles" in container_names else (container_names[0] if container_names else "bundles")

        storage = StorageConfig(
            provider="azure",  # Default for now
            container=primary_container,
            connection_string=storage_outputs.get("sas_connection_string"),  # Use read-only SAS
            endpoint=storage_outputs.get("primary_endpoint")
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
        timestamp=datetime.utcnow().isoformat()
    )

    # Save using Pydantic's serialization
    config_path = get_environments_dir() / f"{env}.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Use Pydantic's model_dump for clean serialization
    with open(config_path, "w") as f:
        yaml.safe_dump(config.model_dump(exclude_none=True), f, default_flow_style=False, sort_keys=False)

    # Restrict permissions for security (connection strings)
    os.chmod(config_path, 0o600)

    return config_path


def load_environment_config(env: str) -> Optional[BundleEnvironment]:
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


def list_environments() -> List[str]:
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


def create_local_dev_config() -> Path:
    """Create local development config with Docker registry and Azurite.

    This is called when local Docker containers are detected.

    Returns:
        Path to created config file
    """
    # Create local dev config using contract models
    registry = RegistryConfig(
        provider="docker",
        login_server="localhost:5555",
        requires_auth=False
    )

    storage = StorageConfig(
        provider="azure",  # Azurite is Azure-compatible
        container="bundles",
        connection_string=(
            "DefaultEndpointsProtocol=http;"
            "AccountName=devstoreaccount1;"
            "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
            "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1"
        ),
        endpoint="http://127.0.0.1:10000/devstoreaccount1"
    )

    config = BundleEnvironment(
        environment="local",
        registry=registry,
        storage=storage,
        timestamp=datetime.utcnow().isoformat()
    )

    config_path = get_environments_dir() / "local.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w") as f:
        yaml.safe_dump(config.model_dump(exclude_none=True), f, default_flow_style=False, sort_keys=False)

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
            timeout=2
        )

        if result.returncode == 0:
            containers = result.stdout.strip().split("\n")
            has_registry = any("registry" in c.lower() for c in containers)
            has_azurite = any("azurite" in c.lower() for c in containers)
            return has_registry or has_azurite
    except:
        pass

    return False


def ensure_local_dev_config() -> Optional[Path]:
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