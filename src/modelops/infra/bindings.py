"""Infrastructure bindings for cross-plane communication.

These bindings are the typed contracts between infrastructure
planes as described in the architecture.
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional


@dataclass
class ClusterBinding:
    """Binding output from infrastructure plane.
    
    Contains all necessary information for Kubernetes operations.
    Passed from Infra Plane to Workspace Plane.
    """
    kubeconfig: str
    provider: str
    cluster_name: str
    resource_group: str
    location: str
    acr_login_server: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for state storage."""
        return {
            "kubeconfig": self.kubeconfig,
            "provider": self.provider,
            "cluster_name": self.cluster_name,
            "resource_group": self.resource_group,
            "location": self.location,
            "acr_login_server": self.acr_login_server
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClusterBinding":
        """Create from dictionary."""
        return cls(
            kubeconfig=data["kubeconfig"],
            provider=data.get("provider", "unknown"),
            cluster_name=data.get("cluster_name", "unknown"),
            resource_group=data.get("resource_group", "unknown"),
            location=data.get("location", "unknown"),
            acr_login_server=data.get("acr_login_server")
        )


@dataclass
class DaskBinding:
    """Binding output from workspace plane.
    
    Contains Dask cluster connection information.
    Passed from Workspace Plane to Adaptive Plane.
    """
    scheduler_address: str
    dashboard_url: str
    namespace: str
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for state storage."""
        return {
            "scheduler_address": self.scheduler_address,
            "dashboard_url": self.dashboard_url,
            "namespace": self.namespace
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DaskBinding":
        """Create from dictionary."""
        return cls(
            scheduler_address=data["scheduler_address"],
            dashboard_url=data["dashboard_url"],
            namespace=data["namespace"]
        )


@dataclass
class PostgresBinding:
    """Binding for Postgres database connection.
    
    Contains database connection information for
    distributed coordination (e.g., Optuna).
    """
    connection_string: str
    host: str
    port: int
    database: str
    username: str
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for state storage."""
        return {
            "connection_string": self.connection_string,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "username": self.username
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PostgresBinding":
        """Create from dictionary."""
        return cls(
            connection_string=data["connection_string"],
            host=data["host"],
            port=data.get("port", 5432),
            database=data["database"],
            username=data["username"]
        )