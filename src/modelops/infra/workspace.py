"""Pulumi stack for provisioning Dask workspaces on Kubernetes."""

import pulumi
import pulumi_kubernetes as k8s
from dataclasses import dataclass, asdict
from typing import Optional, Any, Dict

from .config import WorkspaceConfig


@dataclass
class WorkspaceOutputs:
    """Outputs from a provisioned workspace.
    
    These are the values needed to connect to and use the workspace.
    """
    name: str
    namespace: str
    scheduler_address: str  # Internal cluster address
    dashboard_hint: str  # Instructions for port-forwarding
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for state storage."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkspaceOutputs":
        """Create from dictionary."""
        return cls(**data)


class WorkspaceStack:
    """Pulumi program for provisioning a Dask workspace on Kubernetes.
    
    This creates:
    - Namespace for isolation
    - Dask scheduler deployment and services
    - Dask worker deployment (fixed scale for MVP)
    - Optional storage secrets from provider
    """
    
    def __init__(self, config: WorkspaceConfig, provider: Any):
        """Initialize workspace stack.
        
        Args:
            config: Workspace configuration
            provider: Cloud provider instance
        """
        self.config = config
        self.provider = provider
    
    def create_program(self):
        """Create and return the Pulumi program function.
        
        Returns:
            A function that defines all Pulumi resources
        """
        def program():
            # Get K8s provider from cloud provider (None for local)
            k8s_provider = self.provider.get_k8s_provider()
            opts = pulumi.ResourceOptions(provider=k8s_provider) if k8s_provider else None
            
            # 1. Create namespace
            ns = self._create_namespace(opts)
            
            # 2. Create storage secrets if provider has them
            storage_secret = self._create_storage_secret(ns, opts)
            
            # 3. Create Dask scheduler deployment
            scheduler = self._create_scheduler(ns, storage_secret, opts)
            
            # 4. Create services (scheduler comm + dashboard)
            scheduler_svc = self._create_scheduler_service(ns, opts)
            dashboard_svc = self._create_dashboard_service(ns, opts)
            
            # 5. Create Dask workers (fixed scale for MVP)
            workers = self._create_workers(ns, storage_secret, opts)
            
            # 6. Export outputs (no secrets!)
            pulumi.export("name", self.config.name)
            pulumi.export("namespace", self.config.namespace)
            pulumi.export("scheduler_address", 
                         pulumi.Output.concat(
                             "tcp://dask-scheduler.",
                             self.config.namespace,
                             ".svc.cluster.local:8786"
                         ))
            pulumi.export("dashboard_hint",
                         pulumi.Output.concat(
                             "Run: mops workspace port-forward --name ",
                             self.config.name,
                             " then open http://localhost:8787"
                         ))
            
        return program
    
    def _create_namespace(self, opts: Optional[pulumi.ResourceOptions]) -> k8s.core.v1.Namespace:
        """Create Kubernetes namespace for the workspace."""
        return k8s.core.v1.Namespace(
            f"{self.config.name}-ns",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=self.config.namespace,
                labels={
                    "app.kubernetes.io/managed-by": "modelops",
                    "modelops.io/workspace": self.config.name
                }
            ),
            opts=opts
        )
    
    def _create_storage_secret(
        self, 
        ns: k8s.core.v1.Namespace, 
        opts: Optional[pulumi.ResourceOptions]
    ) -> Optional[k8s.core.v1.Secret]:
        """Create storage secret if provider has storage configuration."""
        storage_data = self.provider.setup_storage()
        if not storage_data.get("secret_data"):
            return None
        
        # Mark all secret values as secret to prevent leaking
        secret_map = {}
        for key, value in storage_data["secret_data"].items():
            if isinstance(value, str):
                secret_map[key] = pulumi.Output.secret(value)
            else:
                # Already a Pulumi Output (from Azure provider)
                secret_map[key] = value
        
        return k8s.core.v1.Secret(
            f"{self.config.name}-storage",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=ns.metadata.name,
                name="storage-secret"
            ),
            string_data=secret_map,
            opts=opts
        )
    
    def _create_scheduler(
        self,
        ns: k8s.core.v1.Namespace,
        storage_secret: Optional[k8s.core.v1.Secret],
        opts: Optional[pulumi.ResourceOptions]
    ) -> k8s.apps.v1.Deployment:
        """Create Dask scheduler deployment."""
        env = []
        if storage_secret:
            env.append(
                k8s.core.v1.EnvVarArgs(
                    name="AZURE_STORAGE_CONNECTION",
                    value_from=k8s.core.v1.EnvVarSourceArgs(
                        secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(
                            name=storage_secret.metadata.name,
                            key="connection_string"
                        )
                    )
                )
            )
        
        return k8s.apps.v1.Deployment(
            f"{self.config.name}-scheduler",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=ns.metadata.name,
                name="dask-scheduler",
                labels={"app": "dask-scheduler", "component": "scheduler"}
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(
                    match_labels={"app": "dask-scheduler"}
                ),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels={"app": "dask-scheduler", "component": "scheduler"}
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="scheduler",
                                image=self.config.image,
                                image_pull_policy="IfNotPresent",
                                command=["dask-scheduler"],
                                ports=[
                                    k8s.core.v1.ContainerPortArgs(
                                        container_port=8786, name="comm"
                                    ),
                                    k8s.core.v1.ContainerPortArgs(
                                        container_port=8787, name="dashboard"
                                    )
                                ],
                                env=env if env else None,
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests={
                                        "memory": self.config.scheduler_memory,
                                        "cpu": self.config.scheduler_cpu
                                    },
                                    limits={
                                        "memory": self.config.scheduler_memory,
                                        "cpu": self.config.scheduler_cpu
                                    }
                                )
                            )
                        ]
                    )
                )
            ),
            opts=opts
        )
    
    def _create_scheduler_service(
        self,
        ns: k8s.core.v1.Namespace,
        opts: Optional[pulumi.ResourceOptions]
    ) -> k8s.core.v1.Service:
        """Create service for Dask scheduler communication (port 8786)."""
        return k8s.core.v1.Service(
            f"{self.config.name}-scheduler-svc",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=ns.metadata.name,
                name="dask-scheduler",
                labels={"app": "dask-scheduler", "component": "scheduler"}
            ),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector={"app": "dask-scheduler"},
                ports=[
                    k8s.core.v1.ServicePortArgs(
                        name="comm",
                        port=8786,
                        target_port=8786
                    )
                ],
                type="ClusterIP"
            ),
            opts=opts
        )
    
    def _create_dashboard_service(
        self,
        ns: k8s.core.v1.Namespace,
        opts: Optional[pulumi.ResourceOptions]
    ) -> k8s.core.v1.Service:
        """Create service for Dask dashboard (port 8787).
        
        This is a separate service to make port-forwarding easier.
        """
        return k8s.core.v1.Service(
            f"{self.config.name}-dashboard-svc",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=ns.metadata.name,
                name="dask-dashboard",
                labels={"app": "dask", "component": "dashboard"}
            ),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector={"app": "dask-scheduler"},  # Points to scheduler pod
                ports=[
                    k8s.core.v1.ServicePortArgs(
                        name="dashboard",
                        port=8787,
                        target_port=8787
                    )
                ],
                type="ClusterIP"
            ),
            opts=opts
        )
    
    def _create_workers(
        self,
        ns: k8s.core.v1.Namespace,
        storage_secret: Optional[k8s.core.v1.Secret],
        opts: Optional[pulumi.ResourceOptions]
    ) -> k8s.apps.v1.Deployment:
        """Create Dask workers deployment.
        
        For MVP, this uses a fixed replica count. HPA can be added later
        when metrics-server is available.
        """
        env = []
        if storage_secret:
            env.append(
                k8s.core.v1.EnvVarArgs(
                    name="AZURE_STORAGE_CONNECTION",
                    value_from=k8s.core.v1.EnvVarSourceArgs(
                        secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(
                            name=storage_secret.metadata.name,
                            key="connection_string"
                        )
                    )
                )
            )
        
        return k8s.apps.v1.Deployment(
            f"{self.config.name}-workers",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=ns.metadata.name,
                name="dask-workers",
                labels={"app": "dask-workers", "component": "worker"}
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=self.config.min_workers,  # Fixed scale for MVP
                selector=k8s.meta.v1.LabelSelectorArgs(
                    match_labels={"app": "dask-workers"}
                ),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels={"app": "dask-workers", "component": "worker"}
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="worker",
                                image=self.config.image,
                                image_pull_policy="IfNotPresent",
                                command=["dask-worker", "tcp://dask-scheduler:8786"],
                                env=env if env else None,
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests={
                                        "memory": self.config.worker_memory,
                                        "cpu": self.config.worker_cpu
                                    },
                                    limits={
                                        "memory": self.config.worker_memory,
                                        "cpu": self.config.worker_cpu
                                    }
                                )
                            )
                        ]
                    )
                )
            ),
            opts=opts
        )
