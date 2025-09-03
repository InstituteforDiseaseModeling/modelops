"""Workspace plane ComponentResource for Dask deployment.

Deploys Dask scheduler and workers on existing Kubernetes cluster.
"""

import pulumi
import pulumi_kubernetes as k8s
from typing import Dict, Any, Optional, List
import json
import base64
import os
from ...core import StackNaming


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
        
        # Extract environment from config or parse from stack reference
        env = config.get("environment", "dev")
        if "-" in infra_stack_ref:
            # Try to parse environment from stack name
            parsed = StackNaming.parse_stack_name(infra_stack_ref)
            if "env" in parsed:
                env = parsed["env"]
        
        # Parse metadata and spec if structured
        if "metadata" in config and "spec" in config:
            metadata = config["metadata"]
            spec = config["spec"]
            # Use centralized naming for default namespace
            namespace = metadata.get("namespace", StackNaming.get_namespace("dask", env))
            scheduler_config = spec.get("scheduler", {})
            workers_config = spec.get("workers", {})
        else:
            # Fallback to flat config with centralized naming
            namespace = config.get("namespace", StackNaming.get_namespace("dask", env))
            scheduler_config = config
            workers_config = config
        
        # Extract configuration values
        # Use specific version that matches our client (2024.8.0)
        scheduler_image = scheduler_config.get("image", "ghcr.io/dask/dask:2024.8.0")
        worker_image = workers_config.get("image", scheduler_image)
        worker_count = workers_config.get("replicas", config.get("worker_count", 3))
        
        # Node selectors
        scheduler_node_selector = scheduler_config.get("nodeSelector", {})
        worker_node_selector = workers_config.get("nodeSelector", {})
        
        # Tolerations for tainted nodes
        tolerations = config.get("spec", {}).get("tolerations", [])
        # Add default toleration for modelops.io/role taint
        if not tolerations:
            tolerations = [
                {"key": "modelops.io/role", "operator": "Equal", "value": "cpu", "effect": "NoSchedule"}
            ]
        
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
        
        # No image pull secrets needed for public GHCR images
        pull_secrets = []
        
        # Optional: Could still support private registries if needed
        # ghcr_pat = os.getenv("GHCR_PAT")
        # if ghcr_pat and "ghcr.io" in scheduler_image:
        #     ... (secret creation code)
        
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
                                image=scheduler_image,
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
                                    requests=scheduler_config.get("resources", {}).get("requests", {
                                        "memory": "2Gi",
                                        "cpu": "1"
                                    }),
                                    limits=scheduler_config.get("resources", {}).get("limits", {
                                        "memory": "2Gi",
                                        "cpu": "1"
                                    })
                                ),
                                env=[k8s.core.v1.EnvVarArgs(**env) for env in scheduler_config.get("env", [])]
                            )
                        ],
                        node_selector=scheduler_node_selector if scheduler_node_selector else None,
                        image_pull_secrets=pull_secrets if pull_secrets else None,
                        tolerations=[k8s.core.v1.TolerationArgs(**t) for t in tolerations] if tolerations else None
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
                                image=worker_image,
                                command=[
                                    "dask-worker",
                                    "tcp://dask-scheduler:8786",
                                    "--nthreads", str(workers_config.get("threads", 2)),
                                    "--memory-limit", workers_config.get("resources", {}).get("limits", {}).get("memory", "4Gi")
                                ],
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests=workers_config.get("resources", {}).get("requests", {
                                        "memory": "4Gi",
                                        "cpu": "2"
                                    }),
                                    limits=workers_config.get("resources", {}).get("limits", {
                                        "memory": "4Gi",
                                        "cpu": "2"
                                    })
                                ),
                                env=[k8s.core.v1.EnvVarArgs(**env) for env in workers_config.get("env", [])]
                            )
                        ],
                        node_selector=worker_node_selector if worker_node_selector else None,
                        image_pull_secrets=pull_secrets if pull_secrets else None,
                        tolerations=[k8s.core.v1.TolerationArgs(**t) for t in tolerations] if tolerations else None
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
            "scheduler_image": scheduler_image,
            "worker_image": worker_image
        })