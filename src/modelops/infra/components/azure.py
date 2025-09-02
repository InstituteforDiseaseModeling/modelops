"""Azure infrastructure component using Pulumi ComponentResource.

Creates all Azure resources from zero for ModelOps deployment.
"""

import base64
import pulumi
import pulumi_azure_native as azure
from typing import Dict, Any, List, Optional
from pathlib import Path


class AzureModelOpsInfra(pulumi.ComponentResource):
    """Encapsulates all Azure infrastructure from zero.
    
    Creates Resource Group, optional ACR, and AKS cluster with labeled node pools.
    Exports typed outputs for downstream consumption via ClusterBinding.
    """
    
    def __init__(self, name: str, config: Dict[str, Any], 
                 opts: Optional[pulumi.ResourceOptions] = None):
        """Initialize Azure infrastructure component.
        
        Args:
            name: Component name (e.g., "modelops")
            config: Provider configuration dictionary from YAML
            opts: Optional Pulumi resource options
        """
        super().__init__("modelops:infra:azure", name, None, opts)
        
        # Extract configuration with defaults
        subscription_id = config["subscription_id"]
        location = config.get("location", "eastus2")
        rg_name = config.get("resource_group", "modelops-rg")
        aks_config = config.get("aks", {})
        acr_config = config.get("acr")
        ssh_config = config.get("ssh", {})
        
        # Create Resource Group
        rg = azure.resources.ResourceGroup(
            f"{name}-rg",
            resource_group_name=rg_name,
            location=location,
            tags={
                "managed-by": "modelops",
                "project": "modelops",
                "component": name
            },
            opts=pulumi.ResourceOptions(parent=self)
        )
        
        # Create optional ACR
        acr_login_server = None
        if acr_config:
            acr = self._create_acr(name, acr_config, rg, location)
            acr_login_server = acr.login_server
        
        # Get or generate SSH key
        ssh_pubkey = self._get_ssh_key(ssh_config)
        
        # Create AKS cluster with node pools
        aks = self._create_aks_cluster(name, rg, location, aks_config, ssh_pubkey)
        
        # Setup ACR pull permissions if ACR exists
        if acr_config and acr:
            self._setup_acr_pull(aks, acr, subscription_id)
        
        # Get kubeconfig
        creds = azure.containerservice.list_managed_cluster_user_credentials_output(
            resource_group_name=rg.name,
            resource_name=aks.name
        )
        
        kubeconfig = creds.kubeconfigs[0].value.apply(
            lambda b64: base64.b64decode(b64).decode("utf-8")
        )
        
        # Set component outputs
        self.kubeconfig = kubeconfig
        self.cluster_name = aks.name
        self.resource_group = rg.name
        self.location = pulumi.Output.from_input(location)
        self.acr_login_server = acr_login_server if acr_login_server else None
        
        # Register outputs for state
        self.register_outputs({
            "kubeconfig": pulumi.Output.secret(self.kubeconfig),
            "cluster_name": self.cluster_name,
            "resource_group": self.resource_group,
            "location": self.location,
            "acr_login_server": self.acr_login_server
        })
    
    def _create_acr(self, name: str, acr_config: Dict[str, Any], 
                    rg: azure.resources.ResourceGroup, location: str) -> azure.containerregistry.Registry:
        """Create Azure Container Registry."""
        return azure.containerregistry.Registry(
            f"{name}-acr",
            registry_name=acr_config["name"],
            resource_group_name=rg.name,
            location=location,
            sku=azure.containerregistry.SkuArgs(
                name=acr_config.get("sku", "Standard")
            ),
            admin_user_enabled=False,  # Use managed identity
            opts=pulumi.ResourceOptions(parent=self)
        )
    
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
        
        # Fallback to ephemeral key for MVP
        pulumi.log.warn("Using ephemeral SSH key. Provide ssh.public_key or ssh.public_key_path in production.")
        return "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDTnGnX6pPRnxJ+7k7M3Bgz modelops-ephemeral@azure"
    
    def _create_aks_cluster(self, name: str, rg: azure.resources.ResourceGroup,
                           location: str, aks_config: Dict[str, Any], 
                           ssh_pubkey: str) -> azure.containerservice.ManagedCluster:
        """Create AKS cluster with configured node pools."""
        cluster_name = aks_config.get("name", "modelops-aks")
        k8s_version = aks_config.get("kubernetes_version", "1.32")
        
        # Build node pool profiles
        node_pools = self._build_node_pools(aks_config.get("node_pools", []))
        
        return azure.containerservice.ManagedCluster(
            f"{name}-aks",
            resource_name=cluster_name,
            resource_group_name=rg.name,
            location=location,
            dns_prefix=f"{cluster_name}-dns",
            kubernetes_version=k8s_version,
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
                    node_taints=pool.get("taints", [])
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
                    node_taints=pool.get("taints", [])
                )
            
            profiles.append(profile)
        
        return profiles
    
    def _setup_acr_pull(self, aks: azure.containerservice.ManagedCluster,
                       acr: azure.containerregistry.Registry,
                       subscription_id: str):
        """Setup AcrPull role assignment for AKS to pull from ACR."""
        # Get kubelet identity
        principal_id = aks.identity_profile.apply(
            lambda profile: profile["kubeletidentity"]["object_id"] if profile else None
        )
        
        # AcrPull role definition ID
        acr_pull_role = f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d"
        
        azure.authorization.RoleAssignment(
            f"{aks.name}-acr-pull",
            principal_id=principal_id,
            principal_type="ServicePrincipal",
            role_definition_id=acr_pull_role,
            scope=acr.id,
            opts=pulumi.ResourceOptions(parent=self)
        )