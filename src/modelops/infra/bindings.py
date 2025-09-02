"""Infrastructure bindings for cross-plane communication.

These bindings are the typed contracts between infrastructure
planes as described in the architecture.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class DaskBinding:
    """Binding output from workspace plane.
    
    Contains Dask cluster connection information.
    Passed from Workspace Plane to Adaptive Plane.
    """
    scheduler_address: str
    dashboard_url: str
    namespace: str


@dataclass(frozen=True)
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