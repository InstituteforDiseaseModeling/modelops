"""Tests for service layer."""

import pytest
from unittest.mock import Mock, patch, MagicMock

from modelops.client.base import ComponentState, ComponentStatus, InfraResult
from modelops.client.utils import (
    stack_exists,
    get_safe_outputs,
    DependencyGraph
)
from modelops.components.specs import UnifiedInfraSpec


class TestDependencyOrdering:
    """Test dependency graph and topological sorting."""

    def test_provision_order_basic(self):
        """Test basic provision ordering with DependencyGraph."""
        graph = DependencyGraph()
        graph.add_dependency("workspace", "cluster")
        graph.add_dependency("workspace", "storage")

        components = ["workspace", "cluster", "storage"]
        order = graph.get_provision_order(components)

        # Cluster should come before workspace
        assert order.index("cluster") < order.index("workspace")
        # Storage should come before workspace
        assert order.index("storage") < order.index("workspace")

    def test_provision_order_with_custom_deps(self):
        """Test provision ordering with custom dependencies."""
        graph = DependencyGraph()
        graph.add_dependency("workspace", "cluster")
        graph.add_dependency("workspace", "registry")
        graph.add_dependency("cluster", "registry")

        components = ["registry", "cluster", "workspace"]
        order = graph.get_provision_order(components)

        # Registry has no deps, should be first
        assert order[0] == "registry"
        # Cluster depends on registry, should be second
        assert order[1] == "cluster"
        # Workspace depends on both, should be last
        assert order[2] == "workspace"

    def test_destroy_order(self):
        """Test destroy order is reverse of provision order."""
        graph = DependencyGraph()
        graph.add_dependency("workspace", "cluster")
        graph.add_dependency("workspace", "storage")

        components = ["workspace", "cluster", "storage"]
        provision_order = graph.get_provision_order(components)
        destroy_order = graph.get_destroy_order(components)

        # Destroy order should be reverse of provision
        assert destroy_order == list(reversed(provision_order))

    def test_circular_dependency_detection(self):
        """Test that circular dependencies are detected."""
        graph = DependencyGraph()
        graph.add_dependency("a", "b")
        graph.add_dependency("b", "c")
        graph.add_dependency("c", "a")  # Creates cycle

        components = ["a", "b", "c"]
        with pytest.raises(RuntimeError, match="Circular dependencies"):
            graph.get_provision_order(components)

    def test_dependency_graph(self):
        """Test DependencyGraph class."""
        graph = DependencyGraph()

        # Test adding dependencies
        graph.add_dependency("workspace", "cluster")
        assert "cluster" in graph.get_dependencies("workspace")

        # Test getting dependents
        dependents = graph.get_dependents("cluster")
        assert "workspace" in dependents

        # Test removing dependencies
        graph.remove_dependency("workspace", "cluster")
        assert "cluster" not in graph.get_dependencies("workspace")


class TestComponentStatus:
    """Test unified status contract."""

    def test_component_status_creation(self):
        """Test ComponentStatus creation and serialization."""
        status = ComponentStatus(
            deployed=True,
            phase=ComponentState.READY,
            details={"cluster_name": "test-cluster"}
        )

        assert status.deployed is True
        assert status.phase == ComponentState.READY
        assert status.details["cluster_name"] == "test-cluster"

        # Test JSON serialization
        json_data = status.to_json()
        assert json_data["deployed"] is True
        assert json_data["phase"] == "Ready"
        assert json_data["details"]["cluster_name"] == "test-cluster"

    def test_component_states(self):
        """Test ComponentState enum values."""
        assert ComponentState.NOT_DEPLOYED.value == "NotDeployed"
        assert ComponentState.DEPLOYING.value == "Deploying"
        assert ComponentState.READY.value == "Ready"
        assert ComponentState.FAILED.value == "Failed"
        assert ComponentState.UNKNOWN.value == "Unknown"


class TestInfraResult:
    """Test InfraResult data class."""

    def test_infra_result_creation(self):
        """Test InfraResult creation."""
        result = InfraResult(
            success=True,
            components={"cluster": ComponentState.READY},
            outputs={"cluster": {"name": "test"}},
            errors={},
            logs_path="/path/to/logs"
        )

        assert result.success is True
        assert result.components["cluster"] == ComponentState.READY
        assert result.outputs["cluster"]["name"] == "test"
        assert result.logs_path == "/path/to/logs"

    def test_infra_result_json(self):
        """Test InfraResult JSON serialization."""
        result = InfraResult(
            success=False,
            components={"cluster": ComponentState.FAILED},
            outputs={},
            errors={"cluster": "Provisioning failed"}
        )

        json_str = result.to_json()
        assert '"success": false' in json_str
        assert '"cluster": "Failed"' in json_str
        assert '"cluster": "Provisioning failed"' in json_str


