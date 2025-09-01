"""State models for workspace management."""

from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any

# WorkspaceOutputs moved inline since workspace.py is for Stage 2
@dataclass
class WorkspaceOutputs:
    """Outputs from a provisioned workspace."""
    name: str
    namespace: str
    scheduler_address: str
    dashboard_hint: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "scheduler_address": self.scheduler_address,
            "dashboard_hint": self.dashboard_hint
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkspaceOutputs":
        return cls(
            name=data.get("name", ""),
            namespace=data.get("namespace", ""),
            scheduler_address=data.get("scheduler_address", ""),
            dashboard_hint=data.get("dashboard_hint", "")
        )


@dataclass
class WorkspaceState:
    """Full workspace state combining outputs and runtime config.
    
    This class wraps the Pulumi outputs (WorkspaceOutputs) and adds
    operational state like provider, status, and resource configuration.
    """
    outputs: WorkspaceOutputs
    provider: str
    status: str = "running"
    image: str = ""
    min_workers: int = 1
    max_workers: int = 10
    worker_memory: str = ""
    worker_cpu: str = ""
    scheduler_memory: str = ""
    scheduler_cpu: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    # Convenience passthroughs to outputs
    @property
    def name(self) -> str:
        return self.outputs.name
    
    @property
    def namespace(self) -> str:
        return self.outputs.namespace
    
    @property
    def scheduler_address(self) -> str:
        return self.outputs.scheduler_address
    
    @property
    def dashboard_hint(self) -> str:
        return self.outputs.dashboard_hint

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON storage."""
        d = asdict(self)
        d["outputs"] = self.outputs.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkspaceState":
        """Create from dictionary, handling backwards compatibility.
        
        Supports both new nested format and old flat format.
        """
        # Handle new format with nested outputs
        outputs_data = data.get("outputs")
        
        # Backwards compatibility: construct outputs from flat dict
        if not outputs_data:
            outputs_data = {
                "name": data.get("name", ""),
                "namespace": data.get("namespace", ""),
                "scheduler_address": data.get("scheduler_address", ""),
                "dashboard_hint": data.get("dashboard_hint", ""),
            }
        
        outputs = WorkspaceOutputs.from_dict(outputs_data)
        
        # Remove fields that are part of outputs to avoid duplicate kwargs
        clean_data = {
            k: v for k, v in data.items() 
            if k not in ("name", "namespace", "scheduler_address", "dashboard_hint", "outputs")
        }
        
        return cls(outputs=outputs, **clean_data)
    
    @property
    def is_running(self) -> bool:
        """Check if workspace is in running state."""
        return self.status == "running"
    
    @property
    def worker_range(self) -> str:
        """Format worker range for display."""
        if self.max_workers > self.min_workers:
            return f"{self.min_workers}-{self.max_workers}"
        return str(self.min_workers)
    
    @property
    def resource_summary(self) -> str:
        """Format resource summary for display."""
        return f"{self.worker_memory}/{self.worker_cpu}cpu"