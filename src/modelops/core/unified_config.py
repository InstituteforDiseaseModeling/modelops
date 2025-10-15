"""Unified ModelOps configuration model.

This module provides the unified configuration structure that combines
both general settings (previously in config.yaml) and infrastructure
specifications (previously in infrastructure.yaml) into a single model.
"""

from typing import Optional, List
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel, Field
from ..components.config_base import ConfigModel


class PulumiSettings(BaseModel):
    """Pulumi-specific settings."""
    backend_url: Optional[str] = None
    organization: str = "institutefordiseasemodeling"


class GeneralSettings(BaseModel):
    """General ModelOps settings."""
    environment: str = "dev"
    provider: str = "azure"
    username: str  # Required, set from system user


class NodePoolSpec(BaseModel):
    """Kubernetes node pool specification."""
    name: str
    mode: str  # System or User
    vm_size: str
    count: Optional[int] = None  # For fixed size
    min: Optional[int] = None     # For autoscaling
    max: Optional[int] = None     # For autoscaling


class AKSSpec(BaseModel):
    """Azure Kubernetes Service specification."""
    name: str = "modelops-cluster"
    kubernetes_version: str
    node_pools: List[NodePoolSpec]


class ClusterSpec(BaseModel):
    """Cluster infrastructure specification."""
    provider: str = "azure"
    subscription_id: str
    resource_group: str
    location: str = "eastus2"
    aks: AKSSpec


class StorageSpec(BaseModel):
    """Storage specification."""
    account_tier: str = "Standard"


class RegistrySpec(BaseModel):
    """Container registry specification."""
    sku: str = "Basic"


class WorkspaceSpec(BaseModel):
    """Dask workspace specification."""
    scheduler_image: str = "ghcr.io/vsbuffalo/modelops-dask-scheduler:latest"
    scheduler_replicas: int = 1
    worker_image: str = "ghcr.io/vsbuffalo/modelops-dask-worker:latest"
    worker_replicas: int = 2
    worker_processes: int = 4
    worker_threads: int = 1


