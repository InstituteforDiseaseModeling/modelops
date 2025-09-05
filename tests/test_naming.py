"""Tests for centralized naming module."""

import pytest
from modelops.core.naming import StackNaming


class TestStackNaming:
    """Test suite for StackNaming class."""
    
    def test_project_prefix(self):
        """Test that PROJECT_PREFIX is correctly defined."""
        assert StackNaming.PROJECT_PREFIX == "modelops"
    
    def test_get_stack_name(self):
        """Test stack name generation."""
        # Basic stack name
        assert StackNaming.get_stack_name("infra", "dev") == "modelops-infra-dev"
        assert StackNaming.get_stack_name("workspace", "prod") == "modelops-workspace-prod"
        assert StackNaming.get_stack_name("adaptive", "staging") == "modelops-adaptive-staging"
        
        # With run_id
        assert StackNaming.get_stack_name("adaptive", "dev", "run-123") == "modelops-adaptive-dev-run-123"
        assert StackNaming.get_stack_name("adaptive", "prod", "exp-001") == "modelops-adaptive-prod-exp-001"
    
    def test_get_project_name(self):
        """Test project name generation."""
        assert StackNaming.get_project_name("infra") == "modelops-infra"
        assert StackNaming.get_project_name("workspace") == "modelops-workspace"
        assert StackNaming.get_project_name("adaptive") == "modelops-adaptive"
    
    def test_get_resource_group_name(self):
        """Test Azure resource group name generation."""
        # Without username
        assert StackNaming.get_resource_group_name("dev") == "modelops-dev-rg"
        assert StackNaming.get_resource_group_name("prod") == "modelops-prod-rg"
        
        # With username
        assert StackNaming.get_resource_group_name("dev", "alice") == "modelops-dev-rg-alice"
        assert StackNaming.get_resource_group_name("staging", "bob.smith") == "modelops-staging-rg-bobsmith"
        assert StackNaming.get_resource_group_name("prod", "user-123") == "modelops-prod-rg-user123"
    
    def test_get_aks_cluster_name(self):
        """Test AKS cluster name generation."""
        assert StackNaming.get_aks_cluster_name("dev") == "modelops-dev-aks"
        assert StackNaming.get_aks_cluster_name("staging") == "modelops-staging-aks"
        assert StackNaming.get_aks_cluster_name("prod") == "modelops-prod-aks"
    
    def test_get_acr_name(self):
        """Test Azure Container Registry name generation."""
        # Without suffix (auto-generated)
        name = StackNaming.get_acr_name("dev")
        assert name.startswith("modelopsdevacr")
        assert len(name) == len("modelopsdevacr") + 4  # 4 char suffix
        
        # With suffix
        assert StackNaming.get_acr_name("dev", "test") == "modelopsdevacrtest"
        assert StackNaming.get_acr_name("prod", "123") == "modelopsprodacr123"
        
        # Test cleaning of non-alphanumeric
        assert StackNaming.get_acr_name("dev-test", "abc") == "modelopsdevtestacrabc"
    
    def test_get_storage_account_name(self):
        """Test Azure Storage Account name generation."""
        # Without suffix (auto-generated)
        name = StackNaming.get_storage_account_name("dev")
        assert name.startswith("modelopsdevst")
        assert len(name) <= 24  # Azure limit
        
        # With suffix - now includes random part
        name_with_suffix = StackNaming.get_storage_account_name("dev", "test")
        assert name_with_suffix.startswith("modelopsdevtes")  # Truncated + random
        assert len(name_with_suffix) <= 24
        # Storage account names now always add random suffix for uniqueness
        prod_name = StackNaming.get_storage_account_name("prod", "123") 
        assert prod_name.startswith("modelopsprdst")  # Note: "prod" gets truncated to "prd"
        assert len(prod_name) <= 24
        
        # Test truncation for long names
        long_name = StackNaming.get_storage_account_name("verylongenvironmentname", "suffix")
        assert len(long_name) <= 24
    
    def test_get_namespace(self):
        """Test Kubernetes namespace generation."""
        assert StackNaming.get_namespace("dask", "dev") == "modelops-dask-dev"
        assert StackNaming.get_namespace("adaptive", "prod") == "modelops-adaptive-prod"
        assert StackNaming.get_namespace("monitoring", "staging") == "modelops-monitoring-staging"
    
    def test_sanitize_username(self):
        """Test username sanitization."""
        assert StackNaming.sanitize_username("alice") == "alice"
        assert StackNaming.sanitize_username("Bob.Smith") == "bobsmith"
        assert StackNaming.sanitize_username("user-123") == "user123"
        assert StackNaming.sanitize_username("john@example.com") == "johnexamplecom"
        assert StackNaming.sanitize_username("UPPERCASE") == "uppercase"
        
        # Test length limit
        long_username = "verylongusernamethatexceedstheazurelimit"
        assert len(StackNaming.sanitize_username(long_username)) == 20
    
    def test_get_infra_stack_ref(self):
        """Test infrastructure stack reference helper."""
        assert StackNaming.get_infra_stack_ref("dev") == "modelops-infra-dev"
        assert StackNaming.get_infra_stack_ref("prod") == "modelops-infra-prod"
    
    def test_get_workspace_stack_ref(self):
        """Test workspace stack reference helper."""
        assert StackNaming.get_workspace_stack_ref("dev") == "modelops-workspace-dev"
        assert StackNaming.get_workspace_stack_ref("staging") == "modelops-workspace-staging"
    
    def test_parse_stack_name(self):
        """Test parsing stack names back into components."""
        # Basic parsing
        parsed = StackNaming.parse_stack_name("modelops-infra-dev")
        assert parsed["prefix"] == "modelops"
        assert parsed["component"] == "infra"
        assert parsed["env"] == "dev"
        assert "run_id" not in parsed
        
        # With run_id
        parsed = StackNaming.parse_stack_name("modelops-adaptive-prod-run-123")
        assert parsed["prefix"] == "modelops"
        assert parsed["component"] == "adaptive"
        assert parsed["env"] == "prod"
        assert parsed["run_id"] == "run-123"
        
        # Complex run_id with hyphens
        parsed = StackNaming.parse_stack_name("modelops-adaptive-dev-exp-001-test")
        assert parsed["prefix"] == "modelops"
        assert parsed["component"] == "adaptive"
        assert parsed["env"] == "dev"
        assert parsed["run_id"] == "exp-001-test"
        
        # Invalid format
        with pytest.raises(ValueError, match="Invalid stack name format"):
            StackNaming.parse_stack_name("invalid-name")
        
        with pytest.raises(ValueError, match="Invalid stack name format"):
            StackNaming.parse_stack_name("modelops")
    
    def test_consistency(self):
        """Test that all methods use PROJECT_PREFIX consistently."""
        # Change the prefix and verify all methods use it
        original_prefix = StackNaming.PROJECT_PREFIX
        try:
            StackNaming.PROJECT_PREFIX = "testproject"
            
            assert StackNaming.get_stack_name("infra", "dev") == "testproject-infra-dev"
            assert StackNaming.get_project_name("infra") == "testproject-infra"
            assert StackNaming.get_resource_group_name("dev") == "testproject-dev-rg"
            assert StackNaming.get_aks_cluster_name("dev") == "testproject-dev-aks"
            assert StackNaming.get_namespace("dask", "dev") == "testproject-dask-dev"
            assert StackNaming.get_infra_stack_ref("dev") == "testproject-infra-dev"
            assert StackNaming.get_workspace_stack_ref("dev") == "testproject-workspace-dev"
            
            # ACR and storage account names
            acr_name = StackNaming.get_acr_name("dev", "test")
            assert acr_name == "testprojectdevacrtest"
            
            storage_name = StackNaming.get_storage_account_name("dev", "test")
            assert storage_name.startswith("testprojectdevtes")  # Truncated + random
            assert len(storage_name) <= 24
            
        finally:
            # Restore original prefix
            StackNaming.PROJECT_PREFIX = original_prefix