"""Azure infrastructure bootstrapping from zero.

Creates all Azure resources required for ModelOps following the spec.
"""

import pulumi
import pulumi_azure_native as azure
from typing import Dict, Any, List
import base64
from pathlib import Path

from .bindings import ClusterBinding


def create_azure_infrastructure(config: Dict[str, Any]) -> ClusterBinding:
    """Create Azure infrastructure from zero based on configuration.
    
    Creates:
    - Resource Group
    - AKS cluster with labeled node pools
    - Optional ACR for container registry
    
    Args:
        config: Provider configuration dictionary from YAML
        
    Returns:
        ClusterBinding with kubeconfig and cluster information
    """
    # Extract configuration
    subscription_id = config["subscription_id"]
    location = config.get("location", "eastus2")
    resource_group_name = config.get("resource_group", "modelops-rg")
    
    aks_config = config.get("aks", {})
    cluster_name = aks_config.get("name", "modelops-aks")
    node_pools = aks_config.get("node_pools", [])
    
    # Create Resource Group
    resource_group = azure.resources.ResourceGroup(
        resource_group_name,
        resource_group_name=resource_group_name,
        location=location,
        tags={
            "managed-by": "modelops",
            "project": "modelops"
        }
    )
    
    # Create ACR if requested
    acr_login_server = None
    if config.get("acr"):
        acr_config = config["acr"]
        registry = azure.containerregistry.Registry(
            acr_config["name"],
            registry_name=acr_config["name"],
            resource_group_name=resource_group.name,
            location=location,
            sku=azure.containerregistry.SkuArgs(
                name=acr_config.get("sku", "Standard")
            ),
            admin_user_enabled=True  # For MVP; use managed identity in production
        )
        acr_login_server = registry.login_server
    
    # Get SSH key for Linux nodes
    ssh_config = config.get("ssh", {})
    ssh_pubkey = None
    
    # Try to get SSH key from config or generate ephemeral
    if ssh_config.get("public_key"):
        ssh_pubkey = ssh_config["public_key"]
    elif ssh_config.get("public_key_path"):
        key_path = Path(ssh_config["public_key_path"]).expanduser()
        if key_path.exists():
            ssh_pubkey = key_path.read_text().strip()
    
    # Generate ephemeral key if none provided (for MVP)
    if not ssh_pubkey:
        # Use a dummy key for MVP - in production, generate or require real key
        ssh_pubkey = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDTnGnX6pPRnxJ+7k7M3Bgz modelops-ephemeral@azure"
        pulumi.log.warn("Using ephemeral SSH key for AKS. Provide ssh.public_key or ssh.public_key_path in production.")
    
    # Create AKS cluster with node pools
    agent_pool_profiles = _create_node_pool_profiles(node_pools)
    
    aks_cluster = azure.containerservice.ManagedCluster(
        resource_name=cluster_name,
        resource_group_name=resource_group.name,
        location=location,
        dns_prefix=f"{cluster_name}-dns",
        kubernetes_version=aks_config.get("kubernetes_version", "1.32"),
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
        agent_pool_profiles=agent_pool_profiles,
        network_profile=azure.containerservice.ContainerServiceNetworkProfileArgs(
            network_plugin="azure",
            service_cidr="10.0.0.0/16",
            dns_service_ip="10.0.0.10"
        ),
        tags={
            "managed-by": "modelops",
            "project": "modelops"
        }
    )
    
    # Get kubeconfig
    creds = azure.containerservice.list_managed_cluster_user_credentials_output(
        resource_group_name=resource_group.name,
        resource_name=aks_cluster.name
    )
    
    # Decode kubeconfig from base64
    kubeconfig = creds.kubeconfigs[0].value.apply(
        lambda b64: base64.b64decode(b64).decode("utf-8")
    )
    
    # Create ClusterBinding
    return pulumi.Output.all(
        kubeconfig=kubeconfig,
        cluster_name=aks_cluster.name,
        resource_group=resource_group.name,
        location=location,
        acr_login_server=acr_login_server
    ).apply(lambda args: ClusterBinding(
        kubeconfig=args["kubeconfig"],
        provider="azure",
        cluster_name=args["cluster_name"],
        resource_group=args["resource_group"],
        location=args["location"],
        acr_login_server=args["acr_login_server"]
    ))


def _create_node_pool_profiles(node_pools: List[Dict[str, Any]]) -> List:
    """Create AKS agent pool profiles from configuration.
    
    Args:
        node_pools: List of node pool configurations
        
    Returns:
        List of ManagedClusterAgentPoolProfileArgs
    """
    if not node_pools:
        # Default node pools if none specified
        node_pools = [
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
    for idx, pool in enumerate(node_pools):
        # Determine if this is a system or user pool
        mode = pool.get("mode", "System" if idx == 0 else "User")
        
        # Handle both fixed and auto-scaling pools
        if "min" in pool and "max" in pool:
            # Auto-scaling pool
            profile = azure.containerservice.ManagedClusterAgentPoolProfileArgs(
                name=pool["name"],
                vm_size=pool.get("vm_size", "Standard_DS2_v2"),
                mode=mode,
                os_type="Linux",
                enable_auto_scaling=True,
                min_count=pool["min"],
                max_count=pool["max"],
                count=pool.get("count", pool["min"]),  # Initial count
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
                enable_auto_scaling=False,
                count=pool.get("count", 1),
                node_labels=pool.get("labels", {}),
                node_taints=pool.get("taints", [])
            )
        
        profiles.append(profile)
    
    return profiles