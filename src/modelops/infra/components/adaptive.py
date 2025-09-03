"""Adaptive plane ComponentResource for optimization runs.

Deploys adaptive workers that connect to Dask for distributed optimization.
"""

import pulumi
import pulumi_kubernetes as k8s
import secrets
import string
from typing import Dict, Any, Optional


class AdaptiveRun(pulumi.ComponentResource):
    """Stack 3: Adaptive plane - runs optimization workloads.
    
    Uses kubeconfig from infrastructure stack and scheduler address from
    workspace stack to deploy adaptive optimization workers.
    """
    
    def __init__(self, name: str, 
                 infra_stack_ref: str,
                 workspace_stack_ref: str,
                 config: Dict[str, Any],
                 opts: Optional[pulumi.ResourceOptions] = None):
        """Initialize adaptive optimization run.
        
        Args:
            name: Run identifier (e.g., "run-20240101-123456")
            infra_stack_ref: Reference to infrastructure stack
            workspace_stack_ref: Reference to workspace stack
            config: Run configuration including algorithm settings
            opts: Optional Pulumi resource options
        """
        super().__init__("modelops:adaptive:run", name, None, opts)
        
        # Read from Stack 1 (infrastructure)
        infra = pulumi.StackReference(infra_stack_ref)
        kubeconfig = infra.require_output("kubeconfig")
        
        # Read from Stack 2 (workspace)
        workspace = pulumi.StackReference(workspace_stack_ref)
        scheduler_address = workspace.require_output("scheduler_address")
        dask_namespace = workspace.require_output("namespace")
        
        # Create K8s provider
        k8s_provider = k8s.Provider(
            f"{name}-k8s",
            kubeconfig=kubeconfig,
            opts=pulumi.ResourceOptions(parent=self)
        )
        
        # Create namespace for this run
        run_namespace = f"adaptive-{name}"
        ns = k8s.core.v1.Namespace(
            f"{name}-namespace",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=run_namespace,
                labels={
                    "modelops.io/component": "adaptive",
                    "modelops.io/run-id": name,
                    "modelops.io/algorithm": config.get("algorithm", "optuna")
                }
            ),
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Create ConfigMap with run configuration
        config_map = k8s.core.v1.ConfigMap(
            f"{name}-config",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=run_namespace,
                name="run-config"
            ),
            data={
                "RUN_ID": name,
                "ALGORITHM": config.get("algorithm", "optuna"),
                "N_TRIALS": str(config.get("n_trials", 100)),
                "N_PARALLEL": str(config.get("n_parallel", 10)),
                "TIMEOUT_SECONDS": str(config.get("timeout_seconds", 3600)),
                "DASK_SCHEDULER": scheduler_address,
                "TARGET_FUNCTION": config.get("target_function", ""),
                "BUNDLE_REF": config.get("bundle_ref", "")
            },
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Optional: Create Postgres for Optuna storage
        postgres_dsn = None
        if config.get("algorithm") == "optuna" and config.get("use_postgres", True):
            postgres_dsn = self._create_postgres(name, run_namespace, k8s_provider)
        
        # Create Secret for sensitive data
        secret = k8s.core.v1.Secret(
            f"{name}-secret",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=run_namespace,
                name="run-secrets"
            ),
            type="Opaque",
            string_data={
                "POSTGRES_DSN": postgres_dsn or "",
                "STORAGE_CONNECTION": config.get("storage_connection", "")
            },
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Create adaptive worker Job
        job = k8s.batch.v1.Job(
            f"{name}-adaptive",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=run_namespace,
                name=f"adaptive-{name}",
                labels={
                    "modelops.io/component": "adaptive-worker",
                    "modelops.io/run-id": name
                }
            ),
            spec=k8s.batch.v1.JobSpecArgs(
                parallelism=config.get("n_parallel", 10),
                completions=config.get("n_trials", 100),
                backoff_limit=config.get("backoff_limit", 3),
                active_deadline_seconds=config.get("timeout_seconds", 3600),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels={
                            "modelops.io/component": "adaptive-worker",
                            "modelops.io/run-id": name,
                            "job-name": f"adaptive-{name}"
                        }
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        restart_policy="OnFailure",
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="adaptive-worker",
                                image=config.get("image", "ghcr.io/modelops/adaptive:latest"),
                                command=config.get("command", [
                                    "python", "-m", "modelops.adaptive.worker"
                                ]),
                                env_from=[
                                    k8s.core.v1.EnvFromSourceArgs(
                                        config_map_ref=k8s.core.v1.ConfigMapEnvSourceArgs(
                                            name="run-config"
                                        )
                                    ),
                                    k8s.core.v1.EnvFromSourceArgs(
                                        secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                                            name="run-secrets"
                                        )
                                    )
                                ],
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests={
                                        "memory": config.get("worker_memory", "2Gi"),
                                        "cpu": config.get("worker_cpu", "1")
                                    },
                                    limits={
                                        "memory": config.get("worker_memory", "2Gi"),
                                        "cpu": config.get("worker_cpu", "1")
                                    }
                                ),
                                volume_mounts=[
                                    k8s.core.v1.VolumeMountArgs(
                                        name="workspace",
                                        mount_path="/workspace"
                                    )
                                ] if config.get("use_workspace_volume", False) else []
                            )
                        ],
                        volumes=[
                            k8s.core.v1.VolumeArgs(
                                name="workspace",
                                empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs(
                                    size_limit="10Gi"
                                )
                            )
                        ] if config.get("use_workspace_volume", False) else []
                    )
                )
            ),
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Store outputs
        self.run_id = pulumi.Output.from_input(name)
        self.namespace = pulumi.Output.from_input(run_namespace)
        self.scheduler_address = scheduler_address
        self.postgres_dsn = postgres_dsn
        self.job_name = job.metadata.name
        
        # Register outputs
        self.register_outputs({
            "run_id": name,
            "namespace": run_namespace,
            "scheduler_address": scheduler_address,
            "postgres_dsn": postgres_dsn if postgres_dsn else None,
            "job_name": f"adaptive-{name}",
            "algorithm": config.get("algorithm", "optuna"),
            "n_trials": config.get("n_trials", 100),
            "status": "running"
        })
    
    def _create_postgres(self, name: str, namespace: str, 
                        k8s_provider) -> pulumi.Output[str]:
        """Create Postgres deployment for Optuna distributed storage.
        
        Args:
            name: Run identifier
            namespace: Kubernetes namespace
            k8s_provider: Kubernetes provider
            
        Returns:
            PostgreSQL connection string
        """
        # Generate secure random password
        alphabet = string.ascii_letters + string.digits
        postgres_password = ''.join(secrets.choice(alphabet) for _ in range(24))
        # Create PVC for Postgres data
        pvc = k8s.core.v1.PersistentVolumeClaim(
            f"{name}-postgres-pvc",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=namespace,
                name="postgres-data"
            ),
            spec=k8s.core.v1.PersistentVolumeClaimSpecArgs(
                access_modes=["ReadWriteOnce"],
                resources=k8s.core.v1.ResourceRequirementsArgs(
                    requests={
                        "storage": "10Gi"
                    }
                )
            ),
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Create Postgres deployment
        postgres = k8s.apps.v1.Deployment(
            f"{name}-postgres",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=namespace,
                name="postgres",
                labels={
                    "modelops.io/component": "postgres",
                    "modelops.io/run-id": name
                }
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(
                    match_labels={"app": "postgres"}
                ),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels={
                            "app": "postgres",
                            "modelops.io/component": "postgres"
                        }
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="postgres",
                                image="postgres:14-alpine",
                                env=[
                                    k8s.core.v1.EnvVarArgs(
                                        name="POSTGRES_DB",
                                        value="optuna"
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="POSTGRES_USER",
                                        value="optuna"
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="POSTGRES_PASSWORD",
                                        value=postgres_password
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="PGDATA",
                                        value="/var/lib/postgresql/data/pgdata"
                                    )
                                ],
                                ports=[
                                    k8s.core.v1.ContainerPortArgs(
                                        container_port=5432,
                                        name="postgres"
                                    )
                                ],
                                volume_mounts=[
                                    k8s.core.v1.VolumeMountArgs(
                                        name="postgres-storage",
                                        mount_path="/var/lib/postgresql/data"
                                    )
                                ],
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests={
                                        "memory": "512Mi",
                                        "cpu": "500m"
                                    },
                                    limits={
                                        "memory": "1Gi",
                                        "cpu": "1"
                                    }
                                )
                            )
                        ],
                        volumes=[
                            k8s.core.v1.VolumeArgs(
                                name="postgres-storage",
                                persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                                    claim_name="postgres-data"
                                )
                            )
                        ]
                    )
                )
            ),
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Create Postgres service
        postgres_svc = k8s.core.v1.Service(
            f"{name}-postgres-svc",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=namespace,
                name="postgres",
                labels={
                    "modelops.io/component": "postgres",
                    "modelops.io/run-id": name
                }
            ),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector={"app": "postgres"},
                ports=[
                    k8s.core.v1.ServicePortArgs(
                        port=5432,
                        target_port=5432,
                        name="postgres"
                    )
                ],
                type="ClusterIP"
            ),
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Build connection string with secure password
        return pulumi.Output.concat(
            "postgresql://optuna:",
            pulumi.Output.secret(postgres_password),
            "@postgres.",
            namespace,
            ":5432/optuna"
        )