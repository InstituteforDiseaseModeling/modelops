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
                 registry_stack_ref: Optional[str] = None,
                 opts: Optional[pulumi.ResourceOptions] = None):
        """Initialize Dask workspace deployment.

        Args:
            name: Component name (e.g., "dask")
            infra_stack_ref: Reference to infrastructure stack
            config: Optional workspace configuration
            storage_stack_ref: Optional reference to storage stack for blob access
            registry_stack_ref: Optional reference to registry stack for OCI bundle access
            opts: Optional Pulumi resource options
        """
        super().__init__("modelops:workspace:dask", name, None, opts)
        
        # Create all stack references once at the top to avoid duplicates
        infra = pulumi.StackReference(infra_stack_ref)
        registry = pulumi.StackReference(registry_stack_ref) if registry_stack_ref else None
        storage = pulumi.StackReference(storage_stack_ref) if storage_stack_ref else None

        # Read outputs from Stack 1 (infrastructure)
        # Use get_output to handle missing outputs gracefully
        kubeconfig = infra.get_output("kubeconfig")

        # Check if infrastructure stack has outputs
        def validate_kubeconfig(kc):
            if not kc:
                raise ValueError(
                    f"Infrastructure stack '{infra_stack_ref}' has no kubeconfig output.\n"
                    "This usually means the infrastructure was destroyed or never created.\n"
                    "Please run 'mops infra up' first to create the Kubernetes cluster."
                )
            return kc

        kubeconfig = kubeconfig.apply(validate_kubeconfig)

        # Read outputs from registry stack if provided
        registry_url = None
        if registry:
            registry_url_output = registry.get_output("login_server")

            def validate_registry(url):
                if not url:
                    raise ValueError(
                        f"Registry stack '{registry_stack_ref}' has no login_server output.\n"
                        "This usually means the registry was destroyed or never created.\n"
                        "Please run 'mops registry create' first to create the container registry."
                    )
                return url

            registry_url = registry_url_output.apply(validate_registry)

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
        
        # Initialize variables for conditional resources
        has_registry = bool(registry_stack_ref)
        has_storage = bool(storage_stack_ref)
        bundle_secret = None
        secret_checksum = None
        bundle_registry = None

        # Variables for credentials
        registry_username = None
        registry_password = None
        sas_conn_str = None

        # Get registry outputs if available (using registry reference created at top)
        if has_registry and registry:
            login_server = registry.require_output("login_server")
            # Try to get bundle repo, default to modelops-bundles
            bundle_repo = registry.get_output("bundle_repo")
            if not bundle_repo:
                bundle_repo = pulumi.Output.from_input("modelops-bundles")
            # Try to get credentials - these may not exist if registry was created via infra up
            registry_username = registry.get_output("bundles_pull_username")
            registry_password = registry.get_output("bundles_pull_password")
            # Set bundle registry to just the registry URL (without repo path)
            # The modelops-bundle code will add the repository path as needed
            bundle_registry = login_server

        # Get storage outputs if available (using storage reference created at top)
        if has_storage and storage:
            storage_conn_str = storage.require_output("connection_string")
            storage_account = storage.require_output("account_name")
            # Try to get SAS connection string - may not exist if created via infra up
            sas_conn_str = storage.get_output("sas_connection_string")

            # Create storage secret (kept for backward compatibility)
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

        # Create bundle credentials secret if registry is available AND has credentials
        # (storage is optional)
        if has_registry and registry_username is not None and registry_password is not None:
            import hashlib

            # Build secret data - registry credentials are required, storage is optional
            secret_data = {
                "REGISTRY_USERNAME": registry_username,
                "REGISTRY_PASSWORD": registry_password,
            }

            # Add storage connection string only if storage is configured and SAS exists
            if has_storage and sas_conn_str is not None:
                secret_data["AZURE_STORAGE_CONNECTION_STRING"] = sas_conn_str

            # Compute checksum for pod rollout on secret change
            # Include all available credentials in checksum
            checksum_parts = [registry_username, registry_password]
            if has_storage:
                checksum_parts.append(sas_conn_str)

            secret_checksum = pulumi.Output.all(*checksum_parts).apply(
                lambda args: hashlib.sha256(
                    "".join(str(arg) if arg is not None else "" for arg in args).encode()
                ).hexdigest()[:16]
            )

            # Create the secret directly - Pulumi accepts Output[str] in string_data
            bundle_secret = k8s.core.v1.Secret(
                f"{name}-bundle-credentials",
                metadata=k8s.meta.v1.ObjectMetaArgs(
                    name="bundle-credentials",
                    namespace=namespace
                ),
                string_data=secret_data,
                opts=pulumi.ResourceOptions(
                    provider=k8s_provider,
                    parent=self,
                    depends_on=[ns]
                )
            )

        # Create GitHub credentials secret for private repo access (e.g., modelops-calabaria)
        github_token = os.getenv("GITHUB_TOKEN")
        if github_token:
            github_secret = k8s.core.v1.Secret(
                f"{name}-github-credentials",
                metadata=k8s.meta.v1.ObjectMetaArgs(
                    name="github-credentials",
                    namespace=namespace
                ),
                string_data={
                    "GITHUB_TOKEN": github_token,
                    # For git operations via HTTPS
                    "GIT_USERNAME": "x-access-token",
                    "GIT_PASSWORD": github_token,
                    # For UV to use when installing from private GitHub repos
                    "UV_EXTRA_INDEX_URL": f"git+https://x-access-token:{github_token}@github.com/",
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

        # Compute env and envFrom lists for scheduler and workers
        scheduler_env = []
        worker_env = []
        if bundle_registry is not None:
            scheduler_env.append(k8s.core.v1.EnvVarArgs(
                name="MODELOPS_BUNDLE_REGISTRY",
                value=bundle_registry
            ))
            worker_env.append(k8s.core.v1.EnvVarArgs(
                name="MODELOPS_BUNDLE_REGISTRY",
                value=bundle_registry
            ))

        # Add user-configured env vars
        scheduler_env.extend([k8s.core.v1.EnvVarArgs(**env) for env in scheduler_config.get("env", [])])
        worker_env.extend([k8s.core.v1.EnvVarArgs(**env) for env in workers_config.get("env", [])])

        scheduler_env_from = []
        worker_env_from = []
        if has_storage:
            scheduler_env_from.append(k8s.core.v1.EnvFromSourceArgs(
                secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                    name="modelops-storage",
                    optional=True  # Storage might not be configured
                )
            ))
            worker_env_from.append(k8s.core.v1.EnvFromSourceArgs(
                secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                    name="modelops-storage",
                    optional=True  # Storage might not be configured
                )
            ))

        if bundle_secret is not None:
            scheduler_env_from.append(k8s.core.v1.EnvFromSourceArgs(
                secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                    name="bundle-credentials"
                    # No optional=True - fail fast if missing when expected
                )
            ))
            worker_env_from.append(k8s.core.v1.EnvFromSourceArgs(
                secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                    name="bundle-credentials"
                    # No optional=True - fail fast if missing when expected
                )
            ))

        # Add GitHub credentials for private repo access
        if github_token:
            worker_env_from.append(k8s.core.v1.EnvFromSourceArgs(
                secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                    name="github-credentials",
                    optional=True  # Optional since not all bundles need private deps
                )
            ))

        # Compute annotations for pod checksums
        scheduler_annotations = {}
        worker_annotations = {}
        if secret_checksum is not None:
            scheduler_annotations["checksum/bundle-credentials"] = secret_checksum
            worker_annotations["checksum/bundle-credentials"] = secret_checksum

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
                        },
                        annotations=scheduler_annotations if scheduler_annotations else None
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
                                image_pull_policy="Always" if scheduler_image.endswith(":latest") else "IfNotPresent",
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
                                env=scheduler_env if scheduler_env else None,
                                env_from=scheduler_env_from if scheduler_env_from else None
                            )
                        ],
                        node_selector=scheduler_node_selector if scheduler_node_selector else None,
                        image_pull_secrets=pull_secrets if pull_secrets else None,
                        tolerations=[k8s.core.v1.TolerationArgs(**t) for t in tolerations] if tolerations else None
                    )
                )
            ),
            opts=pulumi.ResourceOptions(
                provider=k8s_provider,
                parent=self,
                depends_on=[ns] + ([bundle_secret] if bundle_secret else [])
            )
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
                        },
                        annotations=worker_annotations if worker_annotations else None
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
                                image_pull_policy="Always" if worker_image.endswith(":latest") else "IfNotPresent",
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
                                env=worker_env if worker_env else None,
                                env_from=worker_env_from if worker_env_from else None
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
                depends_on=[scheduler] + ([bundle_secret] if bundle_secret else [])
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
