"""Tests for component dependency validation.

This ensures components cannot be deployed without their required dependencies,
preventing broken Pulumi stacks and cryptic errors.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from modelops.client.utils import (
    DependencyGraph,
    validate_component_dependencies,
    canonicalize_component_name
)
from modelops.client.base import ComponentStatus, ComponentState


class TestDependencyGraph:
    """Test dependency graph functionality."""

    def test_default_dependencies(self):
        """Test that default dependencies are correctly defined."""
        graph = DependencyGraph()

        # Workspace depends on cluster and storage
        assert graph.get_dependencies("workspace") == {"cluster", "storage"}

        # Cluster depends on resource_group and registry
        assert graph.get_dependencies("cluster") == {"resource_group", "registry"}

        # Storage depends on resource_group
        assert graph.get_dependencies("storage") == {"resource_group"}

        # Registry depends on resource_group
        assert graph.get_dependencies("registry") == {"resource_group"}

        # Resource group has no dependencies
        assert graph.get_dependencies("resource_group") == set()

    def test_provision_order(self):
        """Test that provision order respects dependencies."""
        graph = DependencyGraph()

        # Full stack provision order
        components = ["workspace", "cluster", "storage", "registry", "resource_group"]
        order = graph.get_provision_order(components)

        # Resource group must come first
        assert order[0] == "resource_group"

        # Registry and storage must come before cluster
        cluster_idx = order.index("cluster")
        registry_idx = order.index("registry")
        storage_idx = order.index("storage")
        assert registry_idx < cluster_idx
        assert storage_idx < order.index("workspace")

        # Workspace must come last
        assert order[-1] == "workspace"

    def test_destroy_order(self):
        """Test that destroy order is reverse of provision order."""
        graph = DependencyGraph()

        components = ["workspace", "cluster", "storage", "registry", "resource_group"]
        provision_order = graph.get_provision_order(components)
        destroy_order = graph.get_destroy_order(components)

        assert destroy_order == list(reversed(provision_order))

    def test_canonicalize_names(self):
        """Test that component names are canonicalized."""
        graph = DependencyGraph()

        # Should handle both hyphenated and underscored names
        components = ["resource-group", "cluster", "workspace"]
        order = graph.get_provision_order(components)
        assert "resource_group" in order


class TestValidateComponentDependencies:
    """Test component dependency validation."""

    def test_validate_no_dependencies(self):
        """Test validation passes for components with no dependencies."""
        # Resource group has no dependencies - should pass without checking anything
        validate_component_dependencies("resource_group", "dev")

        # Should complete without error

    @patch('modelops.client.infra_service.InfrastructureService')
    def test_validate_all_dependencies_ready(self, mock_infra_service_class):
        """Test validation passes when all dependencies are ready."""
        # Mock the infrastructure service
        mock_service = Mock()
        mock_infra_service_class.return_value = mock_service

        # Mock all dependencies as ready
        mock_service.get_status.return_value = {
            "resource_group": ComponentStatus(
                deployed=True,
                phase=ComponentState.READY,
                details={}
            ),
            "cluster": ComponentStatus(
                deployed=True,
                phase=ComponentState.READY,
                details={}
            ),
            "storage": ComponentStatus(
                deployed=True,
                phase=ComponentState.READY,
                details={}
            ),
        }

        # Should pass without error
        validate_component_dependencies("workspace", "dev", mock_service)

    @patch('modelops.client.infra_service.InfrastructureService')
    def test_validate_missing_dependencies(self, mock_infra_service_class):
        """Test validation fails when dependencies are missing."""
        # Mock the infrastructure service
        mock_service = Mock()
        mock_infra_service_class.return_value = mock_service

        # Mock cluster as not deployed
        mock_service.get_status.return_value = {
            "resource_group": ComponentStatus(
                deployed=True,
                phase=ComponentState.READY,
                details={}
            ),
            "cluster": ComponentStatus(
                deployed=False,
                phase=ComponentState.NOT_DEPLOYED,
                details={}
            ),
            "storage": ComponentStatus(
                deployed=True,
                phase=ComponentState.READY,
                details={}
            ),
        }

        # Should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            validate_component_dependencies("workspace", "dev", mock_service)

        error_msg = str(exc_info.value)
        assert "Cannot deploy workspace" in error_msg
        assert "cluster" in error_msg
        assert "mops infra up" in error_msg

    @patch('modelops.client.infra_service.InfrastructureService')
    def test_validate_dependencies_not_ready(self, mock_infra_service_class):
        """Test validation fails when dependencies exist but aren't ready."""
        # Mock the infrastructure service
        mock_service = Mock()
        mock_infra_service_class.return_value = mock_service

        # Mock cluster as deployed but not ready
        mock_service.get_status.return_value = {
            "resource_group": ComponentStatus(
                deployed=True,
                phase=ComponentState.READY,
                details={}
            ),
            "cluster": ComponentStatus(
                deployed=True,
                phase=ComponentState.DEPLOYING,  # Not ready
                details={}
            ),
            "storage": ComponentStatus(
                deployed=True,
                phase=ComponentState.READY,
                details={}
            ),
        }

        # Should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            validate_component_dependencies("workspace", "dev", mock_service)

        error_msg = str(exc_info.value)
        assert "Cannot deploy workspace" in error_msg
        assert "cluster (deploying)" in error_msg.lower()

    @patch('modelops.client.infra_service.InfrastructureService')
    def test_validate_cluster_dependencies(self, mock_infra_service_class):
        """Test cluster validation requires resource_group and registry."""
        # Mock the infrastructure service
        mock_service = Mock()
        mock_infra_service_class.return_value = mock_service

        # Mock registry as not deployed
        mock_service.get_status.return_value = {
            "resource_group": ComponentStatus(
                deployed=True,
                phase=ComponentState.READY,
                details={}
            ),
            "registry": ComponentStatus(
                deployed=False,
                phase=ComponentState.NOT_DEPLOYED,
                details={}
            ),
        }

        # Should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            validate_component_dependencies("cluster", "dev", mock_service)

        error_msg = str(exc_info.value)
        assert "Cannot deploy cluster" in error_msg
        assert "registry" in error_msg

    def test_validate_uses_existing_service(self):
        """Test validation can use an existing InfrastructureService."""
        # Create a mock service
        mock_service = Mock()
        mock_service.get_status.return_value = {
            "resource_group": ComponentStatus(
                deployed=True,
                phase=ComponentState.READY,
                details={}
            ),
        }

        # Should use the provided service, not create a new one
        validate_component_dependencies("storage", "dev", mock_service)

        # Service should have been called
        mock_service.get_status.assert_called_once()


