"""Client-side job submission orchestration.

This module runs on the user's workstation and handles the workflow
of submitting simulation and calibration jobs to the cluster.
"""

import base64
import json
import os
import uuid
from pathlib import Path

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

# TODO: Integrate modelops-bundle service when available
# from modelops_bundle.bundle_service import BundleService
# from modelops_bundle.auth import get_auth_provider
from modelops_contracts import (
    CalibrationJob,
    CalibrationSpec,
    Job,
    SimJob,
    SimulationStudy,
    TargetSpec,
)

from ..cli.k8s_client import cleanup_temp_kubeconfig, get_k8s_client
from ..core import automation
from ..images import get_image_config
from ..services.job_registry import JobRegistry
from ..services.job_state import JobStatus
from ..services.storage.azure import AzureBlobBackend
from ..services.storage.azure_versioned import AzureVersionedStore


class JobSubmissionClient:
    """Client-side orchestration for job submission.

    Handles bundle management, blob storage upload, and K8s job creation
    for both simulation and calibration jobs.
    """

    def __init__(self, env: str = "dev", namespace: str = "modelops-dask-dev"):
        """Initialize the job submission client.

        Automatically retrieves storage connection from Pulumi stack.
        Falls back to environment variable if stack not available.

        Args:
            env: Environment name (dev, staging, prod)
            namespace: Kubernetes namespace for job execution
        """
        self.env = env
        self.namespace = namespace
        # self._bundle_service = None  # TODO: Lazy initialization when BundleService available

        # TODO: Create auth provider for Azure (ModelOps owns this!)
        # self.auth_provider = get_auth_provider("azure")

        # Get storage connection from Pulumi or environment
        connection_string = self._get_storage_connection()
        self.storage = AzureBlobBackend(container="jobs", connection_string=connection_string)

        # Initialize job registry for state tracking
        # Using the same connection string and a separate container
        try:
            versioned_store = AzureVersionedStore(
                connection_string=connection_string, container="job-registry"
            )
            self.registry = JobRegistry(versioned_store)
        except Exception as e:
            # Registry is optional - if it fails, jobs still submit
            print(f"Warning: Job registry unavailable: {e}")
            self.registry = None

    def _get_registry_url(self) -> str:
        """Get container registry URL from Pulumi stack or environment.

        Returns:
            Registry URL for pushing bundles

        Raises:
            ValueError: If registry URL not found
        """
        # Try environment first (set by workspace deployment)
        registry = os.environ.get("MODELOPS_BUNDLE_REGISTRY")
        if registry:
            return registry

        # Try to get from BundleEnvironment file
        bundle_env_path = Path.home() / ".modelops" / "bundle-env" / f"{self.env}.yaml"
        if bundle_env_path.exists():
            import yaml

            with open(bundle_env_path) as f:
                bundle_env = yaml.safe_load(f)
                registry = bundle_env.get("registry", {}).get("login_server")
                if registry:
                    return registry

        # Try to get from Pulumi infrastructure stack
        try:
            outputs = automation.outputs("infra", self.env, refresh=False)
            if outputs and "acr_login_server" in outputs:
                registry = automation.get_output_value(outputs, "acr_login_server")
                if registry:
                    return registry
        except Exception:
            pass

        raise ValueError(
            "No registry URL found. Please ensure infrastructure is deployed "
            "or set MODELOPS_BUNDLE_REGISTRY environment variable."
        )

    def _get_storage_connection(self) -> str:
        """Get storage connection string from environment or Pulumi stack.

        Returns:
            Connection string for Azure storage

        Raises:
            ValueError: If connection string not found
        """
        # Check environment FIRST (avoids all passphrase issues)
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn_str:
            return conn_str

        # Only try Pulumi as fallback
        try:
            outputs = automation.outputs("storage", self.env, refresh=False)
            if outputs and "connection_string" in outputs:
                conn_str = automation.get_output_value(outputs, "connection_string")
                if conn_str:
                    return conn_str
        except Exception:
            pass  # Fall through to error

        raise ValueError(
            f"Storage connection not found for environment '{self.env}'.\n"
            "Please ensure storage is provisioned:\n"
            "  mops storage up examples/storage.yaml\n"
            "Or set AZURE_STORAGE_CONNECTION_STRING environment variable."
        )

    def _get_postgres_url(self) -> str | None:
        """Read POSTGRES_DSN from adaptive infrastructure if available.

        The adaptive plane provisions PostgreSQL and stores the connection
        string in a secret in the adaptive namespace. This method reads
        that secret and returns the DSN for use by calibration jobs.

        Returns:
            PostgreSQL connection URL, or None if not available
        """
        adaptive_ns = f"modelops-adaptive-{self.env}-default"
        try:
            v1, _, temp_path = get_k8s_client(self.env)
            try:
                secret = v1.read_namespaced_secret("run-secrets", adaptive_ns)
                dsn_b64 = secret.data.get("POSTGRES_DSN")
                if dsn_b64:
                    return base64.b64decode(dsn_b64).decode()
            finally:
                cleanup_temp_kubeconfig(temp_path)
        except ApiException as e:
            if e.status == 404:
                # Adaptive infrastructure not provisioned - this is normal
                pass
            else:
                # Log but don't fail - job can still run without Optuna persistence
                print(f"Warning: Failed to read adaptive secrets: {e}")
        except Exception as e:
            # Unexpected error - log but don't fail
            print(f"Warning: Could not check for PostgreSQL URL: {e}")
        return None

    def _get_postgres_env_vars(self) -> list:
        """Get POSTGRES_URL env var for K8s job if available.

        Returns:
            List containing POSTGRES_URL env var, or empty list if not available
        """
        postgres_url = self._get_postgres_url()
        if postgres_url:
            return [
                k8s_client.V1EnvVar(
                    name="POSTGRES_URL",
                    value=postgres_url,
                )
            ]
        return []

    def submit_job(self, job: Job) -> str:
        """Submit any job type to the cluster.

        This is the polymorphic entry point that handles both
        SimJob and CalibrationJob types.

        Args:
            job: Job to submit (SimJob or CalibrationJob)

        Returns:
            Job ID of submitted job

        Raises:
            ValueError: If job type is unknown or validation fails
        """
        # Validate job
        job.validate()

        # Upload to blob storage
        blob_key = self._upload_job(job)

        # Determine runner image based on job type
        # Use centralized image configuration
        img_config = get_image_config()
        runner_tag = os.environ.get("MODELOPS_RUNNER_TAG")  # Allow tag override

        match job:
            case SimJob():
                # Use runner image from config, with optional tag override
                if runner_tag:
                    # Override just the tag, extract registry/org from existing image
                    base_image = img_config.runner_image()
                    # Split off the tag and replace with custom one
                    image_parts = base_image.rsplit(":", 1)[0]
                    image = f"{image_parts}:{runner_tag}"
                else:
                    image = img_config.runner_image()
            case CalibrationJob():
                # For now, use same runner for both types
                if runner_tag:
                    base_image = img_config.runner_image()
                    image_parts = base_image.rsplit(":", 1)[0]
                    image = f"{image_parts}:{runner_tag}"
                else:
                    image = img_config.runner_image()
            case _:
                raise ValueError(f"Unknown job type: {type(job).__name__}")

        # Register job in tracking system (non-blocking)
        # job_id already has "job-" prefix, don't add another
        k8s_name = job.job_id
        if self.registry:
            try:
                self.registry.register_job(
                    job_id=job.job_id,
                    k8s_name=k8s_name,
                    namespace=self.namespace,
                    metadata={
                        "type": type(job).__name__,
                        "blob_key": blob_key,
                        "image": image,
                    },
                )
                # Update to SUBMITTING status
                self.registry.update_status(job.job_id, JobStatus.SUBMITTING)
            except Exception as e:
                # Don't fail job submission if registry fails
                print(f"Warning: Failed to register job in tracking system: {e}")

        # Create K8s Job
        self._create_k8s_job(job.job_id, blob_key, image)

        # Update status to SCHEDULED if K8s creation succeeded
        if self.registry:
            try:
                self.registry.update_status(job.job_id, JobStatus.SCHEDULED)
            except Exception:
                pass  # Non-critical

        return job.job_id

    def submit_sim_job(
        self,
        study: SimulationStudy,
        bundle_strategy: str = "latest",
        bundle_ref: str | None = None,
        build_path: Path | None = None,
    ) -> str:
        """Submit a simulation study as a SimJob.

        Convenience method that handles bundle resolution and
        converts SimulationStudy to SimJob.

        Args:
            study: SimulationStudy to execute
            bundle_strategy: How to resolve bundle ("explicit", "latest", "build")
            bundle_ref: Explicit bundle reference if strategy="explicit"
            build_path: Path to build from if strategy="build"

        Returns:
            Job ID of submitted job

        Raises:
            ValueError: If bundle strategy is invalid or resolution fails
        """
        # Resolve bundle reference
        resolved_bundle = self._resolve_bundle(study.model, bundle_strategy, bundle_ref, build_path)

        # Create SimJob from study
        job = study.to_simjob(resolved_bundle)

        # Submit the job
        return self.submit_job(job)

    def submit_calibration_job(
        self,
        spec: CalibrationSpec,
        bundle_strategy: str = "latest",
        bundle_ref: str | None = None,
        build_path: Path | None = None,
    ) -> str:
        """Submit a calibration specification as a CalibrationJob.

        Args:
            spec: CalibrationSpec to execute
            bundle_strategy: How to resolve bundle ("explicit", "latest", "build")
            bundle_ref: Explicit bundle reference if strategy="explicit"
            build_path: Path to build from if strategy="build"

        Returns:
            Job ID of submitted job
        """
        # Resolve bundle reference
        resolved_bundle = self._resolve_bundle(spec.model, bundle_strategy, bundle_ref, build_path)

        # Create CalibrationJob
        job = CalibrationJob(
            job_id=f"calib-{uuid.uuid4().hex[:8]}",
            bundle_ref=resolved_bundle,
            algorithm=spec.algorithm,
            target_spec=TargetSpec(
                data=spec.target_data,
                loss_function="mse",  # Default, could be in spec
                metadata=spec.metadata,
            ),
            max_iterations=spec.max_iterations,
            convergence_criteria=spec.convergence_criteria,
            algorithm_config=spec.algorithm_config,
        )

        # Submit the job
        return self.submit_job(job)

    def _resolve_bundle(
        self,
        model: str,
        strategy: str,
        bundle_ref: str | None = None,
        build_path: Path | None = None,
    ) -> str:
        """Resolve bundle reference based on strategy.

        Args:
            model: Model name for latest lookup
            strategy: Resolution strategy ("explicit", "latest", "build")
            bundle_ref: Explicit reference if strategy="explicit"
            build_path: Build path if strategy="build"

        Returns:
            Resolved bundle reference (sha256:...)

        Raises:
            ValueError: If resolution fails
        """
        if strategy == "explicit":
            if not bundle_ref:
                raise ValueError("bundle_ref required for explicit strategy")
            # Return bundle_ref as-is - SimTask supports repository@sha256 format
            return bundle_ref

        elif strategy == "latest":
            return self._get_latest_bundle(model)

        elif strategy == "build":
            if not build_path:
                build_path = Path.cwd()
            return self._build_and_push(build_path)

        else:
            raise ValueError(f"Unknown bundle strategy: {strategy}")

    # TODO: Implement when BundleService is available
    # @property
    # def bundle_service(self) -> BundleService:
    #     """Lazy initialization of BundleService with injected auth."""
    #     if self._bundle_service is None:
    #         # Pass auth provider to the new constructor signature
    #         self._bundle_service = BundleService(auth_provider=self.auth_provider)
    #     return self._bundle_service

    def _get_latest_bundle(self, model: str) -> str:
        """Get latest bundle for a model from registry.

        Args:
            model: Model name to search for

        Returns:
            Bundle reference for latest version

        Raises:
            ValueError: If no bundles found for model
        """
        # TODO: Implement when BundleService is available
        # bundles = self.bundle_service.list_bundles(
        #     filter_prefix=model.replace(".", "/")
        # )
        #
        # if not bundles:
        #     raise ValueError(f"No bundles found for model: {model}")
        #
        # # Get most recent by timestamp
        # latest = sorted(bundles, key=lambda b: b.created_at)[-1]
        # return f"sha256:{latest.digest}"
        raise NotImplementedError("BundleService integration not yet available")

    def _build_and_push(self, path: Path) -> str:
        """Build and push bundle from local path.

        Args:
            path: Local path containing code to bundle

        Returns:
            Bundle reference of pushed bundle
        """
        # Get registry URL dynamically
        registry = self._get_registry_url()

        # TODO: Use modelops-bundle service to build and push when available
        # bundle_ref = self.bundle_service.build_and_push(
        #     source_path=path,
        #     registry=registry,
        # )
        # return bundle_ref
        raise NotImplementedError(
            "BundleService integration not yet available. Use modelops-bundle CLI directly."
        )

    def _upload_job(self, job: Job) -> str:
        """Upload job specification to blob storage.

        Args:
            job: Job to upload

        Returns:
            Blob storage key for uploaded job
        """
        # Serialize job to JSON
        job_json = self._serialize_job(job)

        # Generate blob key
        blob_key = job.to_blob_key()

        # Upload to blob storage
        self.storage.save(blob_key, job_json.encode("utf-8"))

        return blob_key

    def _serialize_job(self, job: Job) -> str:
        """Serialize job to JSON format.

        Args:
            job: Job to serialize

        Returns:
            JSON string representation
        """
        data = {
            "job_type": job.job_type,
            "job_id": job.job_id,
            "bundle_ref": job.bundle_ref,
        }

        # Add type-specific fields
        match job:
            case SimJob(
                tasks=tasks,
                priority=priority,
                metadata=metadata,
                target_spec=target_spec,
            ):
                data["priority"] = priority
                data["metadata"] = metadata

                # Serialize target_spec if present
                if target_spec:
                    data["target_spec"] = {
                        "data": target_spec.data,
                        "loss_function": target_spec.loss_function,
                        "weights": target_spec.weights,
                        "metadata": target_spec.metadata,
                    }

                data["tasks"] = [
                    {
                        "entrypoint": str(task.entrypoint),
                        "bundle_ref": task.bundle_ref,
                        "params": {
                            "param_id": task.params.param_id,
                            "values": dict(task.params.params),  # Convert MappingProxyType to dict
                        },
                        "seed": task.seed,
                        "outputs": task.outputs,
                    }
                    for task in tasks
                ]

            case CalibrationJob(
                algorithm=algo,
                target_spec=target,
                max_iterations=max_iter,
                convergence_criteria=conv,
                algorithm_config=config,
            ):
                data["algorithm"] = algo
                data["target_spec"] = {
                    "data": target.data,
                    "loss_function": target.loss_function,
                    "weights": target.weights,
                    "metadata": target.metadata,
                }
                data["max_iterations"] = max_iter
                data["convergence_criteria"] = conv
                data["algorithm_config"] = config

        return json.dumps(data, indent=2, default=str)

    def _create_k8s_job(self, job_id: str, blob_key: str, image: str) -> None:
        """Create Kubernetes Job to execute the job.

        Args:
            job_id: Job identifier
            blob_key: Blob storage key containing job spec
            image: Container image for the runner
        """
        # Get K8s client
        v1, apps_v1, temp_path = get_k8s_client(self.env)

        try:
            # Create job manifest
            # job_id already has "job-" prefix, don't add another
            job_manifest = k8s_client.V1Job(
                metadata=k8s_client.V1ObjectMeta(
                    name=job_id,
                    namespace=self.namespace,
                    labels={
                        "app": "modelops",
                        "component": "job-runner",
                        "job-id": job_id,
                    },
                ),
                spec=k8s_client.V1JobSpec(
                    template=k8s_client.V1PodTemplateSpec(
                        spec=k8s_client.V1PodSpec(
                            containers=[
                                k8s_client.V1Container(
                                    name="job-runner",
                                    image=image,
                                    env=[
                                        # Pass blob key to runner
                                        k8s_client.V1EnvVar(name="JOB_BLOB_KEY", value=blob_key),
                                        # Storage connection from secret
                                        k8s_client.V1EnvVar(
                                            name="AZURE_STORAGE_CONNECTION_STRING",
                                            value_from=k8s_client.V1EnvVarSource(
                                                secret_key_ref=k8s_client.V1SecretKeySelector(
                                                    name="modelops-storage",
                                                    key="AZURE_STORAGE_CONNECTION_STRING",
                                                )
                                            ),
                                        ),
                                        # Dask scheduler address
                                        k8s_client.V1EnvVar(
                                            name="DASK_SCHEDULER_ADDRESS",
                                            value="tcp://dask-scheduler:8786",
                                        ),
                                        # Bundle registry for worker plugin
                                        k8s_client.V1EnvVar(
                                            name="MODELOPS_BUNDLE_REGISTRY",
                                            value=self._get_registry_url(),
                                        ),
                                    ]
                                    + self._get_postgres_env_vars(),
                                    resources=k8s_client.V1ResourceRequirements(
                                        requests={"cpu": "1", "memory": "2Gi"},
                                        limits={"cpu": "2", "memory": "4Gi"},
                                    ),
                                )
                            ],
                            restart_policy="Never",
                            service_account_name="default",
                            # TODO: Re-enable node selector when nodes are properly labeled
                            # tolerations=[
                            #     # Tolerate CPU node taint
                            #     k8s_client.V1Toleration(
                            #         key="modelops.io/role",
                            #         operator="Equal",
                            #         value="cpu",
                            #         effect="NoSchedule"
                            #     )
                            # ],
                            # node_selector={
                            #     "modelops.io/role": "cpu"
                            # },
                        )
                    ),
                    backoff_limit=3,  # Retry up to 3 times
                    ttl_seconds_after_finished=86400,  # Clean up after 24 hours
                ),
            )

            # Create the job
            batch_v1 = k8s_client.BatchV1Api()
            batch_v1.create_namespaced_job(namespace=self.namespace, body=job_manifest)

        finally:
            cleanup_temp_kubeconfig(temp_path)