class TestOutputSafety:
    """Test secret masking in outputs."""

    def test_get_safe_outputs_masks_secrets(self):
        """Test that secrets are masked by default."""
        outputs = {
            "cluster_name": "test-cluster",
            "connection_string": "secret-connection-string",
            "password": "super-secret",
            "token": "auth-token-12345",
            "kubeconfig": "sensitive-kubeconfig-data"
        }

        safe = get_safe_outputs(outputs, show_secrets=False)

        assert safe["cluster_name"] == "test-cluster"  # Not a secret
        assert safe["connection_string"] == "****"  # Masked
        assert safe["password"] == "****"  # Masked
        assert safe["token"] == "****"  # Masked
        assert safe["kubeconfig"] == "****"  # Masked

    def test_get_safe_outputs_shows_when_requested(self):
        """Test that secrets are shown when explicitly requested."""
        outputs = {
            "connection_string": "secret-connection-string",
            "password": "super-secret"
        }

        safe = get_safe_outputs(outputs, show_secrets=True)

        assert safe["connection_string"] == "secret-connection-string"
        assert safe["password"] == "super-secret"


class TestUnifiedInfraSpec:
    """Test unified infrastructure specification."""

    def test_spec_creation(self):
        """Test creating UnifiedInfraSpec."""
        # Test with None values (all optional)
        spec = UnifiedInfraSpec(
            schema_version=1,
            cluster=None,
            storage=None,
            workspace=None,
            registry=None
        )

        assert spec.schema_version == 1
        assert spec.cluster is None
        assert spec.storage is None
        assert spec.workspace is None

    def test_get_components(self):
        """Test getting list of configured components."""
        # Create with just registry (doesn't require complex config)
        spec = UnifiedInfraSpec(
            schema_version=1,
            cluster=None,
            storage=None,
            workspace=None,
            registry={"provider": "azure", "name": "test"}
        )

        components = spec.get_components()
        assert "cluster" not in components
        assert "storage" not in components
        assert "workspace" not in components
        assert "registry" in components

    def test_validate_dependencies(self):
        """Test dependency validation."""
        # Test with properly formed workspace config
        from modelops.components.specs import WorkspaceConfig

        workspace_config = WorkspaceConfig(
            apiVersion="modelops/v1",
            kind="Workspace",
            metadata={"name": "test"},
            spec={
                "scheduler": {"image": "dask:latest"},
                "workers": {"image": "dask:latest", "replicas": 2}
            }
        )

        # Workspace without cluster should fail
        spec = UnifiedInfraSpec(
            schema_version=1,
            workspace=workspace_config,
            cluster=None
        )

        with pytest.raises(ValueError, match="Workspace requires cluster"):
            spec.validate_dependencies()

        # With cluster should pass - use properly formed config
        from modelops.components.specs.azure import AzureProviderConfig, AKSConfig, NodePool

        cluster_config = AzureProviderConfig(
            provider="azure",
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="test-rg",
            location="eastus2",
            username="test",
            aks=AKSConfig(
                name="test-aks",
                kubernetes_version="1.27",
                node_pools=[
                    NodePool(
                        name="default",
                        vm_size="Standard_B2s",
                        mode="System",
                        min=1,
                        max=3
                    )
                ]
            )
        )
        spec.cluster = cluster_config
        assert spec.validate_dependencies() is True


class TestServiceRetry:
    """Test retry mechanism for transient failures."""

    def test_retry_on_transient_error(self):
        """Test that transient errors are retried."""
        from modelops.client.base import BaseService

        class TestService(BaseService):
            def provision(self, config, verbose=False):
                return {}

            def destroy(self, verbose=False):
                pass

            def status(self):
                return ComponentStatus(False, ComponentState.NOT_DEPLOYED, {})

        service = TestService("test")

        # Mock function that fails twice then succeeds
        call_count = 0

        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("429 Too Many Requests")
            return "success"

        # Should retry and eventually succeed
        result = service.with_retry(flaky_func, max_retries=3, base_delay=0.01)
        assert result == "success"
        assert call_count == 3

    def test_retry_non_transient_error(self):
        """Test that non-transient errors are not retried."""
        from modelops.client.base import BaseService

        class TestService(BaseService):
            def provision(self, config, verbose=False):
                return {}

            def destroy(self, verbose=False):
                pass

            def status(self):
                return ComponentStatus(False, ComponentState.NOT_DEPLOYED, {})

        service = TestService("test")

        call_count = 0

        def failing_func():
            nonlocal call_count
            call_count += 1
            raise ValueError("Not a transient error")

        # Should not retry non-transient errors
        with pytest.raises(ValueError, match="Not a transient error"):
            service.with_retry(failing_func, max_retries=3, base_delay=0.01)

        assert call_count == 1  # Only called once