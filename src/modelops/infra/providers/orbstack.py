"""OrbStack provider for local Kubernetes development."""

from typing import Dict, Any, Optional
from .base import WorkspaceProvider


class OrbStackProvider(WorkspaceProvider):
    """Provider for OrbStack local Kubernetes.
    
    OrbStack provides a lightweight Kubernetes environment for local development.
    This provider assumes kubectl is configured with the correct context.
    """
    
    def validate(self) -> None:
        """Validate OrbStack configuration.
        
        Checks that required configuration fields are present.
        Does not use subprocess to maintain Pulumi determinism.
        
        Raises:
            ValueError: If configuration is invalid
        """
        spec = self.config.get("spec", {})
        
        # Check required fields
        if not spec.get("context"):
            raise ValueError(
                "OrbStack provider requires 'context' in spec. "
                "Common values: 'orbstack', 'docker-desktop'"
            )
        
        # Note: User must ensure kubectl is configured correctly
        # We don't check this here to avoid subprocess calls
    
    def get_k8s_provider(self) -> Optional[Any]:
        """Get Kubernetes provider for OrbStack.
        
        Returns None to use the default kubeconfig.
        Pulumi will use KUBECONFIG env var or ~/.kube/config.
        
        Returns:
            None - uses default kubeconfig
        """
        # OrbStack uses standard kubeconfig
        # User must ensure correct context is set
        return None
    
    def setup_storage(self) -> Dict[str, Any]:
        """Setup storage for OrbStack.
        
        For local development, we don't need cloud storage.
        Pods can use emptyDir or hostPath volumes if needed.
        
        Returns:
            Empty dict - no storage secrets needed
        """
        storage_type = self.config.get("spec", {}).get("storage", {}).get("type", "emptydir")
        
        if storage_type == "hostpath":
            # Could return hostPath configuration here
            # But for MVP, we'll keep it simple
            return {}
        else:
            # EmptyDir is the default - no setup needed
            return {}
    
    def get_storage_secret_data(self) -> Dict[str, str]:
        """Get secret data for storage access.
        
        OrbStack doesn't need storage secrets for local development.
        
        Returns:
            Empty dict - no secrets needed
        """
        return {}
    
    def get_resource_defaults(self) -> Dict[str, Any]:
        """Get resource defaults for local development.
        
        OrbStack runs locally, so we use minimal resources.
        
        Returns:
            Dictionary with local-appropriate resource settings
        """
        return {
            "min_workers": 1,
            "max_workers": 3,  # Local can't handle too many
            "worker_memory": "512Mi",
            "worker_cpu": "0.5",
            "scheduler_memory": "512Mi",
            "scheduler_cpu": "0.5"
        }
    
    def get_labels(self) -> Dict[str, str]:
        """Get standard labels for resources.
        
        Returns:
            Dictionary of labels to apply to all resources
        """
        return {
            "app.kubernetes.io/managed-by": "modelops",
            "modelops.io/provider": "orbstack",
            "modelops.io/environment": "local"
        }