class TestCLIIntegration:
    """Test that CLIs properly use dependency validation."""

    @patch('modelops.client.utils.validate_component_dependencies')
    @patch('modelops.cli.workspace.WorkspaceService')
    def test_workspace_cli_validates(self, mock_service, mock_validate):
        """Test workspace CLI validates dependencies."""
        from typer.testing import CliRunner
        from modelops.cli.workspace import app

        # Mock validation to pass
        mock_validate.return_value = None
        mock_service_instance = Mock()
        mock_service.return_value = mock_service_instance
        mock_service_instance.provision.return_value = {
            "scheduler_address": "tcp://localhost:8786"
        }

        runner = CliRunner()

        # Create a valid config file
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.yaml', mode='w', delete=False) as f:
            f.write("""metadata:
  name: test-workspace
spec:
  workers:
    image: test-image
    replicas: 2
  scheduler:
    image: test-image""")
            config_path = f.name

        try:
            result = runner.invoke(app, ["up", "--config", config_path])

            # Should have called validate_component_dependencies
            mock_validate.assert_called_once_with("workspace", "dev")

        finally:
            import os
            os.unlink(config_path)

    @patch('modelops.client.utils.validate_component_dependencies')
    def test_workspace_cli_fails_on_validation_error(self, mock_validate):
        """Test workspace CLI exits when validation fails."""
        from typer.testing import CliRunner
        from modelops.cli.workspace import app

        # Mock validation to fail
        mock_validate.side_effect = ValueError("Missing dependencies: cluster")

        runner = CliRunner()

        # Create a valid config file
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.yaml', mode='w', delete=False) as f:
            f.write("""metadata:
  name: test-workspace
spec:
  workers:
    image: test-image
    replicas: 2
  scheduler:
    image: test-image""")
            config_path = f.name

        try:
            result = runner.invoke(app, ["up", "--config", config_path])

            # Should exit with error
            assert result.exit_code != 0
            assert "Missing dependencies" in result.stdout

        finally:
            import os
            os.unlink(config_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])