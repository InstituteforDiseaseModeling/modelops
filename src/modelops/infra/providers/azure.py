"""Azure provider for AKS-based workspaces."""

import base64
import pulumi
import pulumi_kubernetes as k8s
from typing import Dict, Any, Optional

# Azure imports are conditional - only if azure extra is installed
try:
    import pulumi_azure_native as azure
    HAS_AZURE = True
except ImportError:
    HAS_AZURE = False

from .base import WorkspaceProvider


class AzureProvider(WorkspaceProvider):
    """Provider for Azure AKS clusters.
    
    This provider uses Azure SDK through Pulumi to provision resources
    without subprocess calls, maintaining determinism for Pulumi programs.
    
    Requires the 'azure' extra: pip install modelops[azure]
    """
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize Azure provider.
        
        Args:
            config: Provider configuration
            
        Raises:
            ImportError: If pulumi-azure-native is not installed
        """
        super().__init__(config)
        
        if not HAS_AZURE:
            raise ImportError(
                "Azure provider requires pulumi-azure-native. "
                "Install with: pip install modelops[azure]"
            )
    
    def validate(self) -> None:
        """Validate Azure configuration.
        
        Checks that required fields are present in the configuration.
        Does not make external calls to maintain Pulumi determinism.
        
        Raises:
            ValueError: If required configuration is missing
        """
        spec = self.config.get("spec", {})
        
        # Check required fields
        required_fields = ["subscription_id", "resource_group", "aks_cluster"]
        missing = [f for f in required_fields if not spec.get(f)]
        
        if missing:
            raise ValueError(
                f"Azure provider requires these fields in spec: {', '.join(missing)}"
            )
        
        # Note: We don't check Azure credentials here to avoid external calls
        # Pulumi will handle auth through Azure CLI or environment variables
    
    def get_k8s_provider(self) -> Optional[k8s.Provider]:
        """Get Pulumi Kubernetes provider configured for AKS.
        
        Uses Pulumi Azure SDK to get AKS credentials without subprocess calls.
        
        Returns:
            Configured Kubernetes provider for AKS
        """
        spec = self.config["spec"]
        
        # Get AKS credentials using Pulumi Azure SDK
        creds = azure.containerservice.list_managed_cluster_user_credentials_output(
            resource_group_name=spec["resource_group"],
            resource_name=spec["aks_cluster"]
        )
        
        # Decode kubeconfig from base64
        # The credentials come as base64-encoded, we need to decode them
        kubeconfig = creds.kubeconfigs[0].value.apply(
            lambda b64: base64.b64decode(b64).decode("utf-8")
        )
        
        # Create and return K8s provider with AKS kubeconfig
        return k8s.Provider(
            "aks-provider",
            kubeconfig=kubeconfig
        )
    
    def setup_storage(self) -> Dict[str, Any]:
        """Setup Azure Storage for artifact storage.
        
        For MVP, this is optional. If storage is configured, creates
        connection strings using Pulumi SDK (no subprocess).
        
        Returns:
            Dictionary with secret_data containing storage credentials,
            or empty dict if storage is not configured
        """
        spec = self.config["spec"]
        storage = spec.get("storage", {})
        
        if not storage.get("account"):
            # No storage configured - that's fine for MVP
            # Workspaces can run without cloud storage
            return {}
        
        # Get storage account key using Pulumi Azure SDK
        keys = azure.storage.list_storage_account_keys_output(
            account_name=storage["account"],
            resource_group_name=spec["resource_group"]
        )
        
        # Build connection string as a Pulumi Output
        # This ensures the secret is properly handled by Pulumi
        connection_string = pulumi.Output.concat(
            "DefaultEndpointsProtocol=https;",
            "AccountName=", storage["account"], ";",
            "AccountKey=", keys.keys[0].value, ";",
            "EndpointSuffix=core.windows.net"
        )
        
        # Mark as secret to prevent leaking in logs
        connection_string_secret = pulumi.Output.secret(connection_string)
        
        return {
            "secret_data": {
                "connection_string": connection_string_secret,
                "account_name": storage["account"]
            }
        }
        
        # TODO: Future improvement - use Workload Identity instead of keys
        # This would eliminate the need for storage account keys entirely
    
    def get_storage_secret_data(self) -> Dict[str, str]:
        """Get secret data for pod storage access.
        
        Returns:
            Dictionary of secret keys and values for Kubernetes secret
        """
        # This is handled by setup_storage() returning Pulumi Outputs
        # The WorkspaceStack will handle creating the K8s secret
        return {}
    
    def get_resource_defaults(self) -> Dict[str, Any]:
        """Get resource defaults for cloud/production.
        
        Azure AKS can handle production workloads with more resources.
        
        Returns:
            Dictionary with production-appropriate resource settings
        """
        return {
            "min_workers": 2,
            "max_workers": 10,
            "worker_memory": "4Gi",
            "worker_cpu": "2",
            "scheduler_memory": "2Gi",
            "scheduler_cpu": "1"
        }
    
    def get_labels(self) -> Dict[str, str]:
        """Get standard labels for resources.
        
        Returns:
            Dictionary of labels to apply to all resources
        """
        return {
            "app.kubernetes.io/managed-by": "modelops",
            "modelops.io/provider": "azure",
            "modelops.io/environment": "cloud"
        }