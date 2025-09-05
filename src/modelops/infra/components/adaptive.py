"""Adaptive plane ComponentResource for optimization infrastructure.

Provisions stateful components needed by optimization algorithms.

TODO: some of the wiring here is too specific to Postgres and not general enough to 
support other central stores. Refactor to be more generic.
"""

import pulumi
import pulumi_kubernetes as k8s
from typing import Dict, Any, Optional
from ...core import StackNaming
from ...versions import POSTGRES_IMAGE
from .smoke_test import SmokeTest


class AdaptiveInfra(pulumi.ComponentResource):
    """Stack 4: Adaptive plane - infrastructure for optimization algorithms.
    
    Provisions stateful components (databases, caches, queues) that
    optimization algorithms require. These are long-lived infrastructure
    components, not ephemeral runs.
    """
    
    def __init__(self, name: str, 
                 infra_stack_ref: str,
                 workspace_stack_ref: str,
                 config: Dict[str, Any],
                 storage_stack_ref: Optional[str] = None,
                 opts: Optional[pulumi.ResourceOptions] = None):
        """Initialize adaptive infrastructure.
        
        Args:
            name: Infrastructure identifier (e.g., "default", "postgres", "mlflow")
            infra_stack_ref: Reference to infrastructure stack
            workspace_stack_ref: Reference to workspace stack
            config: Component configuration from adaptive.yaml
            storage_stack_ref: Optional reference to storage stack for blob access
            opts: Optional Pulumi resource options
        """
        super().__init__("modelops:adaptive:infra", name, None, opts)
        
        # Store storage_stack_ref for use in child methods
        self.storage_stack_ref = storage_stack_ref
        
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
        
        # Create namespace for this adaptive infrastructure
        # Using full naming pattern for clarity and avoiding conflicts
        parsed_infra = StackNaming.parse_stack_name(infra_stack_ref.split('/')[-1])
        env = parsed_infra.get('env', 'dev')
        infra_namespace = f"modelops-adaptive-{env}-{name}"
        ns = k8s.core.v1.Namespace(
            f"{name}-namespace",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=infra_namespace,
                labels={
                    "modelops.io/component": "adaptive",
                    "modelops.io/adaptive-name": name,
                    "modelops.io/algorithm": config.get("algorithm", "optuna")
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
                    namespace=infra_namespace
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
        
        # Create ConfigMap with infrastructure configuration
        config_map = k8s.core.v1.ConfigMap(
            f"{name}-config",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=infra_namespace,
                name="adaptive-config"
            ),
            data={
                "INFRA_NAME": name,
                "ALGORITHM": config.get("algorithm", "optuna"),
                "NAMESPACE": infra_namespace,
            },
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Optional: Create central store (e.g., Postgres for Optuna)
        postgres_dsn = None
        postgres_deployment = None
        central_store = config.get("central_store", {})
        if central_store and central_store.get("kind") == "postgres":
            postgres_dsn, postgres_deployment = self._create_postgres(name, infra_namespace, central_store, k8s_provider)
        
        # Create Secret for sensitive data
        secret = k8s.core.v1.Secret(
            f"{name}-secret",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=infra_namespace,
                name="run-secrets"
            ),
            type="Opaque",
            string_data={
                "POSTGRES_DSN": postgres_dsn or "",
                "STORAGE_CONNECTION": config.get("storage_connection", "")
            },
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Create adaptive worker Deployment
        workers_config = config.get("workers", {})
        worker_replicas = workers_config.get("replicas", 2)
        worker_image = workers_config.get("image", "ghcr.io/modelops/adaptive-worker:latest")
        worker_resources = workers_config.get("resources", {})
        
        workers = k8s.apps.v1.Deployment(
            f"{name}-workers",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=infra_namespace,
                name=f"adaptive-workers",
                labels={
                    "modelops.io/component": "adaptive-worker",
                    "modelops.io/adaptive-name": name
                }
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=worker_replicas,
                selector=k8s.meta.v1.LabelSelectorArgs(
                    match_labels={"app": "adaptive-worker"}
                ),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels={
                            "app": "adaptive-worker",
                            "modelops.io/component": "adaptive-worker",
                            "modelops.io/adaptive-name": name
                        }
                    ),
                    spec=k8s.core.v1.PodSpecArgs(
                        # Security: Run adaptive workers as non-root to prevent privilege escalation
                        # Adaptive workers access external services (Dask, Postgres) but shouldn't have root access
                        security_context=k8s.core.v1.PodSecurityContextArgs(
                            run_as_non_root=True,
                            run_as_user=1000,
                            fs_group=1000
                        ),
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="adaptive-worker",
                                image=worker_image,
                                # Security: Drop all capabilities and prevent privilege escalation
                                # Adaptive workers only need network access, no special kernel capabilities
                                security_context=k8s.core.v1.SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    run_as_non_root=True,
                                    run_as_user=1000,
                                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=["ALL"])
                                ),
                                command=workers_config.get("command", [
                                    "python", "-m", "modelops.adaptive.worker"
                                ]),
                                env=[
                                    k8s.core.v1.EnvVarArgs(
                                        name="POSTGRES_DSN",
                                        value_from=k8s.core.v1.EnvVarSourceArgs(
                                            secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(
                                                name="run-secrets",
                                                key="POSTGRES_DSN"
                                            )
                                        )
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="DASK_SCHEDULER",
                                        value=scheduler_address
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="ALGORITHM",
                                        value=config.get("algorithm", "optuna")
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="NAMESPACE",
                                        value=infra_namespace
                                    )
                                ],
                                # Mount storage secret if available
                                env_from=[
                                    k8s.core.v1.EnvFromSourceArgs(
                                        secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                                            name="modelops-storage",
                                            optional=True  # Don't fail if secret doesn't exist
                                        )
                                    )
                                ] if storage_stack_ref else None,
                                resources=k8s.core.v1.ResourceRequirementsArgs(
                                    requests=worker_resources.get("requests", {
                                        "cpu": "100m",
                                        "memory": "256Mi"
                                    }),
                                    limits=worker_resources.get("limits", {
                                        "cpu": "500m",
                                        "memory": "512Mi"
                                    })
                                ),
                                volume_mounts=[]
                            )
                        ]
                    )
                )
            ),
            opts=pulumi.ResourceOptions(
                provider=k8s_provider, 
                parent=self,
                depends_on=[postgres_deployment] if postgres_deployment else []  # Workers depend on postgres if it exists
            )
        )
        
        # Run smoke tests if configured (opt-in to prevent deployment failures)
        run_smoke_tests = config.get("smoke_tests", config.get("run_smoke_tests", False))
        if run_smoke_tests:
            # Determine which tests to run
            tests = ["dask"]  # Test Dask connectivity
            if postgres_dsn:
                tests.append("postgres")  # Test database if configured
            if storage_stack_ref:
                tests.append("storage")  # Test storage if configured
            
            # Environment for smoke test
            test_env = [
                k8s.core.v1.EnvVarArgs(
                    name="DASK_SCHEDULER",
                    value=scheduler_address
                )
            ]
            
            if postgres_dsn:
                test_env.append(
                    k8s.core.v1.EnvVarArgs(
                        name="POSTGRES_DSN",
                        value=postgres_dsn
                    )
                )
            
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
                f"adaptive-{name}",
                namespace=infra_namespace,
                tests=tests,
                k8s_provider=k8s_provider,
                env=test_env,
                env_from=test_env_from,
                timeout_seconds=60,
                opts=pulumi.ResourceOptions(
                    parent=self,
                    depends_on=[workers] + ([postgres_deployment] if postgres_deployment else [])
                )
            )
            
            # Add smoke test outputs
            self.smoke_test_job = smoke_test.job_name
            self.smoke_test_status = pulumi.Output.from_input("created")
        
        # Store outputs
        self.name = pulumi.Output.from_input(name)
        self.namespace = pulumi.Output.from_input(infra_namespace)
        self.scheduler_address = scheduler_address
        self.postgres_dsn = postgres_dsn
        self.workers_name = workers.metadata.name
        
        # Register outputs
        outputs_dict = {
            "name": name,
            "namespace": infra_namespace,
            "scheduler_address": scheduler_address,
            # Security: Mark postgres_dsn as secret to prevent plaintext exposure in state files
            # Without this, database credentials are visible in ~/.pulumi/stacks/*.json
            "postgres_dsn": pulumi.Output.secret(postgres_dsn) if postgres_dsn else None,
            "workers_name": f"adaptive-workers",
            "worker_replicas": worker_replicas,
            "algorithm": config.get("algorithm", "optuna")
        }
        
        # Add smoke test outputs if created
        if run_smoke_tests:
            outputs_dict["smoke_test_job"] = self.smoke_test_job
            outputs_dict["smoke_test_status"] = self.smoke_test_status
        
        self.register_outputs(outputs_dict)
    
    def _create_postgres(self, name: str, namespace: str, 
                        central_store_config: Dict[str, Any],
                        k8s_provider):
        """Create Postgres deployment for central store (e.g., Optuna distributed storage).
        
        Args:
            name: Run identifier
            namespace: Kubernetes namespace
            central_store_config: Central store configuration from adaptive.yaml
            k8s_provider: Kubernetes provider
            
        Returns:
            Tuple of (PostgreSQL connection string, Postgres deployment)
        """
        # Generate secure random password using pulumi_random to ensure it persists
        # This prevents password regeneration on every pulumi up (ISSUE-3 fix)
        import pulumi_random
        
        postgres_password_resource = pulumi_random.RandomPassword(
            f"{name}-postgres-password",
            length=24,
            special=False,  # Only alphanumeric for simplicity
            opts=pulumi.ResourceOptions(parent=self)
        )
        postgres_password = postgres_password_resource.result
        
        # Create K8s Secret for Postgres password to avoid plaintext in state
        # Using secretKeyRef ensures the password is never exposed in pod spec
        postgres_secret = k8s.core.v1.Secret(
            f"{name}-postgres-secret",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=namespace,
                name="postgres-secret"
            ),
            string_data={
                "password": postgres_password
            },
            type="Opaque",
            opts=pulumi.ResourceOptions(provider=k8s_provider, parent=self)
        )
        
        # Extract configuration values
        persistence = central_store_config.get("persistence", {})
        storage_size = persistence.get("size", "10Gi")
        storage_class = persistence.get("storageClass", "managed-csi")
        database_name = central_store_config.get("database", "optuna")
        database_user = central_store_config.get("user", "optuna_user")
        
        # Create PVC for Postgres data
        pvc = k8s.core.v1.PersistentVolumeClaim(
            f"{name}-postgres-pvc",
            metadata=k8s.meta.v1.ObjectMetaArgs(
                namespace=namespace,
                name="postgres-data"
            ),
            spec=k8s.core.v1.PersistentVolumeClaimSpecArgs(
                access_modes=["ReadWriteOnce"],
                storage_class_name=storage_class,
                resources=k8s.core.v1.ResourceRequirementsArgs(
                    requests={
                        "storage": storage_size
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
                        # Security: Run Postgres as non-root postgres user (uid 999 in official image)
                        # Postgres container already drops to non-root, but we enforce it at pod level
                        security_context=k8s.core.v1.PodSecurityContextArgs(
                            run_as_non_root=True,
                            run_as_user=999,
                            fs_group=999
                        ),
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="postgres",
                                image=POSTGRES_IMAGE,
                                # Security: Postgres doesn't need special capabilities
                                # Official image already runs as non-root postgres user
                                security_context=k8s.core.v1.SecurityContextArgs(
                                    allow_privilege_escalation=False,
                                    run_as_non_root=True,
                                    run_as_user=999,
                                    capabilities=k8s.core.v1.CapabilitiesArgs(drop=["ALL"])
                                ),
                                env=[
                                    k8s.core.v1.EnvVarArgs(
                                        name="POSTGRES_DB",
                                        value=database_name
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="POSTGRES_USER",
                                        value=database_user
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="POSTGRES_PASSWORD",
                                        # Reference password from K8s Secret to avoid plaintext in state
                                        value_from=k8s.core.v1.EnvVarSourceArgs(
                                            secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(
                                                name="postgres-secret",
                                                key="password"
                                            )
                                        )
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="PGDATA",
                                        value="/var/lib/postgresql/data/pgdata"
                                    )
                                ],
                                # Mount storage secret if available (passed from parent)
                                env_from=[
                                    k8s.core.v1.EnvFromSourceArgs(
                                        secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                                            name="modelops-storage",
                                            optional=True  # Don't fail if secret doesn't exist
                                        )
                                    )
                                ] if self.storage_stack_ref else None,
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
        # Password is already marked as secret, so the entire DSN will be secret
        dsn = pulumi.Output.concat(
            f"postgresql://{database_user}:",
            postgres_password,
            "@postgres.",
            namespace,
            f":5432/{database_name}"
        )
        
        # Return both DSN and deployment reference
        return dsn, postgres
