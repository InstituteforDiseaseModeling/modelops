"""Azure infrastructure component using Pulumi ComponentResource.

Creates all Azure resources from zero for ModelOps deployment.
"""

import base64
import os
import re
import pulumi
import pulumi_azure_native as azure
from typing import Dict, Any, List, Optional
from pathlib import Path
from ...core import StackNaming


class ModelOpsCluster(pulumi.ComponentResource):
    """Stack 1: Infrastructure plane - creates Azure cloud resources.
    
    Creates Resource Group, optional ACR, and AKS cluster with labeled node pools.
    Exports typed outputs for downstream consumption via StackReference.
    """
    
    def __init__(self, name: str, config: Dict[str, Any], 
                 opts: Optional[pulumi.ResourceOptions] = None):
        """Initialize Azure infrastructure component.
        
        Args:
            name: Component name (e.g., "modelops")
            config: Provider configuration dictionary from YAML
            opts: Optional Pulumi resource options
        """
        super().__init__("modelops:infra:cluster", name, None, opts)
        
        # Extract configuration with defaults
        # Get subscription ID from config or environment
        subscription_id = config.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID")
        if not subscription_id:
            raise ValueError("Azure subscription ID required: set in config or AZURE_SUBSCRIPTION_ID env var")
        
        location = config.get("location", "eastus2")
        env = config.get("environment", "dev")  # Get environment from config
        
        # Get username for per-user resource group
        username = self._get_username(config)
        rg_name = StackNaming.get_resource_group_name(env, username)
        
        aks_config = config.get("aks", {})
        ssh_config = config.get("ssh", {})
        
        # Create or get existing Resource Group (idempotent)
        rg = self._ensure_resource_group(
            name=name,
            rg_name=rg_name,
            location=location,
            subscription_id=subscription_id,
            username=username
        )
        
        # Get or generate SSH key
        ssh_pubkey = self._get_ssh_key(ssh_config)
        
        # Create AKS cluster with node pools
        aks = self._create_aks_cluster(name, rg, location, aks_config, ssh_pubkey, env)
        
        # Get kubeconfig using the *actual* cluster name emitted by the resource
        # This handles auto-naming correctly
        creds = azure.containerservice.list_managed_cluster_user_credentials_output(
            resource_group_name=rg.name,
            resource_name=aks.name,  # Use the actual ARM name from the resource
            opts=pulumi.InvokeOptions(parent=self)
        )
        
        kubeconfig = creds.kubeconfigs[0].value.apply(
            lambda b64: base64.b64decode(b64).decode("utf-8")
        )
        
        # Set component outputs
        self.kubeconfig = kubeconfig
        self.cluster_name = aks.name  # Use the actual resource name
        self.resource_group = rg.name
        self.location = pulumi.Output.from_input(location)
        
        # Register outputs for StackReference access
        self.register_outputs({
            "kubeconfig": pulumi.Output.secret(self.kubeconfig),
            "cluster_name": self.cluster_name,
            "resource_group": self.resource_group,
            "location": self.location,
            "provider": pulumi.Output.from_input("azure")
        })
    
    def _get_ssh_key(self, ssh_config: Dict[str, Any]) -> str:
        """Get SSH public key from config or generate ephemeral."""
        # Try config first
        if ssh_config.get("public_key"):
            return ssh_config["public_key"]
        
        # Try file path
        if ssh_config.get("public_key_path"):
            key_path = Path(ssh_config["public_key_path"]).expanduser()
            if key_path.exists():
                return key_path.read_text().strip()
        
        # Generate a valid ephemeral SSH key for MVP
        pulumi.log.warn("Using ephemeral SSH key. Provide ssh.public_key or ssh.public_key_path in production.")
        # This is a valid but insecure SSH key for testing only
        # TODO/PLACEHOLDER: integrate real key generation
        return "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDPaTXgcq3a8qEWVN9F4kPTXogk0cKAQjWLoyaGtOnostXCoL3pMPmsCyEh2H8xGr7kvTaLQuKcfa5+gOqE+mRwQO3OtMr7K1UoGlEO5V3rZPZLnLrOuWtrCbBVXLxBvlNfWPqRaMQT6N/06xrB2V3aF5YQtpt3zPLbMjU7miPVMvjV0pXQHCioFUDJmnFBcOT/EQlhxMKSqeMmQ+BYXQV7TT3aCrJiM6oC7Pa2h9REd5HDnZ5fwmGOXF3H0gxR8sPEEofV1tRFmacnVzk5tfoT0z0adPNDBoMD7bxs5xB5LQXdV8K9n5RYPsJc1p7Ms8pqXAQUJdGFaHOjKiGJjvPT modelops-ephemeral@azure"
    
    def _ensure_resource_group(self, name: str, rg_name: str, location: str,
                              subscription_id: str, username: str) -> azure.resources.ResourceGroup:
        """Create or get existing resource group (idempotent).
        
        This method handles the case where a resource group already exists in Azure
        but may not be in the Pulumi state. It attempts to use an existing RG if found,
        otherwise creates a new one.
        """
        rg_id = f"/subscriptions/{subscription_id}/resourceGroups/{rg_name}"
        
        # Try to check if resource group exists in Azure
        try:
            # Attempt to get the existing resource group
            existing_rg_result = azure.resources.get_resource_group(
                resource_group_name=rg_name,
                opts=pulumi.InvokeOptions(parent=self)
            )
            
            # If we get here, the RG exists in Azure
            # Use ResourceGroup.get to import it into our state
            pulumi.log.info(f"Resource group '{rg_name}' already exists, importing into state")
            
            rg = azure.resources.ResourceGroup.get(
                f"{name}-rg",
                id=rg_id,
                opts=pulumi.ResourceOptions(
                    parent=self,
                    protect=True,  # Prevent accidental deletion
                    retain_on_delete=True  # Retain on replacement
                )
            )
            
            return rg
            
        except Exception as e:
            # Resource group doesn't exist or we can't access it
            # Create a new one
            pulumi.log.info(f"Creating new resource group: {rg_name}")
            
            rg = azure.resources.ResourceGroup(
                f"{name}-rg",
                resource_group_name=rg_name,
                location=location,
                tags={
                    "managed-by": "modelops",
                    "project": "modelops",
                    "component": name,
                    "user": username
                },
                opts=pulumi.ResourceOptions(
                    parent=self,
                    protect=True,  # Prevent accidental deletion
                    retain_on_delete=True  # Also retain on replacement
                )
            )
            
            return rg
    
    def _create_aks_cluster(self, name: str, rg: azure.resources.ResourceGroup,
                           location: str, aks_config: Dict[str, Any], 
                           ssh_pubkey: str, env: str) -> azure.containerservice.ManagedCluster:
        """Create AKS cluster with configured node pools."""
        # Use centralized naming for AKS cluster
        cluster_name = StackNaming.get_aks_cluster_name(env)
        # Make K8s version optional - Azure will use latest stable if not specified
        k8s_version = aks_config.get("kubernetes_version")
        
        # Build node pool profiles
        node_pools = self._build_node_pools(aks_config.get("node_pools", []))
        
        # Create the AKS cluster
        # First positional arg IS resource_name in Pulumi Azure Native
        aks_resource = azure.containerservice.ManagedCluster(
            cluster_name,  # This becomes the Azure resource name
            resource_group_name=rg.name,
            location=location,
            dns_prefix=f"{cluster_name}-dns",
            kubernetes_version=k8s_version if k8s_version else None,
            identity=azure.containerservice.ManagedClusterIdentityArgs(
                type="SystemAssigned"
            ),
            linux_profile=azure.containerservice.ContainerServiceLinuxProfileArgs(
                admin_username="azureuser",
                ssh=azure.containerservice.ContainerServiceSshConfigurationArgs(
                    public_keys=[
                        azure.containerservice.ContainerServiceSshPublicKeyArgs(
                            key_data=ssh_pubkey
                        )
                    ]
                )
            ),
            agent_pool_profiles=node_pools,
            network_profile=azure.containerservice.ContainerServiceNetworkProfileArgs(
                network_plugin="azure",
                service_cidr="10.0.0.0/16",
                dns_service_ip="10.0.0.10"
            ),
            tags={
                "managed-by": "modelops",
                "project": "modelops",
                "component": name
            },
            opts=pulumi.ResourceOptions(parent=self)
        )
        
        return aks_resource
    
    def _build_node_pools(self, node_pools_config: List[Dict[str, Any]]) -> List:
        """Build AKS node pool profiles from configuration."""
        if not node_pools_config:
            # Default pools if none specified
            node_pools_config = [
                {
                    "name": "system",
                    "vm_size": "Standard_DS2_v2",
                    "count": 1,
                    "mode": "System"
                },
                {
                    "name": "cpuworkers",
                    "vm_size": "Standard_DS3_v2",
                    "min": 2,
                    "max": 5,
                    "mode": "User",
                    "labels": {"modelops.io/role": "cpu"},
                    "taints": ["modelops.io/role=cpu:NoSchedule"]
                }
            ]
        
        profiles = []
        for idx, pool in enumerate(node_pools_config):
            mode = pool.get("mode", "System" if idx == 0 else "User")
            
            # Build profile based on scaling type
            if "min" in pool and "max" in pool:
                # Auto-scaling pool
                profile = azure.containerservice.ManagedClusterAgentPoolProfileArgs(
                    name=pool["name"],
                    vm_size=pool.get("vm_size", "Standard_DS2_v2"),
                    mode=mode,
                    os_type="Linux",
                    type="VirtualMachineScaleSets",
                    enable_auto_scaling=True,
                    min_count=pool["min"],
                    max_count=pool["max"],
                    count=pool.get("count", pool["min"]),
                    node_labels=pool.get("labels", {}),
                    node_taints=self._format_taints_for_azure(pool.get("taints", []))
                )
            else:
                # Fixed size pool
                profile = azure.containerservice.ManagedClusterAgentPoolProfileArgs(
                    name=pool["name"],
                    vm_size=pool.get("vm_size", "Standard_DS2_v2"),
                    mode=mode,
                    os_type="Linux",
                    type="VirtualMachineScaleSets",
                    enable_auto_scaling=False,
                    count=pool.get("count", 1),
                    node_labels=pool.get("labels", {}),
                    node_taints=self._format_taints_for_azure(pool.get("taints", []))
                )
            
            profiles.append(profile)
        
        return profiles
    
    def _format_taints_for_azure(self, taints: list) -> list:
        """Format taints for Azure API.
        
        Converts Taint objects or strings to Azure's expected string format.
        Azure expects strings like 'key=value:Effect' or 'key:Effect'.
        """
        formatted = []
        for taint in taints:
            if isinstance(taint, str):
                # Already in string format
                formatted.append(taint)
            elif hasattr(taint, 'to_azure_format'):
                # It's a Taint object with formatting method
                formatted.append(taint.to_azure_format())
            elif isinstance(taint, dict):
                # Dict format from YAML
                key = taint.get('key', '')
                value = taint.get('value', '')
                effect = taint.get('effect', 'NoSchedule')
                if value:
                    formatted.append(f"{key}={value}:{effect}")
                else:
                    formatted.append(f"{key}:{effect}")
            else:
                # Try to convert to string
                formatted.append(str(taint))
        return formatted
    
    def _get_username(self, config: Dict[str, Any]) -> str:
        """Get username for per-user resource group.
        
        Priority: config > environment > error
        Sanitizes username for Azure resource group naming requirements.
        """
        # Check config first
        if config.get("username"):
            username = config["username"]
        else:
            # Get username from config or system
            from ...core.config import get_username
            username = get_username()
        
        # Sanitize for Azure RG naming (alphanumeric, hyphen, underscore)
        import re
        username = re.sub(r'[^a-zA-Z0-9-]', '', username).lower()
        
        # Azure RG names have max length, truncate if needed
        return username[:20]
