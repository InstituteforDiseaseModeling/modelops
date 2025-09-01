"""Tests for state manager and workspace state."""

import pytest
import tempfile
import json
from pathlib import Path
from modelops.state.manager import StateManager
from modelops.state.models import WorkspaceState, WorkspaceOutputs


def test_state_manager_roundtrip():
    """Test saving and loading workspace state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create state manager with temp directory
        state_file = Path(tmpdir) / "state.json"
        manager = StateManager(state_file)
        
        # Create workspace state
        outputs = WorkspaceOutputs(
            name="test-workspace",
            namespace="test-ns",
            scheduler_address="tcp://localhost:8786",
            dashboard_hint="http://localhost:8787"
        )
        workspace = WorkspaceState(
            outputs=outputs,
            provider="local",
            status="running",
            image="test:latest",
            min_workers=1,
            max_workers=4
        )
        
        # Save workspace
        manager.save_workspace("test", workspace)
        
        # Load it back
        loaded = manager.get_workspace("test")
        
        # Check all fields preserved
        assert loaded.name == "test-workspace"
        assert loaded.namespace == "test-ns"
        assert loaded.scheduler_address == "tcp://localhost:8786"
        assert loaded.dashboard_hint == "http://localhost:8787"
        assert loaded.provider == "local"
        assert loaded.status == "running"
        assert loaded.min_workers == 1
        assert loaded.max_workers == 4


def test_workspace_state_properties():
    """Test WorkspaceState property accessors."""
    outputs = WorkspaceOutputs(
        name="test",
        namespace="ns",
        scheduler_address="tcp://scheduler:8786",
        dashboard_hint="http://dashboard:8787"
    )
    state = WorkspaceState(
        outputs=outputs,
        provider="azure",
        min_workers=2,
        max_workers=10,
        worker_memory="4Gi",
        worker_cpu="2"
    )
    
    # Test property passthroughs
    assert state.name == "test"
    assert state.namespace == "ns"
    assert state.scheduler_address == "tcp://scheduler:8786"
    assert state.dashboard_hint == "http://dashboard:8787"
    
    # Test computed properties
    assert state.is_running  # default status is "running"
    assert state.worker_range == "2-10"
    assert state.resource_summary == "4Gi/2cpu"
    
    # Single worker case
    state2 = WorkspaceState(
        outputs=outputs,
        provider="local",
        min_workers=1,
        max_workers=1,
        worker_memory="2Gi",
        worker_cpu="1"
    )
    assert state2.worker_range == "1"


def test_workspace_state_backwards_compat():
    """Test backwards compatibility with old flat format."""
    # Old flat format
    old_data = {
        "name": "legacy",
        "namespace": "legacy-ns",
        "scheduler_address": "tcp://old:8786",
        "dashboard_hint": "http://old:8787",
        "provider": "orbstack",
        "status": "running"
    }
    
    # Should load without error
    state = WorkspaceState.from_dict(old_data)
    assert state.name == "legacy"
    assert state.namespace == "legacy-ns"
    assert state.scheduler_address == "tcp://old:8786"
    assert state.provider == "orbstack"
    
    # New nested format
    new_data = {
        "outputs": {
            "name": "modern",
            "namespace": "modern-ns",
            "scheduler_address": "tcp://new:8786",
            "dashboard_hint": "http://new:8787"
        },
        "provider": "azure",
        "status": "running"
    }
    
    # Should also load correctly
    state2 = WorkspaceState.from_dict(new_data)
    assert state2.name == "modern"
    assert state2.namespace == "modern-ns"
    assert state2.scheduler_address == "tcp://new:8786"
    assert state2.provider == "azure"


def test_list_workspaces():
    """Test listing multiple workspaces."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        manager = StateManager(state_file)
        
        # Create multiple workspaces
        for i in range(3):
            outputs = WorkspaceOutputs(
                name=f"workspace-{i}",
                namespace=f"ns-{i}",
                scheduler_address=f"tcp://scheduler-{i}:8786",
                dashboard_hint=f"http://dashboard-{i}:8787"
            )
            workspace = WorkspaceState(
                outputs=outputs,
                provider="local",
                status="running"
            )
            manager.save_workspace(f"ws{i}", workspace)
        
        # List all workspaces
        all_workspaces = manager.list_workspaces()
        
        # Should have all 3
        assert len(all_workspaces) == 3
        assert "ws0" in all_workspaces
        assert "ws1" in all_workspaces
        assert "ws2" in all_workspaces
        
        # Each should be a WorkspaceState
        for ws in all_workspaces.values():
            assert isinstance(ws, WorkspaceState)
            assert ws.is_running


def test_remove_workspace():
    """Test removing a workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "state.json"
        manager = StateManager(state_file)
        
        # Create workspace
        outputs = WorkspaceOutputs(
            name="temp",
            namespace="temp-ns",
            scheduler_address="tcp://temp:8786",
            dashboard_hint="http://temp:8787"
        )
        workspace = WorkspaceState(outputs=outputs, provider="local")
        manager.save_workspace("temp", workspace)
        
        # Verify it exists
        assert manager.get_workspace("temp") is not None
        
        # Remove it
        removed = manager.remove_workspace("temp")
        assert removed is True
        
        # Should be gone
        assert manager.get_workspace("temp") is None
        
        # Removing again should return False
        removed_again = manager.remove_workspace("temp")
        assert removed_again is False