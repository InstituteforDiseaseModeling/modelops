"""Tests for configuration models and validation."""

import pytest
import yaml
import tempfile
from pathlib import Path
from pydantic import ValidationError
from modelops.components import (
    AzureProviderConfig,
    AKSConfig,
    NodePool,
    ACRConfig,
    Taint,
    WorkspaceConfig,
)


class TestAzureProviderConfig:
    """Test Azure provider configuration validation."""
    
    def test_invalid_subscription_id(self):
        """Test invalid subscription ID format."""
        config = {
            "provider": "azure",
            "subscription_id": "invalid-id",
            "resource_group": "test-rg",
            "aks": {
                "name": "test-aks",
                "node_pools": [
                    {"name": "system", "vm_size": "Standard_DS2_v2", "count": 1, "mode": "System"}
                ]
            }
        }
        
        with pytest.raises(ValidationError, match="subscription ID"):
            AzureProviderConfig(**config)
    
    def test_username_derivation(self, monkeypatch):
        """Test username derivation from environment."""
        # Mock get_username to return the test value
        from modelops.core import config as core_config
        monkeypatch.setattr(core_config, "get_username", lambda: "Alice.Smith")
        
        config = {
            "provider": "azure",
            "subscription_id": "00000000-0000-0000-0000-000000000000",
            "resource_group": "test-rg",
            "aks": {
                "name": "test-aks",
                "node_pools": [
                    {"name": "system", "vm_size": "Standard_DS2_v2", "count": 1, "mode": "System"}
                ]
            }
        }
        
        provider = AzureProviderConfig(**config)
        assert provider.username == "alicesmith"  # Sanitized
        assert provider.resource_group_final == "test-rg-alicesmith"


class TestNodePool:
    """Test node pool configuration and validation."""
    
    def test_fixed_size_pool(self):
        """Test fixed size node pool."""
        pool = NodePool(
            name="workers",
            vm_size="Standard_DS3_v2",
            count=5,
            mode="User"
        )
        assert pool.count == 5
        assert pool.min is None
        assert pool.max is None
    
    def test_autoscaling_pool(self):
        """Test autoscaling node pool."""
        pool = NodePool(
            name="workers",
            vm_size="Standard_DS3_v2",
            min=2,
            max=10,
            mode="User"
        )
        assert pool.count is None
        assert pool.min == 2
        assert pool.max == 10
    
    def test_invalid_xor_sizing(self):
        """Test that both count and min/max fails."""
        with pytest.raises(ValidationError, match="Specify either 'count' OR"):
            NodePool(
                name="workers",
                vm_size="Standard_DS3_v2",
                count=5,
                min=2,
                max=10,
                mode="User"
            )
    
    def test_missing_sizing(self):
        """Test that neither count nor min/max fails."""
        with pytest.raises(ValidationError, match="both 'min' and 'max' are required"):
            NodePool(
                name="workers",
                vm_size="Standard_DS3_v2",
                mode="User"
            )
    
    def test_labels_validation(self):
        """Test label key validation."""
        pool = NodePool(
            name="workers",
            vm_size="Standard_DS3_v2",
            count=1,
            labels={"app": "test", "example.com/component": "worker"}
        )
        assert pool.labels["app"] == "test"
        assert pool.labels["example.com/component"] == "worker"
    
    def test_invalid_label_key(self):
        """Test invalid label key format."""
        with pytest.raises(ValidationError, match="Invalid label key"):
            NodePool(
                name="workers",
                vm_size="Standard_DS3_v2",
                count=1,
                labels={"invalid key with spaces": "value"}
            )


class TestTaint:
    """Test taint parsing and validation."""
    
    def test_parse_taint_string_with_value(self):
        """Test parsing taint string with value."""
        taint = Taint.parse("gpu=true:NoSchedule")
        assert taint.key == "gpu"
        assert taint.value == "true"
        assert taint.effect == "NoSchedule"
    
    def test_parse_taint_string_without_value(self):
        """Test parsing taint string without value."""
        taint = Taint.parse("dedicated:NoExecute")
        assert taint.key == "dedicated"
        assert taint.value is None
        assert taint.effect == "NoExecute"
    
    def test_parse_taint_dict(self):
        """Test parsing taint from dictionary."""
        taint = Taint.parse({
            "key": "workload",
            "value": "ml",
            "effect": "PreferNoSchedule"
        })
        assert taint.key == "workload"
        assert taint.value == "ml"
        assert taint.effect == "PreferNoSchedule"
    
    def test_invalid_taint_string(self):
        """Test invalid taint string format."""
        with pytest.raises(ValueError, match="Taint must be"):
            Taint.parse("invalid-format")
    
    def test_to_azure_format(self):
        """Test conversion to Azure format."""
        taint1 = Taint(key="gpu", value="true", effect="NoSchedule")
        assert taint1.to_azure_format() == "gpu=true:NoSchedule"
        
        taint2 = Taint(key="dedicated", value=None, effect="NoExecute")
        assert taint2.to_azure_format() == "dedicated:NoExecute"


