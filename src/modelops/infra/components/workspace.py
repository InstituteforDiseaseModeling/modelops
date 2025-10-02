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
from ...versions import DASK_IMAGE
from .smoke_test import SmokeTest


class DaskWorkspace(pulumi.ComponentResource):
    """Stack 2: Workspace plane - deploys Dask on existing cluster.
    
    Uses kubeconfig from infrastructure stack to deploy Dask components.
    Exports scheduler address for adaptive plane consumption.
    """
    
    def __init__(self, name: str, infra_stack_ref: str,
                 config: Optional[Dict[str, Any]] = None,
                 storage_stack_ref: Optional[str] = None,
                 opts: Optional[pulumi.ResourceOptions] = None):
        """Initialize Dask workspace deployment.
        
        Args:
            name: Component name (e.g., "dask")
            infra_stack_ref: Reference to infrastructure stack
            config: Optional workspace configuration
            storage_stack_ref: Optional reference to storage stack for blob access
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
        
        # Extract environment from config (passed from CLI)
        # Don't try to parse from stack ref as it contains full path
        env = config.get("environment", "dev")
        
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
        
        # Extract configuration values - images are REQUIRED, no defaults
        scheduler_image = scheduler_config.get("image")
        if not scheduler_image:
            raise ValueError("scheduler.image is required in workspace configuration")
        worker_image = workers_config.get("image")
        if not worker_image:
            raise ValueError("workers.image is required in workspace configuration")
        worker_count = workers_config.get("replicas", config.get("worker_count", 3))
        worker_processes = workers_config.get("processes", 1)  # Default to 1 process
        worker_threads = workers_config.get("threads", 2)  # Default to 2 threads
        
        # Convert K8s memory format to Dask format
        # Dask expects GiB/MiB/GB/MB notation
        memory_limit = workers_config.get("resources", {}).get("limits", {}).get("memory", "4Gi")
        
        # Handle various memory format suffixes
        memory_conversions = {
            "Gi": "GiB",   # K8s Gi -> Dask GiB
            "Mi": "MiB",   # K8s Mi -> Dask MiB  
            "Ki": "KiB",   # K8s Ki -> Dask KiB
            "G": "GB",     # G -> GB (already Dask compatible)
            "M": "MB",     # M -> MB (already Dask compatible)
            "K": "KB",     # K -> KB (already Dask compatible)
        }
        
        for k8s_suffix, dask_suffix in memory_conversions.items():
            if memory_limit.endswith(k8s_suffix):
                memory_limit = memory_limit[:-len(k8s_suffix)] + dask_suffix
                break
        
        # Adjust memory limit per process if using multiple processes
        if worker_processes > 1:
            # Parse memory value and unit
            import re
            match = re.match(r'^(\d+(?:\.\d+)?)\s*([A-Za-z]+)$', memory_limit)
            if match:
                value, unit = match.groups()
                per_process_value = float(value) / worker_processes
                memory_limit = f"{per_process_value:.1f}{unit}"
        
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
        
        # Create storage secret if storage stack is referenced
        if storage_stack_ref:
            storage = pulumi.StackReference(storage_stack_ref)
            storage_conn_str = storage.require_output("connection_string")
            storage_account = storage.require_output("account_name")
            
            k8s.core.v1.Secret(
                f"{name}-storage-secret",
                metadata=k8s.meta.v1.ObjectMetaArgs(
                    name="modelops-storage",
                    namespace=namespace
                ),
                string_data={
                    "AZURE_STORAGE_CONNECTION_STRING": storage_conn_str,
                    "AZURE_STORAGE_ACCOUNT": storage_account
                },
                opts=pulumi.ResourceOptions(
                    provider=k8s_provider,
                    parent=self,
                    depends_on=[ns]
                )
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
                        # Security: Run containers as non-root to prevent privilege escalation
                        # Default K8s behavior runs as root (uid 0), enabling container escape attacks
                        security_context=k8s.core.v1.PodSecurityContextArgs(
                            run_as_non_root=True,
                            run_as_user=1000,
                            fs_group=1000
                        ),
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="scheduler",
                                image=scheduler_image,
                                command=["dask-scheduler"],
                                # Security: Drop all Linux capabilities to minimize attack surface
                                # Dask doesn't need special kernel capabilities for normal operation
                                security_context=k8s.core.v1.SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    run_as_non_root=True,
                                    run_as_user=1000,
                                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=["ALL"])
                                ),
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
                                env=[k8s.core.v1.EnvVarArgs(**env) for env in scheduler_config.get("env", [])],
                                # Mount storage secret if available
                                env_from=[
                                    k8s.core.v1.EnvFromSourceArgs(
                                        secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                                            name="modelops-storage",
                                            optional=True  # Don't fail if secret doesn't exist
                                        )
                                    )
                                ] if storage_stack_ref else None
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
        
        # Extract autoscaling config if using structured config
        autoscaling_config = None
        if "spec" in config and "autoscaling" in config["spec"]:
            autoscaling_config = config["spec"]["autoscaling"]
        elif "autoscaling" in config:
            autoscaling_config = config["autoscaling"]

        # Default autoscaling settings
        if autoscaling_config is None:
            autoscaling_config = {
                "enabled": True,
                "min_workers": 2,
                "max_workers": 20,
                "target_cpu": 70
            }

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
                replicas=worker_count if not autoscaling_config.get("enabled", True) else autoscaling_config.get("min_workers", 2),
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
                        # Security: Run containers as non-root to prevent privilege escalation
                        # Workers process untrusted code, so security isolation is critical
                        security_context=k8s.core.v1.PodSecurityContextArgs(
                            run_as_non_root=True,
                            run_as_user=1000,
                            fs_group=1000
                        ),
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="worker",
                                image=worker_image,
                                # Security: Drop all capabilities and prevent privilege escalation
                                # Workers don't need kernel capabilities for computation tasks
                                security_context=k8s.core.v1.SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    run_as_non_root=True,
                                    run_as_user=1000,
                                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=["ALL"])
                                ),
                                command=[
                                    "dask-worker",
                                    "tcp://dask-scheduler:8786",
                                    "--nworkers", str(worker_processes),
                                    "--nthreads", str(worker_threads),
                                    "--memory-limit", memory_limit
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
                                env=[
                                    *[k8s.core.v1.EnvVarArgs(**env) for env in workers_config.get("env", [])]
                                ],
                                # Mount storage secret if available
                                env_from=[
                                    k8s.core.v1.EnvFromSourceArgs(
                                        secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                                            name="modelops-storage",
                                            optional=True  # Don't fail if secret doesn't exist
                                        )
                                    )
                                ] if storage_stack_ref else None
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

        # Create HorizontalPodAutoscaler if autoscaling is enabled
        hpa = None
        if autoscaling_config.get("enabled", True):
            hpa = k8s.autoscaling.v2.HorizontalPodAutoscaler(
                f"{name}-workers-hpa",
                metadata=k8s.meta.v1.ObjectMetaArgs(
                    namespace=namespace,
                    name="dask-workers-hpa",
                    labels={
                        "modelops.io/component": "workers",
                        "app": "dask-worker"
                    }
                ),
                spec=k8s.autoscaling.v2.HorizontalPodAutoscalerSpecArgs(
                    scale_target_ref=k8s.autoscaling.v2.CrossVersionObjectReferenceArgs(
                        api_version="apps/v1",
                        kind="Deployment",
                        name="dask-workers"
                    ),
                    min_replicas=autoscaling_config.get("min_workers", 2),
                    max_replicas=autoscaling_config.get("max_workers", 20),
                    metrics=[
                        k8s.autoscaling.v2.MetricSpecArgs(
                            type="Resource",
                            resource=k8s.autoscaling.v2.ResourceMetricSourceArgs(
                                name="cpu",
                                target=k8s.autoscaling.v2.MetricTargetArgs(
                                    type="Utilization",
                                    average_utilization=autoscaling_config.get("target_cpu", 70)
                                )
                            )
                        )
                    ],
                    behavior=k8s.autoscaling.v2.HorizontalPodAutoscalerBehaviorArgs(
                        scale_down=k8s.autoscaling.v2.HPAScalingRulesArgs(
                            stabilization_window_seconds=autoscaling_config.get("scale_down_delay", 300),
                            policies=[
                                k8s.autoscaling.v2.HPAScalingPolicyArgs(
                                    type="Percent",
                                    value=50,  # Scale down by 50% at most
                                    period_seconds=60
                                )
                            ]
                        )
                    )
                ),
                opts=pulumi.ResourceOptions(
                    provider=k8s_provider,
                    parent=self,
                    depends_on=[workers]
                )
            )

        # Build connection strings
        scheduler_address = pulumi.Output.concat(
            "tcp://dask-scheduler.", namespace, ":8786"
        )
        dashboard_url = pulumi.Output.concat(
            "http://dask-scheduler.", namespace, ":8787"
        )
        
        # Run smoke tests if configured (opt-in to prevent deployment failures)
        run_smoke_tests = config.get("smoke_tests", config.get("run_smoke_tests", False))
        if run_smoke_tests:
            # Determine which tests to run
            tests = ["dask"]  # Always test Dask connectivity
            if storage_stack_ref:
                tests.append("storage")  # Test storage if configured
            
            # Environment for smoke test
            test_env = [
                k8s.core.v1.EnvVarArgs(
                    name="DASK_SCHEDULER",
                    value=scheduler_address
                )
            ]
            
            # Add storage env if available
            test_env_from = None
            if storage_stack_ref:
                test_env_from = [
                    k8s.core.v1.EnvFromSourceArgs(
                        secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                            name="modelops-storage",
                            optional=True
                        )
                    )
                ]
            
            smoke_test = SmokeTest(
                "workspace",
                namespace=namespace,
                tests=tests,
                k8s_provider=k8s_provider,
                env=test_env,
                env_from=test_env_from,
                timeout_seconds=60,
                opts=pulumi.ResourceOptions(
                    parent=self,
                    depends_on=[scheduler, scheduler_svc, workers]
                )
            )
            
            # Add smoke test outputs
            self.smoke_test_job = smoke_test.job_name
            self.smoke_test_status = pulumi.Output.from_input("created")
        
        # Store outputs for reference
        self.scheduler_address = scheduler_address
        self.dashboard_url = dashboard_url
        self.namespace = pulumi.Output.from_input(namespace)
        self.worker_count = pulumi.Output.from_input(worker_count if not autoscaling_config.get("enabled", True) else f"{autoscaling_config.get('min_workers', 2)}-{autoscaling_config.get('max_workers', 20)}")
        self.worker_processes = pulumi.Output.from_input(worker_processes)
        self.worker_threads = pulumi.Output.from_input(worker_threads)
        self.autoscaling_enabled = pulumi.Output.from_input(autoscaling_config.get("enabled", True))
        self.autoscaling_min = pulumi.Output.from_input(autoscaling_config.get("min_workers", 2))
        self.autoscaling_max = pulumi.Output.from_input(autoscaling_config.get("max_workers", 20))
        
        # Register outputs for Stack 3 to use via StackReference
        outputs_dict = {
            "scheduler_address": scheduler_address,
            "dashboard_url": dashboard_url,
            "namespace": namespace,
            "worker_count": worker_count,
            "worker_processes": worker_processes,
            "worker_threads": worker_threads,
            "scheduler_image": scheduler_image,
            "worker_image": worker_image,
            # Explicit service details for port-forwarding
            "scheduler_service_name": "dask-scheduler",
            "scheduler_port": 8786,
            "dashboard_port": 8787,
            # Autoscaling info
            "autoscaling_enabled": self.autoscaling_enabled,
            "autoscaling_min": self.autoscaling_min,
            "autoscaling_max": self.autoscaling_max
        }
        
        # Add smoke test outputs if created
        if run_smoke_tests:
            outputs_dict["smoke_test_job"] = self.smoke_test_job
            outputs_dict["smoke_test_status"] = self.smoke_test_status
        
        self.register_outputs(outputs_dict)
