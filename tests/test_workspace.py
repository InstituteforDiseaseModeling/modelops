"""Tests for workspace configuration and validation."""

import pytest
import yaml
from pathlib import Path
from modelops.components.specs import (
    WorkspaceSpec,
    WorkspaceSpecV2,
    ResourceRequirements,
    SchedulerSpec,
    WorkersSpec,
    WorkspaceMetadata,
    WorkspaceSpecDetails
)


def test_workspace_spec_validation():
    """Test basic workspace specification validation."""
    spec = {
        "apiVersion": "modelops/v1",
        "kind": "Workspace",
        "metadata": {
            "name": "test-workspace",
            "namespace": "modelops-test"
        },
        "spec": {
            "scheduler": {
                "image": "ghcr.io/dask/dask:latest",
                "resources": {
                    "requests": {"memory": "2Gi", "cpu": "1"},
                    "limits": {"memory": "2Gi", "cpu": "1"}
                }
            },
            "workers": {
                "replicas": 4,
                "image": "ghcr.io/dask/dask:latest",
                "resources": {
                    "requests": {"memory": "4Gi", "cpu": "2"},
                    "limits": {"memory": "4Gi", "cpu": "2"}
                }
            }
        }
    }
    
    # Should parse without errors
    ws = WorkspaceSpec(**spec)
    assert ws.kind == "Workspace"
    assert ws.metadata.name == "test-workspace"


def test_workspace_spec_invalid_kind():
    """Test that invalid kind is rejected."""
    spec = {
        "apiVersion": "modelops/v1",
        "kind": "InvalidKind",
        "metadata": {"name": "test"},
        "spec": {}
    }
    
    with pytest.raises(ValueError, match="Invalid kind"):
        WorkspaceSpec(**spec)


def test_resource_requirements():
    """Test resource requirements validation."""
    # Valid resources
    resources = ResourceRequirements(
        requests={"memory": "2Gi", "cpu": "1"},
        limits={"memory": "2Gi", "cpu": "1"}
    )
    assert resources.requests["memory"] == "2Gi"
    
    # Invalid resource type should fail
    with pytest.raises(ValueError, match="Unknown resource type"):
        ResourceRequirements(
            requests={"invalid_resource": "100"}
        )


def test_scheduler_spec_defaults():
    """Test scheduler spec with defaults."""
    scheduler = SchedulerSpec()
    assert scheduler.image == "ghcr.io/dask/dask:latest"
    assert scheduler.resources.requests["memory"] == "1Gi"
    assert scheduler.node_selector is None
    assert scheduler.env == []


def test_workers_spec_with_node_selector():
    """Test workers spec with node selector."""
    workers = WorkersSpec(
        replicas=5,
        nodeSelector={"modelops.io/role": "cpu"},  # Use alias
        threads=4
    )
    assert workers.replicas == 5
    assert workers.node_selector["modelops.io/role"] == "cpu"
    assert workers.threads == 4


def test_workspace_example_yaml():
    """Test that the example workspace.yaml is valid."""
    example_path = Path(__file__).parent.parent / "examples" / "workspace.yaml"
    if not example_path.exists():
        pytest.skip("Example workspace.yaml not found")
    
    with open(example_path) as f:
        spec_dict = yaml.safe_load(f)
    
    # Should parse without errors
    ws = WorkspaceSpec(**spec_dict)
    assert ws.kind == "Workspace"
    assert ws.metadata.name == "dev-workspace"
    
    # Check that spec contains expected sections
    assert "scheduler" in ws.spec
    assert "workers" in ws.spec


def test_workspace_spec_to_config_dict():
    """Test conversion to configuration dictionary."""
    spec = WorkspaceSpec(
        apiVersion="modelops/v1",
        kind="Workspace",
        metadata={"name": "test", "namespace": "test-ns"},
        spec={
            "scheduler": {"image": "test:latest"},
            "workers": {"replicas": 2}
        }
    )
    
    config = spec.to_config_dict()
    assert config["apiVersion"] == "modelops/v1"
    assert config["metadata"]["namespace"] == "test-ns"


def test_workspace_spec_v2_structured():
    """Test structured workspace specification with full validation."""
    spec = WorkspaceSpecV2(
        metadata=WorkspaceMetadata(
            name="structured-workspace",
            namespace="modelops-prod"
        ),
        spec=WorkspaceSpecDetails(
            scheduler=SchedulerSpec(
                image="custom/dask:v1",
                nodeSelector={"zone": "us-east"}  # Use alias
            ),
            workers=WorkersSpec(
                replicas=10,
                threads=4,
                nodeSelector={"zone": "us-east"}  # Use alias
            ),
            tolerations=[
                {"key": "gpu", "operator": "Equal", "value": "true", "effect": "NoSchedule"}
            ]
        )
    )
    
    assert spec.metadata.name == "structured-workspace"
    assert spec.spec.workers.replicas == 10
    assert spec.spec.scheduler.node_selector["zone"] == "us-east"
    assert len(spec.spec.tolerations) == 1


def test_env_var_parsing():
    """Test environment variable parsing from YAML structure."""
    env_config = [
        {"name": "TEST_VAR", "value": "test_value"},
        {"name": "ANOTHER_VAR", "value": "another_value"}
    ]
    
    scheduler = SchedulerSpec(
        env=[{"name": e["name"], "value": e["value"]} for e in env_config]
    )
    
    assert len(scheduler.env) == 2
    assert scheduler.env[0].name == "TEST_VAR"
    assert scheduler.env[1].value == "another_value"