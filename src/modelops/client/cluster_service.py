"""Service for Kubernetes cluster management."""

from typing import Dict, Any, Optional
from pathlib import Path
import subprocess
import os

from .base import BaseService, ComponentStatus, ComponentState, OutputCapture
from .utils import stack_exists, get_safe_outputs
from ..components import AzureProviderConfig
from ..core import StackNaming, automation
from ..core.automation import get_output_value
from ..core.state_manager import PulumiStateManager


class ClusterService(BaseService):
    """Service for Kubernetes cluster management (formerly infra)."""

    def __init__(self, env: str):
        """Initialize cluster service."""
        super().__init__(env)

    def provision(self, config: AzureProviderConfig, verbose: bool = False) -> Dict[str, Any]:
        """
        Provision AKS cluster.

        Args:
            config: Azure provider configuration
            verbose: Show detailed output

        Returns:
            Stack outputs including kubeconfig

        Raises:
            Exception: If provisioning fails
        """
        def pulumi_program():
            """Pulumi program that creates infrastructure using ComponentResource."""
            import pulumi
            from ..core.naming import StackNaming
            from ..core import automation

            if config.provider == "azure":
                from ..infra.components.azure import ModelOpsCluster

                # Get registry_id from registry stack if it exists
                # This ensures ACR exists before cluster tries to grant permissions
                registry_id = None
                try:
                    registry_outputs = automation.outputs("registry", self.env, refresh=False)
                    if registry_outputs and "registry_id" in registry_outputs:
                        # Extract the value if it's wrapped
                        registry_id_val = registry_outputs["registry_id"]
                        if hasattr(registry_id_val, 'value'):
                            registry_id = pulumi.Output.from_input(registry_id_val.value)
                        elif isinstance(registry_id_val, dict) and 'value' in registry_id_val:
                            registry_id = pulumi.Output.from_input(registry_id_val['value'])
                        else:
                            registry_id = pulumi.Output.from_input(registry_id_val)
                except:
                    # Registry doesn't exist yet or has no outputs
                    pulumi.log.warn("Registry stack not found or has no outputs, ACR permissions will be skipped")

                # Pass validated config dict to component with environment and registry_id
                config_dict = config.to_pulumi_config()
                config_dict["environment"] = self.env
                cluster = ModelOpsCluster("modelops", config_dict, registry_id=registry_id)

                # Export outputs at the stack level for access via StackReference
                pulumi.export("kubeconfig", cluster.kubeconfig)
                pulumi.export("cluster_name", cluster.cluster_name)
                pulumi.export("resource_group", cluster.resource_group)
                pulumi.export("location", cluster.location)
                pulumi.export("provider", pulumi.Output.from_input("azure"))

                return cluster
            else:
                raise ValueError(f"Provider '{config.provider}' not yet implemented")

        # Use PulumiStateManager for automatic lock recovery and state management
        state_manager = PulumiStateManager("infra", self.env)
        capture = OutputCapture(verbose)

        # State manager handles:
        # - Stale lock detection and clearing
        # - State reconciliation with Azure
        # - Environment YAML updates (cluster doesn't save to YAML)
        result = state_manager.execute_with_recovery(
            "up",
            program=pulumi_program,
            on_output=capture
        )

        outputs = result.outputs if result else {}

        # Verify kubeconfig exists
        if not get_output_value(outputs, "kubeconfig"):
            raise Exception("No kubeconfig returned from infrastructure creation")

        # Automatically update local kubeconfig with the new cluster credentials
        # This ensures kubectl always points to the newly created cluster
        try:
            cluster_name = get_output_value(outputs, "cluster_name")
            resource_group = get_output_value(outputs, "resource_group")

            if cluster_name and resource_group:
                print(f"  Updating kubeconfig for cluster {cluster_name}...")
                self._update_kubeconfig(resource_group, cluster_name)
                print(f"  ✓ Kubeconfig updated and context set to {cluster_name}")
        except Exception as e:
            # Don't fail provisioning if kubeconfig update fails
            print(f"  ⚠ Could not update kubeconfig automatically: {e}")
            print(f"  Run: az aks get-credentials -g {resource_group} -n {cluster_name} --overwrite-existing")

        return outputs

    def destroy(
        self,
        delete_rg: bool = False,
        force: bool = False,
        verbose: bool = False
    ) -> None:
        """
        Destroy cluster.

        Args:
            delete_rg: Also delete the resource group
            force: Skip dependency checks
            verbose: Show detailed output

        Raises:
            Exception: If destruction fails
        """
        # Check for dependent stacks unless forced
        if not force:
            dependent_stacks = self._check_dependent_stacks()
            if dependent_stacks:
                raise Exception(
                    f"Dependent stacks exist: {', '.join(dependent_stacks)}. "
                    "Destroy them first or use force=True"
                )

        # Use PulumiStateManager for automatic lock recovery and cleanup
        state_manager = PulumiStateManager("infra", self.env)
        capture = OutputCapture(verbose)

        # Get cluster name before destroying for cleanup
        cluster_name = None
        try:
            outputs = automation.outputs("infra", self.env, refresh=False)
            if outputs:
                cluster_name = get_output_value(outputs, "cluster_name")
        except:
            pass  # Ignore errors getting outputs

        # State manager handles:
        # - Stale lock detection and clearing
        # - No environment YAML cleanup (cluster doesn't save to YAML)
        state_manager.execute_with_recovery(
            "destroy",
            on_output=capture
        )

        # Clean up kubeconfig entries for this cluster
        if cluster_name:
            try:
                self._prune_kubeconfig(cluster_name)
                print(f"  ✓ Removed kubeconfig entries for {cluster_name}")
            except Exception as e:
                # Best effort - don't fail destroy if cleanup fails
                print(f"  ⚠ Could not clean up kubeconfig: {e}")

        # Note: Resource group deletion is handled by InfrastructureService
        # which properly destroys it through resource_group_service.destroy()
        # This ensures Pulumi state management is maintained

    def status(self) -> ComponentStatus:
        """
        Get cluster status with unified contract.

        Returns:
            ComponentStatus with cluster details
        """
        try:
            outputs = automation.outputs("infra", self.env, refresh=False)

            if outputs:
                # Try to check actual connectivity if possible
                kubeconfig = get_output_value(outputs, "kubeconfig")
                connectivity = self._check_k8s_connectivity(kubeconfig) if kubeconfig else False

                return ComponentStatus(
                    deployed=True,
                    phase=ComponentState.READY if connectivity else ComponentState.UNKNOWN,
                    details={
                        "cluster_name": get_output_value(outputs, "cluster_name", "unknown"),
                        "resource_group": get_output_value(outputs, "resource_group", "unknown"),
                        "location": get_output_value(outputs, "location", "unknown"),
                        "provider": get_output_value(outputs, "provider", "azure"),
                        "connectivity": connectivity
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

    def get_kubeconfig(
        self,
        merge: bool = False,
        output: Optional[Path] = None,
        show_secrets: bool = False
    ) -> Optional[str]:
        """
        Get kubeconfig from infrastructure state.

        Args:
            merge: Merge with existing ~/.kube/config
            output: Write to file instead of returning
            show_secrets: Show kubeconfig content (it's sensitive)

        Returns:
            Kubeconfig YAML string or None
        """
        outputs = automation.outputs("infra", self.env, refresh=False)

        if not outputs:
            return None

        kubeconfig_yaml = get_output_value(outputs, "kubeconfig")
        if not kubeconfig_yaml:
            return None

        if merge:
            self._merge_kubeconfig(kubeconfig_yaml)
            return None
        elif output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(kubeconfig_yaml)
            os.chmod(output, 0o600)  # Secure permissions
            return None
        else:
            if show_secrets:
                return kubeconfig_yaml
            else:
                return "****"  # Mask by default

    def get_outputs(self) -> Dict[str, Any]:
        """
        Get all outputs from infrastructure stack.

        Returns:
            Dictionary of stack outputs
        """
        outputs = automation.outputs("infra", self.env, refresh=False)
        return outputs or {}

    def _check_dependent_stacks(self) -> list[str]:
        """Check for stacks that depend on the cluster."""
        dependent = []
        for component in ["workspace", "storage"]:
            if stack_exists(component, self.env):
                # Check if it has resources
                try:
                    outputs = automation.outputs(component, self.env, refresh=False)
                    if outputs:
                        dependent.append(component)
                except:
                    pass
        return dependent

    def _check_k8s_connectivity(self, kubeconfig: str) -> bool:
        """
        Check if we can connect to the Kubernetes API.

        Args:
            kubeconfig: Kubeconfig YAML content

        Returns:
            True if can connect, False otherwise
        """
        import tempfile

        try:
            # Write kubeconfig to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(kubeconfig)
                temp_path = f.name

            try:
                # Try to get nodes
                result = subprocess.run(
                    ["kubectl", "get", "nodes", "--kubeconfig", temp_path, "-o", "name"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                return result.returncode == 0
            finally:
                os.unlink(temp_path)
        except:
            return False

    def _update_kubeconfig(self, resource_group: str, cluster_name: str):
        """Update kubeconfig using Azure CLI with overwrite.

        This uses az aks get-credentials with --overwrite-existing to ensure
        we always get the latest cluster credentials, fixing stale DNS issues.

        Args:
            resource_group: Azure resource group name
            cluster_name: AKS cluster name
        """
        import subprocess

        # Use --overwrite-existing to handle stale DNS from old clusters
        cmd = ["az", "aks", "get-credentials",
               "-g", resource_group,
               "-n", cluster_name,
               "--overwrite-existing"]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Failed to update kubeconfig: {result.stderr}")

        # Set the context to the new cluster
        subprocess.run(["kubectl", "config", "use-context", cluster_name],
                      capture_output=True, check=False)

    def _prune_kubeconfig(self, cluster_name: str):
        """Remove kubeconfig entries for a cluster.

        Cleans up context, cluster, and user entries to avoid accumulation
        of stale entries in kubeconfig.

        Args:
            cluster_name: Name of the cluster to remove
        """
        import subprocess

        # These commands may fail if entries don't exist, that's ok
        commands = [
            ["kubectl", "config", "delete-context", cluster_name],
            ["kubectl", "config", "delete-cluster", cluster_name],
            ["kubectl", "config", "delete-user", f"clusterUser_modelops-{self.env}-rg-{os.environ.get('USER', 'user')}_{cluster_name}"]
        ]

        for cmd in commands:
            subprocess.run(cmd, capture_output=True, stderr=subprocess.DEVNULL, check=False)

    def _merge_kubeconfig(self, kubeconfig_yaml: str):
        """Merge kubeconfig with existing ~/.kube/config."""
        import tempfile
        import shutil

        # Create temp file with new kubeconfig
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(kubeconfig_yaml)
            temp_path = f.name

        try:
            # Backup existing config
            kube_dir = Path.home() / ".kube"
            kube_dir.mkdir(exist_ok=True)
            config_path = kube_dir / "config"

            if config_path.exists():
                backup_path = kube_dir / "config.backup"
                shutil.copy(config_path, backup_path)

            # Use kubectl to merge configs
            env_vars = os.environ.copy()
            if config_path.exists():
                env_vars["KUBECONFIG"] = f"{config_path}:{temp_path}"
            else:
                env_vars["KUBECONFIG"] = temp_path

            result = subprocess.run(
                ["kubectl", "config", "view", "--flatten"],
                env=env_vars,
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                raise Exception(f"Failed to merge kubeconfig: {result.stderr}")

            # Write merged config
            config_path.write_text(result.stdout)

        finally:
            # Clean up temp file
            os.unlink(temp_path)