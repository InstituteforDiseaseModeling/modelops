"""Local state management for ModelOps workspaces and runs."""

import json
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
from .models import WorkspaceState


class StateManager:
    """Manages local state for workspaces and runs.
    
    State is stored as JSON in ~/.modelops/state.json for simplicity.
    This provides a lightweight way to track provisioned resources
    without requiring a database.
    """
    
    def __init__(self, state_dir: Optional[Path] = None):
        """Initialize state manager.
        
        Args:
            state_dir: Optional custom state directory (for testing)
        """
        self.state_dir = state_dir or (Path.home() / ".modelops")
        self.state_file = self.state_dir / "state.json"
        self._ensure_state()
    
    def _ensure_state(self):
        """Ensure state directory and file exist with proper structure."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        
        if not self.state_file.exists():
            initial_state = {
                "version": "1.0",
                "workspaces": {},
                "runs": {},
                "metadata": {
                    "created_at": datetime.now().isoformat(),
                    "last_updated": datetime.now().isoformat()
                }
            }
            self._write(initial_state)
    
    def _read(self) -> dict:
        """Read state from disk.
        
        Returns:
            Current state dictionary
        """
        try:
            with open(self.state_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            # If file is corrupted or deleted, reinitialize
            self._ensure_state()
            with open(self.state_file, 'r') as f:
                return json.load(f)
    
    def _write(self, state: dict):
        """Write state to disk atomically.
        
        Args:
            state: State dictionary to persist
        """
        # Update metadata
        state.setdefault("metadata", {})["last_updated"] = datetime.now().isoformat()
        
        # Write to temp file first for atomicity
        temp_file = self.state_file.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(state, f, indent=2, default=str, sort_keys=True)
        
        # Atomic rename
        temp_file.replace(self.state_file)
    
    # Workspace operations
    
    def save_workspace(self, name: str, workspace: WorkspaceState):
        """Save or update workspace state.
        
        Args:
            name: Workspace name
            workspace: WorkspaceState object
        """
        state = self._read()
        
        # Preserve created_at if updating existing workspace
        existing = state.get("workspaces", {}).get(name, {})
        if not workspace.created_at and existing.get("created_at"):
            workspace.created_at = existing["created_at"]
        elif not workspace.created_at:
            workspace.created_at = datetime.now().isoformat()
        
        workspace.updated_at = datetime.now().isoformat()
        
        state.setdefault("workspaces", {})[name] = workspace.to_dict()
        self._write(state)
    
    def get_workspace(self, name: str) -> Optional[WorkspaceState]:
        """Get workspace state by name.
        
        Args:
            name: Workspace name
            
        Returns:
            WorkspaceState object or None if not found
        """
        state = self._read()
        raw = state.get("workspaces", {}).get(name)
        return WorkspaceState.from_dict(raw) if raw else None
    
    def list_workspaces(self) -> Dict[str, WorkspaceState]:
        """List all workspaces.
        
        Returns:
            Dictionary of workspace name -> WorkspaceState object
        """
        state = self._read()
        return {
            name: WorkspaceState.from_dict(data)
            for name, data in state.get("workspaces", {}).items()
        }
    
    def remove_workspace(self, name: str) -> bool:
        """Remove workspace from state.
        
        Args:
            name: Workspace name
            
        Returns:
            True if workspace was removed, False if not found
        """
        state = self._read()
        workspaces = state.get("workspaces", {})
        if name in workspaces:
            del workspaces[name]
            self._write(state)
            return True
        return False
    
    def workspace_exists(self, name: str) -> bool:
        """Check if workspace exists.
        
        Args:
            name: Workspace name
            
        Returns:
            True if workspace exists
        """
        return self.get_workspace(name) is not None
    
    # Run operations (for adaptive plane)
    
    def save_run(self, run_id: str, data: Dict[str, Any]):
        """Save or update adaptive run state.
        
        Args:
            run_id: Run identifier
            data: Run data (config, status, etc.)
        """
        state = self._read()
        state.setdefault("runs", {})[run_id] = {
            **data,
            "run_id": run_id,
            "created_at": state.get("runs", {}).get(run_id, {}).get("created_at", datetime.now().isoformat()),
            "updated_at": datetime.now().isoformat()
        }
        self._write(state)
    
    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get run state by ID.
        
        Args:
            run_id: Run identifier
            
        Returns:
            Run data or None if not found
        """
        state = self._read()
        return state.get("runs", {}).get(run_id)
    
    def list_runs(self, workspace: Optional[str] = None) -> Dict[str, Any]:
        """List all runs, optionally filtered by workspace.
        
        Args:
            workspace: Optional workspace name to filter by
            
        Returns:
            Dictionary of run_id -> run data
        """
        state = self._read()
        runs = state.get("runs", {})
        
        if workspace:
            # Filter runs by workspace
            return {
                run_id: data 
                for run_id, data in runs.items() 
                if data.get("workspace") == workspace
            }
        
        return runs
    
    def remove_run(self, run_id: str) -> bool:
        """Remove run from state.
        
        Args:
            run_id: Run identifier
            
        Returns:
            True if run was removed, False if not found
        """
        state = self._read()
        runs = state.get("runs", {})
        if run_id in runs:
            del runs[run_id]
            self._write(state)
            return True
        return False
    
    # Binding operations (for cross-plane communication)
    
    def save_binding(self, binding_type: str, data: Dict[str, Any]):
        """Save a binding (ClusterBinding, DaskBinding, etc).
        
        Args:
            binding_type: Type of binding (e.g., 'infra', 'dask', 'postgres')
            data: Binding data dictionary
        """
        state = self._read()
        state.setdefault("bindings", {})[binding_type] = {
            **data,
            "created_at": state.get("bindings", {}).get(binding_type, {}).get("created_at", datetime.now().isoformat()),
            "updated_at": datetime.now().isoformat()
        }
        self._write(state)
    
    def get_binding(self, binding_type: str) -> Optional[Dict[str, Any]]:
        """Get a binding by type.
        
        Args:
            binding_type: Type of binding (e.g., 'infra', 'dask', 'postgres')
            
        Returns:
            Binding data or None if not found
        """
        state = self._read()
        return state.get("bindings", {}).get(binding_type)
    
    def remove_binding(self, binding_type: str) -> bool:
        """Remove a binding from state.
        
        Args:
            binding_type: Type of binding to remove
            
        Returns:
            True if binding was removed, False if not found
        """
        state = self._read()
        bindings = state.get("bindings", {})
        if binding_type in bindings:
            del bindings[binding_type]
            self._write(state)
            return True
        return False
    
    # Utility methods
    
    def clear_all(self):
        """Clear all state (for testing/reset)."""
        self._write({
            "version": "1.0",
            "workspaces": {},
            "runs": {},
            "metadata": {
                "created_at": datetime.now().isoformat(),
                "last_updated": datetime.now().isoformat()
            }
        })
    
    def get_stats(self) -> Dict[str, Any]:
        """Get state statistics.
        
        Returns:
            Dictionary with counts and metadata
        """
        state = self._read()
        return {
            "workspace_count": len(state.get("workspaces", {})),
            "run_count": len(state.get("runs", {})),
            "metadata": state.get("metadata", {})
        }
