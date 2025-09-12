"""Isolated warm execution environment using subprocess pools."""

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Any

from modelops_contracts import SimTask, SimReturn, TableArtifact, ErrorInfo, task_id, sim_root
from modelops_contracts.ports import ExecutionEnvironment, BundleRepository, CAS

from ...worker.process_manager import WarmProcessManager

logger = logging.getLogger(__name__)


class IsolatedWarmExecEnv(ExecutionEnvironment):
    """Execution environment using warm isolated subprocesses.
    
    This environment maintains a pool of warm subprocesses, each
    isolated with its own virtual environment. Processes are reused
    for the same bundle digest to avoid repeated initialization.
    """
    
    def __init__(
        self,
        bundle_repo: BundleRepository,
        cas: CAS,
        venvs_dir: Path,
        mem_limit_bytes: Optional[int] = None,
        max_warm_processes: int = 128,
        inline_artifact_max_bytes: int = 64_000
    ):
        """Initialize the execution environment.
        
        Args:
            bundle_repo: Repository for fetching bundles
            cas: Content-addressable storage for results
            venvs_dir: Directory for virtual environments
            mem_limit_bytes: Optional memory limit per process
            max_warm_processes: Maximum number of warm processes
            inline_artifact_max_bytes: Max size for inline artifacts (vs CAS)
        """
        self.bundle_repo = bundle_repo
        self.cas = cas
        self.venvs_dir = venvs_dir
        self.mem_limit_bytes = mem_limit_bytes
        self.inline_artifact_max_bytes = inline_artifact_max_bytes
        
        # Create process manager
        self._process_manager = WarmProcessManager(
            venvs_dir=venvs_dir,
            max_processes=max_warm_processes
        )
    
    def run(self, task: SimTask) -> SimReturn:
        """Execute task - handling ALL infrastructure concerns.
        
        The core just calls this method. We handle:
        1. Bundle resolution
        2. Process management  
        3. Wire protocol conversion
        4. CAS decisions
        5. Error handling
        
        Args:
            task: Simulation task to execute
            
        Returns:
            SimReturn with status and artifacts
        """
        try:
            # 1. BUNDLE RESOLUTION (infrastructure concern)
            digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)
            
            # 2. EXECUTE in warm process (infrastructure concern)
            # The process manager handles all the complexity
            artifacts = self._process_manager.execute_task(
                bundle_digest=digest,
                bundle_path=bundle_path,
                entrypoint=str(task.entrypoint) if task.entrypoint else "main",
                params=dict(task.params.params),  # UniqueParameterSet → dict
                seed=task.seed
            )
            
            # Check if subprocess returned an error
            if len(artifacts) == 1 and "error" in artifacts:
                # Subprocess execution failed - decode error and re-raise
                error_data = base64.b64decode(artifacts["error"])
                error_info = json.loads(error_data)
                raise RuntimeError(
                    f"Subprocess execution failed: {error_info.get('error', 'Unknown error')} "
                    f"(type: {error_info.get('type', 'Unknown')})"
                )
            
            # 3. CAS DECISIONS (infrastructure concern)
            artifact_refs = {}
            for name, data in artifacts.items():
                # Data comes back as base64-encoded strings from subprocess
                decoded_data = base64.b64decode(data) if isinstance(data, str) else data
                
                if len(decoded_data) > self.inline_artifact_max_bytes:  # Large artifact
                    checksum = hashlib.sha256(decoded_data).hexdigest()
                    ref = self.cas.put(decoded_data, checksum)
                    artifact_refs[name] = f"cas://{ref}"
                else:  # Small artifact - inline
                    artifact_refs[name] = f"inline:{base64.b64encode(decoded_data).decode()}"
            
            # 4. Create proper sim_root and task_id
            root = sim_root(
                bundle_ref=task.bundle_ref,
                params=dict(task.params.params),
                seed=task.seed,
                entrypoint=str(task.entrypoint) if task.entrypoint else "main"
            )
            
            # Determine output names
            output_names = tuple(artifact_refs.keys())
            tid = task_id(
                sim_root=root,
                entrypoint=str(task.entrypoint) if task.entrypoint else "main",
                outputs=output_names
            )
            
            # 5. Convert artifact_refs to TableArtifacts
            outputs = {}
            for name, ref in artifact_refs.items():
                if ref.startswith("cas://"):
                    checksum = ref[6:]  # Remove "cas://" prefix
                    # We don't have size info here, estimate from original data
                    outputs[name] = TableArtifact(
                        ref=ref,
                        checksum=checksum,
                        size=0,  # Size unknown at this point
                        inline=None
                    )
                elif ref.startswith("inline:"):
                    inline_data = base64.b64decode(ref[7:])
                    checksum = hashlib.sha256(inline_data).hexdigest()
                    outputs[name] = TableArtifact(
                        ref=None,
                        checksum=checksum,
                        size=len(inline_data),
                        inline=inline_data
                    )
            
            return SimReturn(
                task_id=tid,
                sim_root=root,
                outputs=outputs
            )
            
        except Exception as e:
            logger.exception(f"Task execution failed for bundle {task.bundle_ref}")
            
            # Create error result with proper structure
            root = sim_root(
                bundle_ref=task.bundle_ref,
                params=dict(task.params.params),
                seed=task.seed,
                entrypoint=str(task.entrypoint) if task.entrypoint else "main"
            )
            tid = task_id(
                sim_root=root,
                entrypoint=str(task.entrypoint) if task.entrypoint else "main",
                outputs=("error",)
            )
            
            # Create error info
            error_info = ErrorInfo(
                error_type=type(e).__name__,
                message=str(e),
                retryable=False  # Could be smarter about this based on error type
            )
            
            # Store full error details as artifact
            error_details_data = json.dumps({
                "error": str(e),
                "type": type(e).__name__,
                "bundle_ref": task.bundle_ref,
                "entrypoint": str(task.entrypoint) if task.entrypoint else "main",
                "traceback": None  # Could capture traceback if needed
            }).encode()
            checksum = hashlib.sha256(error_details_data).hexdigest()
            
            # Store in CAS if large, otherwise inline
            if len(error_details_data) > self.inline_artifact_max_bytes:
                error_ref = self.cas.put(error_details_data, checksum)
                error_details = TableArtifact(
                    ref=f"cas://{error_ref}",
                    checksum=checksum,
                    size=len(error_details_data),
                    inline=None
                )
            else:
                error_details = TableArtifact(
                    ref=None,
                    checksum=checksum,
                    size=len(error_details_data),
                    inline=error_details_data
                )
            
            return SimReturn(
                task_id=tid,
                sim_root=root,
                outputs={},  # Empty outputs for error case
                error=error_info,
                error_details=error_details
            )
    
    def health_check(self) -> Dict[str, Any]:
        """Check health of execution environment."""
        return {
            'type': 'isolated_warm',
            'active_processes': self._process_manager.active_count(),
            'venvs_dir': str(self.venvs_dir)
        }
    
    def shutdown(self):
        """Clean shutdown of all warm processes."""
        logger.info("Shutting down IsolatedWarmExecEnv")
        self._process_manager.shutdown_all()