class UnifiedModelOpsConfig(ConfigModel):
    """Unified ModelOps configuration.

    This combines all ModelOps configuration into a single model,
    replacing the separate config.yaml and infrastructure.yaml files.
    """
    schema_version: int = 2
    generated: datetime = Field(default_factory=datetime.now)

    # Settings (from config.yaml)
    settings: GeneralSettings
    pulumi: PulumiSettings = Field(default_factory=PulumiSettings)

    # Infrastructure (from infrastructure.yaml)
    cluster: ClusterSpec
    storage: StorageSpec = Field(default_factory=StorageSpec)
    registry: RegistrySpec = Field(default_factory=RegistrySpec)
    workspace: WorkspaceSpec = Field(default_factory=WorkspaceSpec)

    @classmethod
    def get_config_path(cls) -> Path:
        """Get the unified configuration file path.

        Returns:
            Path to ~/.modelops/modelops.yaml
        """
        return Path.home() / ".modelops" / "modelops.yaml"

    @classmethod
    def load(cls) -> "UnifiedModelOpsConfig":
        """Load unified configuration from file.

        Returns:
            UnifiedModelOpsConfig instance loaded from ~/.modelops/modelops.yaml

        Raises:
            FileNotFoundError: If configuration file doesn't exist
        """
        config_path = cls.get_config_path()
        if not config_path.exists():
            raise FileNotFoundError(
                f"Configuration not found at {config_path}\n"
                "Run 'mops init' to create configuration"
            )
        return cls.from_yaml(config_path)

    def save(self) -> None:
        """Save configuration to ~/.modelops/modelops.yaml.

        Creates the .modelops directory if it doesn't exist.
        """
        config_path = self.get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        self.to_yaml(config_path)

    @classmethod
    def from_legacy_configs(cls, config_path: Optional[Path] = None,
                          infra_path: Optional[Path] = None) -> "UnifiedModelOpsConfig":
        """Create unified config from legacy separate files.

        This helps with migration from old format to new unified format.

        Args:
            config_path: Path to config.yaml (default: ~/.modelops/config.yaml)
            infra_path: Path to infrastructure.yaml (default: ~/.modelops/infrastructure.yaml)

        Returns:
            UnifiedModelOpsConfig instance combining both legacy configs
        """
        from .config import ModelOpsConfig
        from ..components.specs.infra import UnifiedInfraSpec
        import getpass

        # Load legacy configs
        config_path = config_path or (Path.home() / ".modelops" / "config.yaml")
        infra_path = infra_path or (Path.home() / ".modelops" / "infrastructure.yaml")

        # Set defaults
        username = getpass.getuser()
        environment = "dev"
        provider = "azure"

        # Try to load old config.yaml
        if config_path.exists():
            old_config = ModelOpsConfig.from_yaml(config_path)
            username = old_config.defaults.username or username
            environment = old_config.defaults.environment
            provider = old_config.defaults.provider
            pulumi_settings = PulumiSettings(
                backend_url=old_config.pulumi.backend_url,
                organization=old_config.pulumi.organization
            )
        else:
            pulumi_settings = PulumiSettings()

        # Try to load old infrastructure.yaml
        if infra_path.exists():
            old_infra = UnifiedInfraSpec.from_yaml(str(infra_path))

            # Convert to new format
            node_pools = []
            if old_infra.cluster and old_infra.cluster.aks:
                for pool in old_infra.cluster.aks.node_pools:
                    node_pools.append(NodePoolSpec(
                        name=pool.name,
                        mode=pool.mode,
                        vm_size=pool.vm_size,
                        count=getattr(pool, 'count', None),
                        min=getattr(pool, 'min', None),
                        max=getattr(pool, 'max', None)
                    ))

            cluster = ClusterSpec(
                provider=old_infra.cluster.provider if old_infra.cluster else "azure",
                subscription_id=old_infra.cluster.subscription_id if old_infra.cluster else "",
                resource_group=old_infra.cluster.resource_group if old_infra.cluster else f"modelops-{username}",
                location=old_infra.cluster.location if old_infra.cluster else "eastus2",
                aks=AKSSpec(
                    name=old_infra.cluster.aks.name if old_infra.cluster and old_infra.cluster.aks else "modelops-cluster",
                    kubernetes_version=old_infra.cluster.aks.kubernetes_version if old_infra.cluster and old_infra.cluster.aks else "1.30",
                    node_pools=node_pools or [
                        NodePoolSpec(name="system", mode="System", vm_size="Standard_B2s", count=1),
                        NodePoolSpec(name="workers", mode="User", vm_size="Standard_B4ms", min=1, max=3)
                    ]
                )
            )

            # Convert workspace if present
            if old_infra.workspace and hasattr(old_infra.workspace, 'spec'):
                workspace = WorkspaceSpec(
                    scheduler_image=old_infra.workspace.spec.scheduler.image,
                    scheduler_replicas=old_infra.workspace.spec.scheduler.replicas,
                    worker_image=old_infra.workspace.spec.workers.image,
                    worker_replicas=old_infra.workspace.spec.workers.replicas,
                    worker_processes=old_infra.workspace.spec.workers.processes,
                    worker_threads=old_infra.workspace.spec.workers.threads
                )
            else:
                workspace = WorkspaceSpec()
        else:
            # No infrastructure.yaml, use defaults
            cluster = ClusterSpec(
                subscription_id="",
                resource_group=f"modelops-{username}",
                aks=AKSSpec(
                    kubernetes_version="1.30",
                    node_pools=[
                        NodePoolSpec(name="system", mode="System", vm_size="Standard_B2s", count=1),
                        NodePoolSpec(name="workers", mode="User", vm_size="Standard_B4ms", min=1, max=3)
                    ]
                )
            )
            workspace = WorkspaceSpec()

        return cls(
            settings=GeneralSettings(
                username=username,
                environment=environment,
                provider=provider
            ),
            pulumi=pulumi_settings,
            cluster=cluster,
            storage=StorageSpec(),
            registry=RegistrySpec(),
            workspace=workspace
        )