class TestAKSConfig:
    """Test AKS configuration validation."""
    
    def test_requires_system_pool(self):
        """Test that at least one System pool is required."""
        with pytest.raises(ValidationError, match="System.*required"):
            AKSConfig(
                name="test-aks",
                kubernetes_version="1.32",
                node_pools=[
                    NodePool(name="workers", vm_size="Standard_DS2_v2", count=3, mode="User")
                ]
            )
    
    def test_valid_aks_config(self):
        """Test valid AKS configuration."""
        config = AKSConfig(
            name="test-aks",
            kubernetes_version="1.32.1",
            node_pools=[
                NodePool(name="system", vm_size="Standard_DS2_v2", count=1, mode="System"),
                NodePool(name="workers", vm_size="Standard_DS3_v2", min=2, max=5, mode="User")
            ]
        )
        assert config.name == "test-aks"
        assert len(config.node_pools) == 2
    
    def test_invalid_kubernetes_version(self):
        """Test invalid Kubernetes version format."""
        with pytest.raises(ValidationError, match="MAJOR.MINOR"):
            AKSConfig(
                name="test-aks",
                kubernetes_version="invalid",
                node_pools=[
                    NodePool(name="system", vm_size="Standard_DS2_v2", count=1, mode="System")
                ]
            )


class TestWorkspaceConfig:
    """Test workspace configuration validation."""
    
    def test_valid_workspace_config(self):
        """Test valid workspace configuration."""
        config = {
            "apiVersion": "modelops/v1",
            "kind": "Workspace",
            "metadata": {
                "name": "test-workspace",
                "namespace": "test-ns"
            },
            "spec": {
                "scheduler": {"image": "dask:latest"},
                "workers": {"image": "dask:latest", "replicas": 4}
            }
        }
        
        workspace = WorkspaceConfig(**config)
        assert workspace.kind == "Workspace"
        assert workspace.metadata["name"] == "test-workspace"
        assert workspace.spec["workers"]["replicas"] == 4
    
    def test_missing_metadata_name(self):
        """Test that metadata.name is required."""
        config = {
            "kind": "Workspace",
            "metadata": {},
            "spec": {}
        }
        
        with pytest.raises(ValidationError, match="metadata.name is required"):
            WorkspaceConfig(**config)
    
    def test_invalid_kind(self):
        """Test that invalid kind is rejected."""
        config = {
            "kind": "InvalidKind",
            "metadata": {"name": "test"},
            "spec": {}
        }
        
        with pytest.raises(ValidationError, match="Input should be 'Workspace'"):
            WorkspaceConfig(**config)
    
    def test_get_namespace_with_env(self):
        """Test namespace generation with environment."""
        config = WorkspaceConfig(
            metadata={"name": "test"},
            spec={
                "scheduler": {"image": "dask:latest"},
                "workers": {"image": "dask:latest", "replicas": 2}
            }
        )
        
        # Should use centralized naming
        namespace = config.get_namespace("dev")
        assert namespace == "modelops-dask-dev"
    
    def test_extra_fields_allowed(self):
        """Test that extra fields are allowed for forward compatibility."""
        config = {
            "apiVersion": "modelops/v1",
            "kind": "Workspace",
            "metadata": {"name": "test"},
            "spec": {
                "scheduler": {"image": "dask:latest"},
                "workers": {"image": "dask:latest", "replicas": 1}
            },
            "future_field": "some_value"  # Extra field
        }
        
        workspace = WorkspaceConfig(**config)
        assert workspace.metadata["name"] == "test"
        # Extra field should be preserved
        assert workspace.model_extra["future_field"] == "some_value"