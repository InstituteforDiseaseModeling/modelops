"""Environment-specific configuration management.

This module provides caching of infrastructure outputs (registry, storage)
in ~/.modelops/environments/ for fast access by modelops-bundle and other tools.
Unlike the main config.yaml which stores user preferences, these environment
configs cache deployed infrastructure details.
"""

from typing import Optional, Dict, Any, List
from pathlib import Path
from datetime import datetime
from pydantic import BaseModel, Field
import yaml
import os


class RegistryConfig(BaseModel):
    """Registry configuration for an environment."""

    provider: str = "docker"
    """Registry provider (azure, docker, ghcr)."""

    login_server: str
    """Registry login server/URL."""

    registry_name: Optional[str] = None
    """Registry name (for Azure ACR)."""

    requires_auth: bool = True
    """Whether authentication is required."""


class StorageConfig(BaseModel):
    """Storage configuration for an environment."""

    provider: str = "azure"
    """Storage provider (azure, azurite, s3, gcs, fs)."""

    account_name: Optional[str] = None
    """Storage account name (Azure)."""

    connection_string: Optional[str] = None
    """Connection string (sensitive, may be encrypted)."""

    containers: List[str] = Field(default_factory=list)
    """List of container/bucket names."""

    endpoint: Optional[str] = None
    """Storage endpoint URL."""


class EnvironmentConfig(BaseModel):
    """Configuration for a specific environment.

    This represents the deployed infrastructure outputs for an environment,
    cached locally for fast access without Pulumi API calls.
    """

    environment: str
    """Environment name (dev, staging, prod, local)."""

    timestamp: datetime = Field(default_factory=datetime.utcnow)
    """When this config was saved."""

    registry: Optional[RegistryConfig] = None
    """Registry configuration."""

    storage: Optional[StorageConfig] = None
    """Storage configuration."""

    # Future extensions can add:
    # cluster: Optional[ClusterConfig] = None
    # database: Optional[DatabaseConfig] = None

    def to_yaml(self, path: Path) -> None:
        """Save config to YAML file.

        Args:
            path: File path to write to
        """
        # Convert to dict with ISO timestamp
        data = self.model_dump(exclude_none=True)
        if "timestamp" in data:
            data["timestamp"] = data["timestamp"].isoformat()

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

        # Restrict permissions for security (connection strings)
        os.chmod(path, 0o600)

    @classmethod
    def from_yaml(cls, path: Path) -> "EnvironmentConfig":
        """Load config from YAML file.

        Args:
            path: File path to read from

        Returns:
            EnvironmentConfig instance
        """
        with open(path) as f:
            data = yaml.safe_load(f)

        # Parse ISO timestamp
        if "timestamp" in data and isinstance(data["timestamp"], str):
            data["timestamp"] = datetime.fromisoformat(data["timestamp"])

        return cls(**data)


def get_environments_dir() -> Path:
    """Get the environments directory path.

    Returns:
        Path to ~/.modelops/environments/
    """
    return Path.home() / ".modelops" / "environments"


def save_environment_config(
    env: str,
    registry_outputs: Optional[Dict[str, Any]] = None,
    storage_outputs: Optional[Dict[str, Any]] = None
) -> Path:
    """Save environment configuration to ~/.modelops/environments/<env>.yaml.

    Args:
        env: Environment name
        registry_outputs: Registry stack outputs from Pulumi
        storage_outputs: Storage stack outputs from Pulumi

    Returns:
        Path to saved config file
    """
    config = EnvironmentConfig(environment=env)

    # Process registry outputs
    if registry_outputs:
        config.registry = RegistryConfig(
            provider=registry_outputs.get("provider", "azure"),
            login_server=registry_outputs.get("login_server", ""),
            registry_name=registry_outputs.get("registry_name"),
            requires_auth=registry_outputs.get("requires_auth", True)
        )

    # Process storage outputs
    if storage_outputs:
        containers = storage_outputs.get("containers", [])
        # Extract container names if they're dicts
        if containers and isinstance(containers[0], dict):
            container_names = [c.get("name", "unnamed") for c in containers]
        else:
            container_names = containers

        config.storage = StorageConfig(
            provider="azure",  # Default for now
            account_name=storage_outputs.get("account_name"),
            connection_string=storage_outputs.get("connection_string"),
            containers=container_names,
            endpoint=storage_outputs.get("primary_endpoint")
        )

    # Save to file
    config_path = get_environments_dir() / f"{env}.yaml"
    config.to_yaml(config_path)

    return config_path


def load_environment_config(env: str) -> Optional[EnvironmentConfig]:
    """Load environment configuration from ~/.modelops/environments/<env>.yaml.

    Args:
        env: Environment name

    Returns:
        EnvironmentConfig if found, None otherwise
    """
    config_path = get_environments_dir() / f"{env}.yaml"
    if not config_path.exists():
        return None

    try:
        return EnvironmentConfig.from_yaml(config_path)
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
    config = EnvironmentConfig(
        environment="local",
        registry=RegistryConfig(
            provider="docker",
            login_server="localhost:5555",
            requires_auth=False
        ),
        storage=StorageConfig(
            provider="azurite",
            connection_string=(
                "DefaultEndpointsProtocol=http;"
                "AccountName=devstoreaccount1;"
                "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
                "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1"
            ),
            containers=["bundles", "results"],
            endpoint="http://127.0.0.1:10000/devstoreaccount1"
        )
    )

    config_path = get_environments_dir() / "local.yaml"
    config.to_yaml(config_path)

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