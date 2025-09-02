"""Workspace plane ComponentResource for Dask deployment.

Deploys Dask scheduler and workers on existing Kubernetes cluster.
"""

import pulumi
import pulumi_kubernetes as k8s
from typing import Dict, Any, Optional


class DaskWorkspace(pulumi.ComponentResource):
    """Stack 2: Workspace plane - deploys Dask on existing cluster.
    
    Uses kubeconfig from infrastructure stack to deploy Dask components.
    Exports scheduler address for adaptive plane consumption.
    """
    
    def __init__(self, name: str, infra_stack_ref: str,
                 config: Optional[Dict[str, Any]] = None,
                 opts: Optional[pulumi.ResourceOptions] = None):
        """Initialize Dask workspace deployment.
        
        Args:
            name: Component name (e.g., "dask")
            infra_stack_ref: Reference to infrastructure stack
            config: Optional workspace configuration
            opts: Optional Pulumi resource options
        """
        super().__init__("modelops:workspace:dask", name, None, opts)
        
        # Read outputs from Stack 1 (infrastructure)
        infra = pulumi.StackReference(infra_stack_ref)
        kubeconfig = infra.require_output("kubeconfig")
        
        # Create K8s provider using kubeconfig from Stack 1
        k8s_provider = k8s.Provider(
            f"{name}-k8s",
            kubeconfig=kubeconfig,
            opts=pulumi.ResourceOptions(parent=self)
        )
        
        # Default configuration
        config = config or {}
        namespace = config.get("namespace", "modelops-dask")
        image = config.get("image", "ghcr.io/dask/dask:latest")
        worker_count = config.get("worker_count", 3)
        
        # Create namespace
        ns = k8s.core.v1.Namespace(
            f"{name}-namespace",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=namespace,
                labels={
                    "modelops.io/component": "workspace",
                    "modelops.io/workspace": name
                }
            ),
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Create Dask scheduler deployment
        scheduler = k8s.apps.v1.Deployment(
            f"{name}-scheduler",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=namespace,
                name="dask-scheduler",
                labels={
                    "modelops.io/component": "scheduler",
                    "app": "dask-scheduler"
                }
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(
                    match_labels={"app": "dask-scheduler"}
                ),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels={
                            "app": "dask-scheduler",
                            "modelops.io/component": "scheduler"
                        }
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="scheduler",
                                image=image,
                                command=["dask-scheduler"],
                                ports=[
                                    k8s.core.v1.ContainerPortArgs(
                                        container_port=8786,
                                        name="scheduler"
                                    ),
                                    k8s.core.v1.ContainerPortArgs(
                                        container_port=8787,
                                        name="dashboard"
                                    )
                                ],
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests={
                                        "memory": config.get("scheduler_memory", "2Gi"),
                                        "cpu": config.get("scheduler_cpu", "1")
                                    },
                                    limits={
                                        "memory": config.get("scheduler_memory", "2Gi"),
                                        "cpu": config.get("scheduler_cpu", "1")
                                    }
                                ),
                                env=[
                                    k8s.core.v1.EnvVarArgs(
                                        name="DASK_SCHEDULER__DASHBOARD__ENABLED",
                                        value="true"
                                    )
                                ]
                            )
                        ]
                    )
                )
            ),
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Create scheduler service
        scheduler_svc = k8s.core.v1.Service(
            f"{name}-scheduler-svc",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=namespace,
                name="dask-scheduler",
                labels={
                    "modelops.io/component": "scheduler",
                    "app": "dask-scheduler"
                }
            ),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector={"app": "dask-scheduler"},
                ports=[
                    k8s.core.v1.ServicePortArgs(
                        port=8786,
                        target_port=8786,
                        name="scheduler"
                    ),
                    k8s.core.v1.ServicePortArgs(
                        port=8787,
                        target_port=8787,
                        name="dashboard"
                    )
                ],
                type="ClusterIP"
            ),
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Create Dask workers deployment
        workers = k8s.apps.v1.Deployment(
            f"{name}-workers",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=namespace,
                name="dask-workers",
                labels={
                    "modelops.io/component": "workers",
                    "app": "dask-worker"
                }
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=worker_count,
                selector=k8s.meta.v1.LabelSelectorArgs(
                    match_labels={"app": "dask-worker"}
                ),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels={
                            "app": "dask-worker",
                            "modelops.io/component": "worker"
                        }
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="worker",
                                image=image,
                                command=[
                                    "dask-worker",
                                    "tcp://dask-scheduler:8786",
                                    "--nthreads", str(config.get("worker_threads", 2)),
                                    "--memory-limit", config.get("worker_memory", "4Gi")
                                ],
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests={
                                        "memory": config.get("worker_memory", "4Gi"),
                                        "cpu": config.get("worker_cpu", "2")
                                    },
                                    limits={
                                        "memory": config.get("worker_memory", "4Gi"),
                                        "cpu": config.get("worker_cpu", "2")
                                    }
                                ),
                                env=[
                                    k8s.core.v1.EnvVarArgs(
                                        name="DASK_WORKER__MEMORY__TARGET",
                                        value="0.90"  # Spill to disk at 90% memory
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="DASK_WORKER__MEMORY__SPILL",
                                        value="0.95"
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="DASK_WORKER__MEMORY__PAUSE",
                                        value="0.98"
                                    )
                                ]
                            )
                        ]
                    )
                )
            ),
            opts=pulumi.ResourceOptions(
                provider=k8s_provider, 
                parent=self,
                depends_on=[scheduler]  # Workers depend on scheduler
            )
        )
        
        # Build connection strings
        scheduler_address = pulumi.Output.concat(
            "tcp://dask-scheduler.", namespace, ":8786"
        )
        dashboard_url = pulumi.Output.concat(
            "http://dask-scheduler.", namespace, ":8787"
        )
        
        # Store outputs for reference
        self.scheduler_address = scheduler_address
        self.dashboard_url = dashboard_url
        self.namespace = pulumi.Output.from_input(namespace)
        self.worker_count = pulumi.Output.from_input(worker_count)
        
        # Register outputs for Stack 3 to use via StackReference
        self.register_outputs({
            "scheduler_address": scheduler_address,
            "dashboard_url": dashboard_url,
            "namespace": namespace,
            "worker_count": worker_count,
            "image": image
        })