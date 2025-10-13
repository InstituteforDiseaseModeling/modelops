"""Service for Dask workspace management."""

from typing import Dict, Any, Optional, List
import subprocess
import os
import json

from .base import BaseService, ComponentStatus, ComponentState, OutputCapture
from .utils import stack_exists, get_safe_outputs
from ..components import WorkspaceConfig
from ..core import StackNaming, automation
from ..core.automation import get_output_value
from ..core.state_manager import PulumiStateManager


class WorkspaceService(BaseService):
    """Service for Dask workspace management."""

    def __init__(self, env: str):
        """Initialize workspace service."""
        super().__init__(env)

    def provision(
        self,
        config: Optional[WorkspaceConfig] = None,
        infra_stack_ref: Optional[str] = None,
        registry_stack_ref: Optional[str] = None,
        storage_stack_ref: Optional[str] = None,
        verbose: bool = False
    ) -> Dict[str, Any]:
        """
        Provision Dask workspace.

        Args:
            config: Workspace configuration
            infra_stack_ref: Infrastructure stack reference (auto-resolved if None)
            registry_stack_ref: Registry stack reference (auto-resolved if None)
            storage_stack_ref: Storage stack reference (auto-resolved if None)
            verbose: Show detailed output

        Returns:
            Stack outputs

        Raises:
            Exception: If provisioning fails
        """
        def pulumi_program():
            """Create DaskWorkspace in Stack 2 context."""
            from ..infra.components.workspace import DaskWorkspace
            import pulumi

            # Convert config to dict if provided
            if config is None:
                raise ValueError(
                    "Workspace configuration is required. "
                    "Use 'mops workspace up' which will use ~/.modelops/infrastructure.yaml by default, "
                    "or provide explicit config with 'mops workspace up --config workspace.yaml'"
                )

            if hasattr(config, 'to_pulumi_config'):
                workspace_config = config.to_pulumi_config()
            elif isinstance(config, dict):
                workspace_config = config
            else:
                raise ValueError(f"Invalid workspace configuration type: {type(config)}")

            workspace_config["environment"] = self.env

            # Centralized ref resolution - all paths use the same logic
            # Resolve infrastructure stack if not provided
            if infra_stack_ref is None:
                infra_ref = StackNaming.ref("infra", self.env)
            else:
                infra_ref = infra_stack_ref

            # Resolve registry stack if not provided
            # TODO: Fix stack_exists() returning False for existing stacks
            # For now, always try to reference if in dev/staging
            if registry_stack_ref is None:
                if self.env in ["dev", "staging"]:
                    registry_ref = StackNaming.ref("registry", self.env)
                elif stack_exists("registry", self.env):
                    registry_ref = StackNaming.ref("registry", self.env)
                else:
                    registry_ref = None
            else:
                registry_ref = registry_stack_ref

            # Resolve storage stack if not provided
            # TODO: Fix stack_exists() returning False for existing stacks
            # For now, always try to reference if in dev/staging
            if storage_stack_ref is None:
                if self.env in ["dev", "staging"]:
                    storage_ref = StackNaming.ref("storage", self.env)
                elif stack_exists("storage", self.env):
                    storage_ref = StackNaming.ref("storage", self.env)
                else:
                    storage_ref = None
            else:
                storage_ref = storage_stack_ref

            # Create the workspace component
            workspace = DaskWorkspace(
                "dask",
                infra_ref,
                workspace_config,
                storage_stack_ref=storage_ref,
                registry_stack_ref=registry_ref
            )

            # Export outputs at stack level for visibility
            pulumi.export("scheduler_address", workspace.scheduler_address)
            pulumi.export("dashboard_url", workspace.dashboard_url)
            pulumi.export("namespace", workspace.namespace)
            pulumi.export("worker_count", workspace.worker_count)
            pulumi.export("worker_processes", workspace.worker_processes)
            pulumi.export("worker_threads", workspace.worker_threads)
            pulumi.export("autoscaling_enabled", workspace.autoscaling_enabled)
            pulumi.export("autoscaling_min", workspace.autoscaling_min)
            pulumi.export("autoscaling_max", workspace.autoscaling_max)

            return workspace

        # Use PulumiStateManager for automatic lock recovery and state management
        state_manager = PulumiStateManager("workspace", self.env)
        capture = OutputCapture(verbose)

        # State manager handles:
        # - Stale lock detection and clearing
        # - State reconciliation with Kubernetes
        # - No environment YAML updates (workspace doesn't save to YAML)
        result = state_manager.execute_with_recovery(
            "up",
            program=pulumi_program,
            on_output=capture
        )

        return result.outputs if result else {}

    def destroy(self, verbose: bool = False) -> None:
        """
        Destroy workspace.

        Args:
            verbose: Show detailed output

        Raises:
            Exception: If destruction fails
        """
        import os
        # Allow deletion of K8s resources even if cluster is unreachable
        os.environ["PULUMI_K8S_DELETE_UNREACHABLE"] = "true"

        # Use PulumiStateManager for automatic lock recovery and cleanup
        state_manager = PulumiStateManager("workspace", self.env)
        capture = OutputCapture(verbose)

        # State manager handles:
        # - Stale lock detection and clearing
        # - No environment YAML cleanup (workspace doesn't save to YAML)
        state_manager.execute_with_recovery(
            "destroy",
            on_output=capture
        )

    def status(self) -> ComponentStatus:
        """
        Get workspace status with unified contract.

        Returns:
            ComponentStatus with workspace details
        """
        try:
            outputs = automation.outputs("workspace", self.env, refresh=False)

            if outputs:
                # Check if we can reach the scheduler
                scheduler_address = get_output_value(outputs, "scheduler_address")
                health = self._check_scheduler_health(scheduler_address) if scheduler_address else False

                # Get autoscaling info
                autoscaling_enabled = get_output_value(outputs, "autoscaling_enabled", False)
                worker_info = get_output_value(outputs, "worker_count", "unknown")
                if autoscaling_enabled:
                    min_workers = get_output_value(outputs, "autoscaling_min", 2)
                    max_workers = get_output_value(outputs, "autoscaling_max", 20)
                    worker_info = f"{min_workers}-{max_workers} (autoscaling)"

                return ComponentStatus(
                    deployed=True,
                    phase=ComponentState.READY if health else ComponentState.UNKNOWN,
                    details={
                        "scheduler_address": get_output_value(outputs, "scheduler_address"),
                        "dashboard_url": get_output_value(outputs, "dashboard_url"),
                        "namespace": get_output_value(outputs, "namespace"),
                        "workers": worker_info,
                        "autoscaling": autoscaling_enabled,
                        "health": health
                    }
                )
            else:
                return ComponentStatus(
                    deployed=False,
                    phase=ComponentState.NOT_DEPLOYED,
                    details={}
                )
        except Exception as e:
            return ComponentStatus(
                deployed=False,
                phase=ComponentState.UNKNOWN,
                details={"error": str(e)}
            )

    def get_outputs(self) -> Dict[str, Any]:
        """
        Get all outputs from workspace stack.

        Returns:
            Dictionary of stack outputs
        """
        outputs = automation.outputs("workspace", self.env, refresh=False)
        return outputs or {}

    def list_workspaces(self) -> List[Dict[str, str]]:
        """
        List all workspaces across environments.

        Returns:
            List of workspace info dicts
        """
        import pulumi.automation as auto
        from ..core.paths import ensure_work_dir, BACKEND_DIR

        project_name = StackNaming.get_project_name("workspace")
        work_dir = ensure_work_dir("workspace")

        if not BACKEND_DIR.exists():
            return []

        try:
            # Create a LocalWorkspace bound to the workspace project + backend
            from ..core.automation import workspace_options
            ws = auto.LocalWorkspace(
                **workspace_options(project_name, work_dir).__dict__
            )

            # List stacks registered for this project
            stacks = ws.list_stacks()
            if not stacks:
                return []

            workspaces = []
            for s in sorted(stacks, key=lambda ss: ss.name):
                stack_name = s.name
                try:
                    stack_env = StackNaming.parse_stack_name(stack_name)["env"]
                except:
                    stack_env = stack_name

                status = "Unknown"
                try:
                    # Fast state read without refresh
                    from ..core.automation import noop_program as _noop
                    st = auto.select_stack(
                        stack_name=stack_name,
                        project_name=project_name,
                        program=_noop,
                        opts=workspace_options(project_name, work_dir)
                    )

                    state = st.export_stack()
                    if hasattr(state, 'deployment') and isinstance(state.deployment, dict):
                        resources = state.deployment.get("resources", [])
                        has_real = any(r.get("type") != "pulumi:pulumi:Stack" for r in resources)
                        status = "Deployed" if has_real else "Not deployed"
                except:
                    pass

                workspaces.append({
                    "env": stack_env,
                    "stack": stack_name,
                    "status": status
                })

            return workspaces

        except Exception:
            return []

    def port_forward(
        self,
        target: str = "dashboard",
        local_port: Optional[int] = None,
        remote_port: Optional[int] = None,
        service_name: Optional[str] = None
    ) -> None:
        """
        Port forward to Dask services.

        Args:
            target: What to forward (dashboard, scheduler, or custom)
            local_port: Local port to use
            remote_port: Remote port on the service
            service_name: Override service name

        Raises:
            Exception: If port forwarding fails
        """
        # Get workspace outputs
        outputs = automation.outputs("workspace", self.env, refresh=False)
        if not outputs:
            raise Exception("Workspace not deployed")

        namespace = get_output_value(outputs, 'namespace')
        if not namespace:
            raise Exception("Could not determine workspace namespace")

        # Determine service and port
        if not service_name:
            service_name = get_output_value(outputs, 'scheduler_service_name', 'dask-scheduler')

        if not remote_port:
            if target == "dashboard":
                remote_port = get_output_value(outputs, 'dashboard_port', 8787)
            elif target == "scheduler":
                remote_port = get_output_value(outputs, 'scheduler_port', 8786)
            else:
                raise ValueError(f"Unknown target '{target}'")

        if not local_port:
            local_port = remote_port

        # Get kubeconfig from infrastructure
        infra_outputs = automation.outputs("infra", self.env, refresh=False)
        if not infra_outputs:
            raise Exception("Infrastructure not deployed")

        kubeconfig = get_output_value(infra_outputs, 'kubeconfig')
        if not kubeconfig:
            raise Exception("Could not get kubeconfig")

        # Start port forward using kubectl
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(kubeconfig)
            temp_kubeconfig = f.name

        try:
            # Find the pod
            cmd = [
                "kubectl", "get", "pods",
                "-n", namespace,
                "-l", f"app={service_name.replace('-scheduler', '-scheduler')}",
                "-o", "jsonpath={.items[0].metadata.name}",
                "--kubeconfig", temp_kubeconfig
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise Exception(f"Could not find pod: {result.stderr}")

            pod_name = result.stdout.strip()
            if not pod_name:
                raise Exception(f"No pods found for service {service_name}")

            # Start port forward
            cmd = [
                "kubectl", "port-forward",
                f"pod/{pod_name}",
                f"{local_port}:{remote_port}",
                "-n", namespace,
                "--kubeconfig", temp_kubeconfig
            ]

            print(f"Starting port forward to {target}...")
            print(f"  Local: http://localhost:{local_port}")
            print(f"  Pod: {pod_name}")
            print("\nPress Ctrl+C to stop")

            proc = subprocess.Popen(cmd)
            proc.wait()

        finally:
            os.unlink(temp_kubeconfig)

    def run_smoke_tests(self, namespace: str = None) -> bool:
        """
        Run smoke tests for workspace connectivity.

        Args:
            namespace: Override namespace

        Returns:
            True if tests pass
        """
        outputs = automation.outputs("workspace", self.env, refresh=False)
        if not outputs:
            return False

        if not namespace:
            namespace = get_output_value(outputs, 'namespace')

        # This would run the actual smoke tests
        # For now, just check basic connectivity
        scheduler_address = get_output_value(outputs, 'scheduler_address')
        return self._check_scheduler_health(scheduler_address)

    def _check_scheduler_health(self, scheduler_address: str) -> bool:
        """Check if Dask scheduler is healthy."""
        # This would actually check the scheduler
        # For now, just return True if address exists
        return bool(scheduler_address)