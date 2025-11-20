# ModelOps Simulation Execution Architecture v3.1 — WorkerPlugin Solution

**Status:** Implementation blueprint  
**Audience:** ModelOps & Calabaria engineers  
**Architecture:** Hexagonal (Ports & Adapters) with Dask WorkerPlugin  
**Primary goals:** Clean architecture, proper lifecycle, low latency, testability

---

## Executive Summary

This document describes a **Hexagonal Architecture** for ModelOps simulation
execution using **Dask's WorkerPlugin** as the composition root:

- **Core Domain (Hexagon):** Simulation execution logic, independent of infrastructure
- **Ports:** Explicit interfaces defined in `modelops-contracts`
- **Adapters:** Concrete implementations (Dask, CAS, Bundle repos, etc.)
- **WorkerPlugin:** Native Dask lifecycle management and dependency injection
- **Wire Interface:** Generic contract for executing simulations

Key improvements from v3:
1. **WorkerPlugin eliminates all singleton hacks** (no LRU bootstrap, no globals)
2. **Proper lifecycle with setup/teardown** (clean resource management)
3. **Native Dask integration** (no fighting the framework)
4. **Simplified configuration** (no hashability issues)
5. **Components to remove** (old runners, env code, singletons)

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [WorkerPlugin Composition Root](#2-workerplugin-composition-root)
3. [Ports Definition](#3-ports-definition)
4. [Core Domain](#4-core-domain)
5. [Secondary Adapters](#5-secondary-adapters)
6. [Primary Adapters](#6-primary-adapters)
7. [Wire Module Integration](#7-wire-module-integration)
8. [Components to Remove](#8-components-to-remove)
9. [Engineering Tradeoffs](#9-engineering-tradeoffs)
10. [Implementation Plan](#10-implementation-plan)
11. [Migration Strategy](#11-migration-strategy)
12. [Unresolved Issues](#12-unresolved-issues)

---

## 1. Architecture Overview

### Hexagonal Architecture with WorkerPlugin

```
┌───────────────────────────────────────────────────────────────┐
│                       DASK SCHEDULER                          │
│   client.register_worker_plugin(ModelOpsWorkerPlugin(),       │
│                               name="modelops-runtime-v1")     │
└───────────────┬───────────────────────────────────────────────┘
                │ distributes plugin
                ▼
┌───────────────────────────────────────────────────────────────┐
│                           DASK WORKER                         │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │          ModelOpsWorkerPlugin (composition root)        │  │
│  │ setup(worker):                                          │  │
│  │   cfg = RuntimeConfig.from_env()                        │  │
│  │   cas = make_cas(cfg)                                   │  │
│  │   bundles = make_bundle_repo(cfg)                       │  │
│  │   exec_env = IsolatedWarmExecEnv(bundles, cas, ...)     │  │
│  │   executor = SimulationExecutor(exec_env)               │  │
│  │   worker.modelops_runtime = executor                    │  │
│  │                                                         │  │
│  │ teardown(worker):                                       │  │
│  │   worker.modelops_runtime.shutdown()                    │  │
│  └───────────────────────┬─────────────────────────────────┘  │
│                          │                                    │
│  ┌───────────────────────▼─────────────────────────────────┐  │
│  │                Task execution function                  │  │
│  │  _worker_run_task(task):                                │  │
│  │     return worker.modelops_runtime.execute(task)        │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
                │
                ▼
       ┌───────────────────────────────────────────┐
       │         CORE DOMAIN (Hexagon)             │
       │  SimulationExecutor (application seam)    │
       │   - (now) delegates to exec_env           │
       │   - (future) validation / caching /       │
       │            routing / metrics              │
       └────────────────┬──────────────────────────┘
                        │ ExecutionEnvironment port
                        ▼
       ┌───────────────────────────────────────────┐
       │  IsolatedWarmExecEnv (secondary adapter)  │
       │   - bundle_repo.ensure_local()            │
       │   - warm process manager (LRU)            │
       │   - JSON-RPC over stdio                   │
       │   - CAS decisions (inline vs CAS)         │
       └────────────────┬──────────────────────────┘
                        │ JSON-RPC
                        ▼
       ┌───────────────────────────────────────────┐
       │  Warm subprocess (per bundle digest)      │
       │   - discover modelops.wire entry point    │
       │   - run framework wire, return artifacts  │
       └───────────────────────────────────────────┘
```

### Key Architectural Improvements

1. **Native Dask Lifecycle:** WorkerPlugin provides setup/teardown hooks
2. **Single Composition Point:** All wiring happens in plugin.setup()
3. **No Global State:** Runtime attached to worker instance
4. **Clean Shutdown:** Proper resource cleanup in teardown()
5. **Framework Integration:** Works with Dask's design, not against it

---

## 2. WorkerPlugin Composition Root

The WorkerPlugin is the **single composition root** for the entire system.

### 2.1 Plugin Implementation

```python
# modelops/plugins/worker.py
from typing import Optional
from dask.distributed import WorkerPlugin
from pathlib import Path
import os

from modelops_contracts.ports import (
    ExecutionEnvironment, BundleRepository, CAS
)
from modelops.core.executor import SimulationExecutor
from modelops.adapters.exec_env.isolated_warm import IsolatedWarmExecEnv
from modelops.adapters.bundle.modelops_bundle_repo import ModelOpsBundleRepository
from modelops.adapters.cas import S3CAS, AzureCAS, MemoryCAS

class ModelOpsWorkerPlugin(WorkerPlugin):
    """Dask WorkerPlugin for ModelOps simulation execution.
    
    This is THE composition root - all dependency injection happens here.
    No singletons, no globals, no LRU tricks needed.
    """
    
    def __init__(self, config: Optional['RuntimeConfig'] = None):
        """Initialize plugin with optional config override."""
        self.config = config
    
    def setup(self, worker):
        """Setup hook called when plugin is installed on worker.
        
        This is where ALL wiring happens. Called ONCE per worker.
        """
        # Get configuration (from env or override)
        config = self.config or RuntimeConfig.from_env()
        
        # Create CAS adapter
        cas = self._make_cas(config)
        
        # Create bundle repository
        bundle_repo = self._make_bundle_repository(config)
        
        # Create execution environment with its dependencies
        exec_env = self._make_execution_environment(config, bundle_repo, cas)
        
        # Create the core domain executor
        executor = SimulationExecutor(
            exec_env=exec_env,
            bundle_repo=bundle_repo,
            cas=cas
        )
        
        # Attach to worker for task access
        # This is the ONLY place we store runtime state
        worker.modelops_runtime = executor
        
        # Also store for clean shutdown
        worker.modelops_exec_env = exec_env
        
        print(f"ModelOps runtime initialized on worker {worker.id}")
    
    def teardown(self, worker):
        """Teardown hook called when worker is shutting down.
        
        Clean shutdown of all resources.
        """
        print(f"Shutting down ModelOps runtime on worker {worker.id}")
        
        # Clean shutdown via the runtime (which delegates to exec_env)
        if hasattr(worker, 'modelops_runtime'):
            try:
                worker.modelops_runtime.shutdown()
            finally:
                delattr(worker, 'modelops_runtime')
    
    def _make_cas(self, config: 'RuntimeConfig') -> CAS:
        """Instantiate the appropriate CAS adapter."""
        if config.cas_backend == 's3':
            return S3CAS(
                bucket=config.cas_bucket,
                prefix=config.cas_prefix,
                region=config.cas_region
            )
        elif config.cas_backend == 'azure':
            return AzureCAS(
                container=config.cas_bucket,
                prefix=config.cas_prefix,
                storage_account=config.azure_storage_account
            )
        elif config.cas_backend == 'memory':
            return MemoryCAS()
        else:
            raise ValueError(f"Unknown CAS backend: {config.cas_backend}")
    
    def _make_bundle_repository(self, config: 'RuntimeConfig') -> BundleRepository:
        """Instantiate the appropriate bundle repository adapter.
        
        NO implicit defaults - bundle references must be explicit for reproducibility.
        """
        if config.bundle_source == 'oci':
            # Use modelops-bundle for OCI registry pulls
            from modelops.adapters.bundle.modelops_bundle_repo import ModelOpsBundleRepository
            
            # STRICT: registry must be specified
            if not config.bundle_registry:
                raise ValueError("bundle_registry must be specified for OCI source")
            
            return ModelOpsBundleRepository(
                registry_ref=config.bundle_registry,  # e.g., "ghcr.io/org/models"
                cache_dir=Path(config.bundles_cache_dir)
                # NO default_tag - must be explicit in bundle_ref
            )
        elif config.bundle_source == 'file':
            # Simple filesystem for local development
            from modelops.adapters.bundle.file_repo import FileBundleRepository
            
            if not config.bundles_dir:
                raise ValueError("bundles_dir must be specified for file source")
                
            return FileBundleRepository(Path(config.bundles_dir))
        else:
            raise ValueError(f"Unknown bundle source: {config.bundle_source}")
    
    def _make_execution_environment(
        self,
        config: 'RuntimeConfig',
        bundle_repo: BundleRepository,
        cas: CAS
    ) -> ExecutionEnvironment:
        """Instantiate the appropriate execution environment."""
        if config.executor_type == 'isolated_warm':
            return IsolatedWarmExecEnv(
                bundle_repo=bundle_repo,
                cas=cas,
                venvs_dir=Path(config.venvs_dir),
                mem_limit_bytes=config.mem_limit_bytes,
                max_warm_processes=config.max_warm_processes
            )
        elif config.executor_type == 'inline':
            from modelops.adapters.exec_env.inline import InlineExecEnv
            return InlineExecEnv()
        else:
            raise ValueError(f"Unknown executor type: {config.executor_type}")
```

### 2.2 Runtime Configuration

```python
# modelops/config/runtime.py
from dataclasses import dataclass
from typing import Optional
import os

@dataclass(frozen=True)
class RuntimeConfig:
    """Immutable runtime configuration.
    
    All configuration in one place, loaded from environment.
    No need for hashability since we use it once in setup().
    """
    # Paths
    bundles_dir: str = '/var/cache/modelops/bundles'
    venvs_dir: str = '/var/cache/modelops/venvs'
    bundles_cache_dir: str = '/var/cache/modelops/bundles-cache'
    
    # Bundle source
    bundle_source: str = 'oci'  # 'oci' or 'file'
    bundle_registry: Optional[str] = None  # e.g., 'ghcr.io/org/models'
    
    # CAS configuration
    cas_backend: str = 's3'  # 's3', 'azure', 'memory'
    cas_bucket: str = 'simulation-outputs'
    cas_prefix: str = 'v1/'
    cas_region: str = 'us-east-1'
    azure_storage_account: Optional[str] = None
    
    # Executor configuration
    executor_type: str = 'isolated_warm'  # 'isolated_warm', 'inline'
    mem_limit_bytes: Optional[int] = 2_147_483_648  # 2GB
    max_warm_processes: int = 128
    
    @classmethod
    def from_env(cls) -> 'RuntimeConfig':
        """Create configuration from environment variables."""
        return cls(
            bundles_dir=os.getenv('MODELOPS_BUNDLES_DIR', cls.bundles_dir),
            venvs_dir=os.getenv('MODELOPS_VENVS_DIR', cls.venvs_dir),
            bundles_cache_dir=os.getenv('MODELOPS_BUNDLES_CACHE_DIR', cls.bundles_cache_dir),
            bundle_source=os.getenv('MODELOPS_BUNDLE_SOURCE', cls.bundle_source),
            bundle_registry=os.getenv('MODELOPS_BUNDLE_REGISTRY'),
            cas_backend=os.getenv('MODELOPS_CAS_BACKEND', cls.cas_backend),
            cas_bucket=os.getenv('MODELOPS_CAS_BUCKET', cls.cas_bucket),
            cas_prefix=os.getenv('MODELOPS_CAS_PREFIX', cls.cas_prefix),
            cas_region=os.getenv('MODELOPS_CAS_REGION', cls.cas_region),
            azure_storage_account=os.getenv('MODELOPS_AZURE_STORAGE_ACCOUNT'),
            executor_type=os.getenv('MODELOPS_EXECUTOR_TYPE', cls.executor_type),
            mem_limit_bytes=int(os.getenv('MODELOPS_MEM_LIMIT_BYTES', str(cls.mem_limit_bytes))) if os.getenv('MODELOPS_MEM_LIMIT_BYTES') else cls.mem_limit_bytes,
            max_warm_processes=int(os.getenv('MODELOPS_MAX_WARM_PROCESSES', str(cls.max_warm_processes)))
        )
```

### 2.3 Plugin Registration

```python
# modelops/services/simulation.py
from dask.distributed import Client
from modelops.plugins.worker import ModelOpsWorkerPlugin

def create_dask_simulation_service(scheduler_address: str) -> DaskSimulationService:
    """Create Dask simulation service with WorkerPlugin.
    
    This registers the plugin with all workers, ensuring they're
    properly initialized with the ModelOps runtime.
    """
    # Connect to Dask scheduler
    client = Client(scheduler_address)
    
    # Register plugin - this gets distributed to ALL workers
    plugin = ModelOpsWorkerPlugin()
    client.register_worker_plugin(plugin)
    
    # Create service wrapper
    return DaskSimulationService(client)

class DaskSimulationService:
    """Dask distributed simulation service.
    
    Primary adapter that submits tasks to workers with
    initialized ModelOps runtime.
    """
    
    def __init__(self, client: Client):
        self.client = client
    
    def submit(self, task: SimTask) -> Future[SimReturn]:
        """Submit task to Dask cluster."""
        dask_future = self.client.submit(
            _worker_run_task,
            task,
            pure=False
        )
        return DaskFutureAdapter(dask_future)
    
    def gather(self, futures: List[Future[SimReturn]]) -> List[SimReturn]:
        """Gather results from futures."""
        dask_futures = [f.wrapped for f in futures]
        return self.client.gather(dask_futures)

# Worker task function - now MUCH simpler!
def _worker_run_task(task: SimTask) -> SimReturn:
    """Execute task using worker's initialized runtime.
    
    No singleton tricks, no LRU cache, no bootstrap needed!
    The WorkerPlugin has already set everything up.
    """
    from dask.distributed import get_worker
    
    # Get current worker
    worker = get_worker()
    
    # Use the runtime that WorkerPlugin attached
    if not hasattr(worker, 'modelops_runtime'):
        raise RuntimeError(
            "ModelOps runtime not initialized. "
            "Ensure ModelOpsWorkerPlugin is registered."
        )
    
    # Execute using the wired executor
    return worker.modelops_runtime.execute(task)
```

---

## 3. Ports Definition

All ports remain in `modelops-contracts` for true dependency inversion.

```python
# modelops-contracts/ports.py
from typing import Protocol, Generic, TypeVar, Optional, List, Dict, Any, Tuple, Mapping
from pathlib import Path
from .tasks import SimTask, EntryPointId
from .returns import SimReturn
from .types import Scalar

T = TypeVar('T')

class Future(Protocol, Generic[T]):
    """Type-safe future abstraction."""
    def result(self, timeout: Optional[float] = None) -> T: ...
    def done(self) -> bool: ...
    def cancel(self) -> bool: ...
    def exception(self) -> Optional[Exception]: ...

class SimulationService(Protocol):
    """Primary port - how clients drive the simulation system."""
    def submit(self, task: SimTask) -> Future[SimReturn]: ...
    def gather(self, futures: List[Future[SimReturn]]) -> List[SimReturn]: ...
    def submit_batch(self, tasks: List[SimTask]) -> List[Future[SimReturn]]: ...

class ExecutionEnvironment(Protocol):
    """Port for executing simulations in isolated environments."""
    def run(self, task: SimTask) -> SimReturn: ...
    def health_check(self) -> Dict[str, Any]: ...
    def shutdown(self) -> None:
        """Clean shutdown of resources (warm processes, etc)."""
        ...

class BundleRepository(Protocol):
    """Port for fetching and staging simulation bundles."""
    def ensure_local(self, bundle_ref: str) -> Tuple[str, Path]:
        """Fetch bundle and return (canonical_digest, local_path)."""
        ...
    def exists(self, bundle_ref: str) -> bool: ...

class CAS(Protocol):
    """Content-addressable storage for large artifacts."""
    def put(self, data: bytes, checksum_hex: str) -> str: ...
    def get(self, ref: str) -> bytes: ...
    def exists(self, ref: str) -> bool: ...

class WireFunction(Protocol):
    """Contract for simulation execution inside isolated environment."""
    def __call__(
        self,
        entrypoint: EntryPointId,
        params: Mapping[str, Scalar],
        seed: int
    ) -> Dict[str, bytes]: ...
```

---

## 4. Core Domain - The Application Service Layer

### 4.1 Why Keep SimulationExecutor?

Even though minimal now, SimulationExecutor is the **application service seam** - the boundary where domain logic lives, separate from infrastructure. This is standard hexagonal architecture.

**Current Responsibilities (minimal):**
- Delegation to ExecutionEnvironment
- Clean shutdown handling

**Future Responsibilities (will grow):**

1. **Validation & Normalization**
   ```python
   def execute(self, task: SimTask) -> SimReturn:
       # Domain rule: enforce parameter bounds
       if task.seed > 2**32:
           raise ValueError(f"Seed {task.seed} exceeds range")
       # Domain rule: required params per bundle type
       if "safety-critical" in task.bundle_ref:
           self._validate_safety_params(task.params)
       return self.exec_env.run(task)
   ```

2. **Fingerprinting & Caching**
   ```python
   def execute(self, task: SimTask) -> SimReturn:
       # Skip duplicate work
       fingerprint = make_fingerprint(task)
       if cached := self.cache.get(fingerprint):
           return cached
       result = self.exec_env.run(task)
       self.cache.put(fingerprint, result)
       return result
   ```

3. **Policy & Routing**
   ```python
   def execute(self, task: SimTask) -> SimReturn:
       # Business rule: route based on size/priority
       if self._is_lightweight(task):
           return self.inline_env.run(task)
       else:
           return self.isolated_env.run(task)
   ```

4. **Observability**
   ```python
   def execute(self, task: SimTask) -> SimReturn:
       with telemetry.span("simulation.execute") as span:
           span.set_tag("bundle_ref", task.bundle_ref)
           span.set_tag("entrypoint", task.entrypoint)
           result = self.exec_env.run(task)
           span.set_tag("status", result.status)
           return result
   ```

**Engineering Rationale:**
- **Separation of Concerns**: Domain logic vs infrastructure
- **Single Responsibility**: Each layer has one job
- **Future-Proof**: Right place for cross-cutting concerns
- **Cost of Change**: Removing and re-adding later touches many files

### 4.2 The Minimal Implementation

```python
# modelops/core/executor.py
from modelops_contracts import SimTask, SimReturn
from modelops_contracts.ports import ExecutionEnvironment

class SimulationExecutor:
    """Application service layer for simulation execution.
    
    This is the seam between:
    - Inbound: Primary adapters (DaskSimulationService)
    - Outbound: Secondary adapters (ExecutionEnvironment)
    
    Even though thin now, this is where domain logic belongs:
    - Validation & normalization (coming soon)
    - Fingerprinting & caching (future)
    - Policy & routing decisions (future)
    - Observability & metrics (future)
    
    Infrastructure concerns stay in ExecutionEnvironment.
    """
    
    def __init__(self, exec_env: ExecutionEnvironment):
        """Initialize with single dependency.
        
        Note: We used to have bundle_repo and cas here too,
        but those are infrastructure concerns that belong
        in ExecutionEnvironment.
        """
        self.exec_env = exec_env
    
    def execute(self, task: SimTask) -> SimReturn:
        """Execute simulation task.
        
        Currently just delegates, but this is where we'll add:
        - Parameter validation
        - Result caching
        - Routing logic
        - Metrics collection
        """
        # Future: self._validate(task)
        # Future: check cache
        return self.exec_env.run(task)
    
    def shutdown(self):
        """Clean shutdown of resources."""
        if hasattr(self.exec_env, 'shutdown'):
            self.exec_env.shutdown()
```

### 4.3 SimTask Data Flow

**Question**: Does SimTask carry redundant information through the layers?

**Answer**: Yes, but this is intentional and correct.

SimTask flows unchanged through all layers:
```
DaskSimulationService → SimulationExecutor → ExecutionEnvironment
```

After bundle fetching, `bundle_ref` becomes somewhat redundant (we have digest and path), but:

1. **SimTask is the domain request** - It represents what the user asked for
2. **Ports accept domain types** - ExecutionEnvironment port accepts SimTask
3. **Transformation adds complexity** - Creating DTOs at each boundary
4. **Traceability** - Keeping original request helps debugging/logging
5. **Harmless redundancy** - Just a string reference

**Data carried at each level:**
- **SimTask** (domain object): bundle_ref, entrypoint, params, seed
- **After bundle fetch**: digest, path (infrastructure details)
- **WireRequest** (infrastructure DTO): entrypoint, params, seed

The transformation to WireRequest happens at the infrastructure boundary (in ExecutionEnvironment), not in the domain

### 4.4 Updated WorkerPlugin Wiring

```python
class ModelOpsWorkerPlugin(WorkerPlugin):
    def setup(self, worker):
        config = RuntimeConfig.from_env()
        
        # Create infrastructure adapters
        cas = self._make_cas(config)
        bundle_repo = self._make_bundle_repository(config)
        
        # ExecutionEnvironment owns its sub-adapters
        exec_env = self._make_execution_environment(
            config, bundle_repo, cas  # ExecEnv gets these
        )
        
        # Core only knows ExecutionEnvironment
        executor = SimulationExecutor(exec_env)  # ONE dependency!
        
        worker.modelops_runtime = executor
```

---

## 5. Secondary Adapters

### 5.1 Isolated Warm Execution Environment (Owns All Infrastructure)

```python
# modelops/adapters/exec_env/isolated_warm.py
import base64
import hashlib
from pathlib import Path
from typing import Dict, Optional, Any
from dataclasses import dataclass

from modelops_contracts import SimTask, SimReturn, TrialStatus
from modelops_contracts.ports import ExecutionEnvironment, BundleRepository, CAS
from .warm_process import WarmProcessManager

@dataclass
class WireRequest:
    """Internal wire protocol - NOT in contracts.
    This is an infrastructure detail, not domain knowledge."""
    entrypoint: str
    params: Dict[str, Any]
    seed: int

class IsolatedWarmExecEnv(ExecutionEnvironment):
    """Secondary adapter - owns ALL infrastructure complexity.
    
    This adapter owns:
    - Bundle fetching (BundleRepository)
    - Process management (WarmProcessManager)
    - Wire protocol conversion (WireRequest)
    - CAS storage decisions
    
    The core domain knows NOTHING about these details!
    """
    
    def __init__(
        self,
        bundle_repo: BundleRepository,
        cas: CAS,
        venvs_dir: Path,
        mem_limit_bytes: Optional[int] = None,
        max_warm_processes: int = 128
    ):
        # This adapter owns its sub-adapters
        self.bundle_repo = bundle_repo
        self.cas = cas
        self.venvs_dir = venvs_dir
        self.mem_limit_bytes = mem_limit_bytes
        
        # Create process manager
        self._process_manager = WarmProcessManager(
            venvs_dir=venvs_dir,
            mem_limit_bytes=mem_limit_bytes,
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
        """
        try:
            # 1. BUNDLE RESOLUTION (infrastructure concern)
            digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)
            
            # 2. PROCESS MANAGEMENT (infrastructure concern)
            process = self._process_manager.get_process(digest, bundle_path)
            
            # 3. DOMAIN → WIRE CONVERSION (at the boundary)
            wire_request = WireRequest(
                entrypoint=str(task.entrypoint) if task.entrypoint else "main",
                params=dict(task.params.params),  # UniqueParameterSet → dict
                seed=task.seed
            )
            
            # 4. EXECUTE over wire (infrastructure)
            wire_response = process.execute(
                entrypoint=wire_request.entrypoint,
                params=wire_request.params,
                seed=wire_request.seed,
                req_id=f"{digest}:{task.seed}"
            )
            
            # 5. CAS DECISIONS (infrastructure concern)
            artifact_refs = {}
            for name, b64_data in wire_response['artifacts'].items():
                # Decode base64 from wire protocol
                data = base64.b64decode(b64_data)
                if len(data) > 64_000:  # Large artifact
                    checksum = hashlib.sha256(data).hexdigest()
                    ref = self.cas.put(data, checksum)
                    artifact_refs[name] = f"cas://{ref}"
                else:  # Small artifact - inline
                    artifact_refs[name] = f"inline:{base64.b64encode(data).decode()}"
            
            # 6. WIRE → DOMAIN CONVERSION (at the boundary)
            return SimReturn(
                status=TrialStatus.COMPLETED,
                artifact_refs=artifact_refs,
                error_message=None
            )
            
        except Exception as e:
            # Error handling - convert to domain type
            return SimReturn(
                status=TrialStatus.FAILED,
                error_message=str(e),
                artifact_refs={}
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
        self._process_manager.shutdown_all()
```

### 5.2 Warm Process Manager (Fixed)

```python
# modelops/adapters/exec_env/warm_process.py
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional
import logging
from .process import WarmProcess

class WarmProcessManager:
    """Manages warm processes with LRU eviction.
    
    Uses OrderedDict for proper iteration during teardown.
    No more functools.lru_cache issues!
    """
    
    def __init__(
        self,
        venvs_dir: Path,
        mem_limit_bytes: Optional[int] = None,
        max_processes: int = 128
    ):
        self.venvs_dir = venvs_dir
        self.mem_limit_bytes = mem_limit_bytes
        self.max_processes = max_processes
        
        # Track processes with explicit dict + LRU ordering
        self._by_digest: Dict[str, WarmProcess] = {}
        self._lru = OrderedDict()  # digest -> None (just for ordering)
    
    def get_process(self, digest: str, bundle_dir: Path) -> WarmProcess:
        """Get or create warm process for digest."""
        proc = self._by_digest.get(digest)
        
        if proc is None or not proc.is_alive():
            # Need to create new process
            if len(self._by_digest) >= self.max_processes:
                # Evict LRU
                evict_digest, _ = self._lru.popitem(last=False)
                try:
                    self._by_digest.pop(evict_digest).close()
                except Exception:
                    pass  # Process might already be dead
            
            # Create new process
            venv_dir = self._ensure_venv(digest, bundle_dir)
            proc = WarmProcess(
                python_bin=venv_dir / 'bin' / 'python',
                bundle_dir=bundle_dir,
                mem_limit_bytes=self.mem_limit_bytes
            )
            self._by_digest[digest] = proc
        
        # Touch LRU (move to end)
        self._lru.pop(digest, None)
        self._lru[digest] = None
        return proc
    
    def _ensure_venv(self, digest: str, bundle_dir: Path) -> Path:
        """Ensure venv exists for bundle using uv."""
        venv_dir = self.venvs_dir / digest[:8]
        if not venv_dir.exists():
            # Use uv for reproducible environments
            import subprocess
            subprocess.run(['uv', 'venv', str(venv_dir)], check=True)
            
            # Sync dependencies from uv.lock
            lock_file = bundle_dir / 'uv.lock'
            if lock_file.exists():
                subprocess.run(
                    ['uv', 'sync', '--frozen'],
                    cwd=bundle_dir,
                    env={'VIRTUAL_ENV': str(venv_dir)},
                    check=True
                )
        return venv_dir
    
    def shutdown_all(self):
        """Shutdown all warm processes."""
        for proc in self._by_digest.values():
            try:
                proc.close()
            except Exception as e:
                logging.warning(f"Error closing process: {e}")
        
        self._by_digest.clear()
        self._lru.clear()
    
    def active_count(self) -> int:
        """Count of active processes."""
        return len(self._by_digest)
    
    def get_uv_lock_hash(self, digest: str) -> Optional[str]:
        """Get hash of uv.lock for fingerprinting."""
        # Implementation would hash the uv.lock file
        return None
```

### 5.3 WarmProcess with Stderr Handling

```python
# modelops/adapters/exec_env/process.py
import os
import subprocess
import threading
import resource
import logging
from pathlib import Path
from typing import Optional, Dict
from ..json_rpc import JSONRPCClient

logger = logging.getLogger("modelops.warm_process")

class WarmProcess:
    """Single warm subprocess for a bundle with proper stderr handling."""
    
    def __init__(
        self,
        python_bin: Path,
        bundle_dir: Path,
        mem_limit_bytes: Optional[int] = None
    ):
        self.python_bin = python_bin
        self.bundle_dir = bundle_dir
        self.mem_limit_bytes = mem_limit_bytes
        self._proc: Optional[subprocess.Popen] = None
        self._rpc: Optional[JSONRPCClient] = None
        self._lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._start()
    
    def _start(self):
        """Start the subprocess with proper stderr handling."""
        env = os.environ.copy()
        env['PYTHONNOUSERSITE'] = '1'
        env['MODELOPS_BUNDLE_PATH'] = str(self.bundle_dir)
        
        def _limits():
            if self.mem_limit_bytes:
                resource.setrlimit(
                    resource.RLIMIT_AS,
                    (self.mem_limit_bytes, self.mem_limit_bytes)
                )
        
        self._proc = subprocess.Popen(
            [str(self.python_bin), '-m', 'modelops.isolated_worker'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # Capture stderr to avoid deadlocks
            bufsize=0,
            preexec_fn=_limits if os.name != 'nt' else None,
            env=env
        )
        
        # Start stderr reader thread to prevent buffer deadlock
        self._stderr_thread = threading.Thread(
            target=self._pump_stderr,
            daemon=True
        )
        self._stderr_thread.start()
        
        self._rpc = JSONRPCClient(
            self._proc.stdin,
            self._proc.stdout
        )
    
    def _pump_stderr(self):
        """Read stderr in background thread to prevent deadlock."""
        try:
            for line in iter(self._proc.stderr.readline, b''):
                if line:
                    # Log with context about which process this is from
                    logger.info(
                        f"[bundle={self.bundle_dir.name}] {line.decode(errors='replace').rstrip()}"
                    )
        except Exception as e:
            logger.error(f"Error reading stderr: {e}")
    
    def is_alive(self) -> bool:
        """Check if subprocess is still running."""
        return self._proc and self._proc.poll() is None
    
    def execute(
        self,
        entrypoint: str,
        params: dict,
        seed: int,
        req_id: str
    ) -> Dict[str, str]:
        """Execute simulation via JSON-RPC."""
        with self._lock:
            if not self.is_alive():
                logger.warning("Process died, restarting...")
                self._start()
            
            return self._rpc.call(
                'execute',
                {
                    'entrypoint': entrypoint,
                    'params': params,
                    'seed': seed
                },
                req_id
            )
    
    def close(self):
        """Terminate the subprocess cleanly."""
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.warning("Process didn't terminate, killing...")
                self._proc.kill()
                self._proc.wait()
```

### 5.4 Minimal JSON-RPC Implementation

```python
# modelops/adapters/json_rpc.py
"""Minimal JSON-RPC 2.0 over stdio with Content-Length framing.

This is a tiny, dependency-free implementation perfect for our use case:
- Single warm process per digest
- Local stdio IPC
- Synchronous calls
- No HTTP/WebSocket complexity
"""

import io
import json
import sys
from typing import Any, Dict, Optional

JSONRPC_VERSION = "2.0"

def _read_headers(stream: io.BufferedReader) -> Optional[int]:
    """Read header lines until blank line; return Content-Length or None on EOF."""
    content_length = None
    while True:
        line = stream.readline()
        if not line:
            return None  # EOF
        if line == b"\r\n":
            break
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
    return content_length

def recv_message(stream: io.BufferedReader) -> Optional[Dict[str, Any]]:
    """Receive a JSON-RPC message with Content-Length framing."""
    length = _read_headers(stream)
    if length is None:
        return None
    payload = stream.read(length)
    return json.loads(payload.decode("utf-8"))

def send_message(stream: io.BufferedWriter, msg: Dict[str, Any]) -> None:
    """Send a JSON-RPC message with Content-Length framing."""
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
    stream.write(data)
    stream.flush()

def make_request(method: str, params: Dict[str, Any], id: str) -> Dict[str, Any]:
    """Create a JSON-RPC request."""
    return {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params, "id": id}

def make_result(id: str, result: Any) -> Dict[str, Any]:
    """Create a JSON-RPC result response."""
    return {"jsonrpc": JSONRPC_VERSION, "result": result, "id": id}

def make_error(id: Optional[str], code: int, message: str, data: Any = None) -> Dict[str, Any]:
    """Create a JSON-RPC error response."""
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "error": err, "id": id}


class JSONRPCClient:
    """Simple JSON-RPC client for subprocess communication."""
    
    def __init__(self, stdin: io.BufferedWriter, stdout: io.BufferedReader):
        self.stdin = stdin
        self.stdout = stdout
        self._id_counter = 0
    
    def call(
        self,
        method: str,
        params: Dict[str, Any],
        req_id: Optional[str] = None
    ) -> Any:
        """Make a JSON-RPC call and wait for response."""
        if req_id is None:
            req_id = str(self._id_counter)
            self._id_counter += 1
        
        # Send request
        request = make_request(method, params, req_id)
        send_message(self.stdin, request)
        
        # Read response
        response = recv_message(self.stdout)
        
        if response is None:
            raise RuntimeError("Connection closed")
        
        # Check for error
        if 'error' in response:
            raise RuntimeError(f"RPC error: {response['error']}")
        
        return response.get('result')
```

### 5.5 Bundle Repository Adapters

#### 5.5.1 ModelOps Bundle Repository (OCI)

```python
# modelops/adapters/bundle/modelops_bundle_repo.py
from pathlib import Path
from typing import Tuple
from modelops_contracts.ports import BundleRepository
from modelops_bundle.ops import ensure_local
from modelops_bundle.oras import OrasAdapter
from modelops_bundle.core import BundleConfig

class ModelOpsBundleRepository(BundleRepository):
    """Bundle repository adapter using modelops-bundle.
    
    This is a thin wrapper around modelops-bundle's ensure_local operation.
    It ONLY fetches model code & data - no result storage!
    
    STRICT: No implicit defaults, bundle references must be explicit.
    """
    
    def __init__(
        self,
        registry_ref: str,
        cache_dir: Path
        # NO default_tag parameter - must be explicit
    ):
        """Initialize with registry info.
        
        Args:
            registry_ref: Base registry (e.g., "ghcr.io/org/models")
            cache_dir: Local directory for materialized bundles
        """
        self.registry_ref = registry_ref
        self.cache_dir = cache_dir
        self.adapter = OrasAdapter()
    
    def ensure_local(self, bundle_ref: str) -> Tuple[str, Path]:
        """Fetch bundle - bundle_ref MUST be explicit.
        
        Args:
            bundle_ref: MUST be one of:
                - "sha256:abc123..." (content-addressed digest)
                - "oci://ghcr.io/org/bundle:v1.0.0" (explicit tag)
                - "oci://ghcr.io/org/bundle@sha256:abc..." (explicit digest)
                
            NOT ALLOWED:
                - "latest" (ambiguous)
                - "v1.0.0" (missing registry)
                - "" (empty)
                
        Returns:
            Tuple of (canonical_digest, local_path)
        """
        # STRICT validation
        if not bundle_ref:
            raise ValueError("bundle_ref cannot be empty")
        
        if bundle_ref == "latest":
            raise ValueError(
                "Implicit 'latest' tag not allowed. "
                "Specify explicit version: 'oci://registry/repo:v1.0.0'"
            )
        
        # Parse bundle reference
        if bundle_ref.startswith("sha256:"):
            # Direct digest - use configured registry
            registry_and_repo = self.registry_ref
            ref = bundle_ref
            
        elif bundle_ref.startswith("oci://"):
            # Full OCI reference - must include tag or digest
            parts = bundle_ref[6:]  # Remove oci://
            
            # Check for @digest
            if "@" in parts:
                registry_and_repo, ref = parts.split("@", 1)
                if not ref.startswith("sha256:"):
                    raise ValueError(f"Invalid digest format: {ref}")
                    
            # Check for :tag
            elif ":" in parts:
                # Split only on last : to handle port numbers
                idx = parts.rfind(":")
                registry_and_repo = parts[:idx]
                ref = parts[idx+1:]
                
                # Validate tag is not empty or "latest"
                if not ref or ref == "latest":
                    raise ValueError(
                        f"Bundle ref must specify explicit version, not '{ref}'"
                    )
            else:
                raise ValueError(
                    f"OCI reference must include tag or digest: {bundle_ref}"
                )
        else:
            # Not a valid format
            raise ValueError(
                f"Invalid bundle_ref format: {bundle_ref}. "
                "Must be 'sha256:...', 'oci://registry/repo:tag', "
                "or 'oci://registry/repo@sha256:...'"
            )
        
        # Create config for this specific operation
        config = BundleConfig(
            registry_ref=registry_and_repo,
            default_tag=None  # Never use defaults
        )
        
        # Resolve to get the digest for cache directory naming
        digest = self.adapter.resolve_tag_to_digest(registry_and_repo, ref)
        dest_dir = self.cache_dir / digest.replace(":", "_")
        
        # Use modelops-bundle's ensure_local
        from modelops_bundle.ops import ensure_local as bundle_ensure_local
        result = bundle_ensure_local(
            config=config,
            ref=ref,
            dest=dest_dir,
            mirror=True,  # Clean directory - only bundle contents
            dry_run=False
        )
        
        return (result.resolved_digest, dest_dir)
    
    def exists(self, bundle_ref: str) -> bool:
        """Check if bundle exists in registry."""
        try:
            # Parse ref similar to ensure_local
            if bundle_ref.startswith("oci://"):
                parts = bundle_ref[6:].split(":", 1)
                registry = parts[0]
                ref = parts[1] if len(parts) > 1 else None
                if not ref:
                    return False
            elif bundle_ref.startswith("sha256:"):
                registry = self.registry_ref
                ref = bundle_ref
            else:
                return False  # Invalid format
            
            # Just check if we can resolve it
            digest = self.adapter.resolve_tag_to_digest(registry, ref)
            return digest is not None
        except:
            return False
```

#### 5.5.2 File Bundle Repository (Development)

```python
# modelops/adapters/bundle/file_repo.py
import hashlib
from pathlib import Path
from typing import Tuple
from modelops_contracts.ports import BundleRepository

class FileBundleRepository(BundleRepository):
    """Simple filesystem bundle repository for development.
    
    Expects bundles to be pre-staged on the filesystem.
    Useful for local development without OCI registry.
    """
    
    def __init__(self, bundles_dir: Path):
        """Initialize with local bundles directory.
        
        Args:
            bundles_dir: Root directory containing bundles
                        Each subdirectory is a bundle
        """
        self.bundles_dir = bundles_dir
        if not self.bundles_dir.exists():
            raise ValueError(f"Bundles directory does not exist: {bundles_dir}")
    
    def ensure_local(self, bundle_ref: str) -> Tuple[str, Path]:
        """Look up bundle on filesystem.
        
        Args:
            bundle_ref: Bundle name (subdirectory name)
                       or "file:///absolute/path"
        
        Returns:
            Tuple of (computed_digest, bundle_path)
        """
        if bundle_ref.startswith("file://"):
            # Absolute path reference
            bundle_path = Path(bundle_ref[7:])
        else:
            # Relative to bundles_dir
            bundle_path = self.bundles_dir / bundle_ref
        
        if not bundle_path.exists():
            raise FileNotFoundError(f"Bundle not found: {bundle_path}")
        
        if not bundle_path.is_dir():
            raise ValueError(f"Bundle must be a directory: {bundle_path}")
        
        # Compute digest of directory contents for consistency
        hasher = hashlib.sha256()
        
        # Sort files for deterministic hashing
        for file_path in sorted(bundle_path.rglob("*")):
            if file_path.is_file():
                # Include relative path in hash for structure
                rel_path = file_path.relative_to(bundle_path)
                hasher.update(str(rel_path).encode())
                hasher.update(b"\0")  # Separator
                
                # Include file size for quick change detection
                hasher.update(str(file_path.stat().st_size).encode())
                hasher.update(b"\0")
                
                # Include file contents
                hasher.update(file_path.read_bytes())
        
        digest = f"sha256:{hasher.hexdigest()}"
        
        return (digest, bundle_path)
    
    def exists(self, bundle_ref: str) -> bool:
        """Check if bundle exists on filesystem."""
        if bundle_ref.startswith("file://"):
            return Path(bundle_ref[7:]).exists()
        return (self.bundles_dir / bundle_ref).exists()
```

---

## 6. Primary Adapters

The Dask adapter is now much simpler with WorkerPlugin handling initialization.

```python
# modelops/adapters/dask/service.py
from typing import List, Optional
from dask.distributed import Client, Future as DaskFuture, get_worker
from modelops_contracts import SimTask, SimReturn
from modelops_contracts.ports import SimulationService, Future

class DaskSimulationService(SimulationService):
    """Dask distributed simulation service adapter.
    
    Much simpler now - WorkerPlugin handles all initialization!
    """
    
    def __init__(self, client: Client):
        self.client = client
    
    def submit(self, task: SimTask) -> Future[SimReturn]:
        """Submit task to Dask cluster."""
        dask_future = self.client.submit(
            _worker_run_task,
            task,
            pure=False  # Tasks have unique IDs
        )
        return DaskFutureAdapter(dask_future)
    
    def gather(self, futures: List[Future[SimReturn]]) -> List[SimReturn]:
        """Gather results from futures."""
        dask_futures = [f.wrapped for f in futures]
        return self.client.gather(dask_futures)
    
    def submit_batch(self, tasks: List[SimTask]) -> List[Future[SimReturn]]:
        """Submit multiple tasks efficiently."""
        dask_futures = self.client.map(
            _worker_run_task,
            tasks,
            pure=False
        )
        return [DaskFutureAdapter(f) for f in dask_futures]

class DaskFutureAdapter:
    """Adapt Dask Future to our Future protocol."""
    
    def __init__(self, dask_future: DaskFuture):
        self.wrapped = dask_future
    
    def result(self, timeout: Optional[float] = None) -> SimReturn:
        return self.wrapped.result(timeout=timeout)
    
    def done(self) -> bool:
        return self.wrapped.done()
    
    def cancel(self) -> bool:
        return self.wrapped.cancel()
    
    def exception(self) -> Optional[Exception]:
        return self.wrapped.exception()

def _worker_run_task(task: SimTask) -> SimReturn:
    """Execute task on worker using plugin-initialized runtime.
    
    This is beautifully simple now - no singletons, no tricks!
    """
    worker = get_worker()
    
    if not hasattr(worker, 'modelops_runtime'):
        raise RuntimeError(
            "ModelOps runtime not initialized. "
            "Ensure ModelOpsWorkerPlugin is registered with the client."
        )
    
    return worker.modelops_runtime.execute(task)
```

---

## 7. Wire Protocol & Dependency Inversion

### 7.1 Entry Point Discovery Pattern

ModelOps uses Python's standard entry points mechanism to discover the wire function,
achieving **perfect dependency inversion**:

- **ModelOps doesn't depend on Calabaria** - Only discovers it at runtime
- **Science bundles stay pure** - Just list Calabaria as a dependency
- **No wire.py in user code** - Infrastructure stays in framework

### 7.2 How It Works

#### Calabaria Package Declares Entry Point
```toml
# In calabaria package's pyproject.toml
[project.entry-points."modelops.wire"]
execute = "calabaria.wire:wire_function"
```

#### Science Bundle Just Depends on Calabaria
```toml
# In user's bundle pyproject.toml or requirements.txt
[project]
dependencies = [
    "calabaria>=1.0.0",  # Brings the wire entry point
    "numpy>=1.20.0",
    "pandas>=1.3.0"
]
# NO wire code, NO entry points, ONLY science!
```

#### Bundle Structure (Pure Science)
```
Bundle sha256:abc123...
├── covid/
│   └── models.py         # User's simulation models
├── data/
│   └── parameters.csv    # Scientific data
├── pyproject.toml        # Dependencies only
└── requirements.txt      # Alternative to pyproject.toml
```

### 7.3 Subprocess Implementation

```python
# modelops/worker/subprocess_runner.py
import importlib.metadata
import sys
from pathlib import Path

class SubprocessRunner:
    def __init__(self, bundle_path: Path, bundle_digest: str):
        # Setup venv and install dependencies (including calabaria)
        self._setup_environment(bundle_path)
        
        # Add bundle to path for user's science code
        sys.path.insert(0, str(bundle_path))
        
        # Discover wire function via entry points
        self.wire_fn = self._discover_wire_function()
        self.bundle_digest = bundle_digest
    
    def _discover_wire_function(self):
        """Discover wire function from installed packages.
        
        This uses Python's standard entry point mechanism.
        Calabaria (or any framework) registers the entry point.
        """
        eps = importlib.metadata.entry_points(group='modelops.wire')
        if not eps:
            raise RuntimeError(
                "No modelops.wire entry point found. "
                "Is calabaria installed in the environment?"
            )
        
        if len(eps) > 1:
            raise RuntimeError(
                f"Multiple modelops.wire entry points found: {list(eps)}. "
                "Only one wire implementation should be installed."
            )
        
        # Load the entry point
        ep = next(iter(eps))
        return ep.load()
    
    def execute(self, entrypoint: str, params: Dict, seed: int) -> Dict[str, str]:
        """Execute simulation using discovered wire function.
        
        The wire function is provided by Calabaria (or another framework)
        and implements the WireFunction protocol.
        
        Returns:
            Dict[str, str]: Artifact names to base64-encoded data strings
        """
        # Call the discovered wire function
        result_bytes = self.wire_fn(entrypoint, params, seed)
        
        # Base64 encode for JSON transport
        import base64
        return {
            name: base64.b64encode(data).decode('ascii')
            for name, data in result_bytes.items()
        }
```

### 7.4 Calabaria's Wire Implementation

```python
# In calabaria package: calabaria/wire.py
from typing import Dict, Any
import importlib
from calabaria.core import parse_entrypoint

def wire_function(entrypoint: str, params: Dict[str, Any], seed: int) -> Dict[str, bytes]:
    """Calabaria's implementation of the wire protocol.
    
    This is NOT in user code - it's part of the Calabaria framework.
    """
    # Parse entrypoint to get import path and scenario
    import_path, scenario = parse_entrypoint(entrypoint)
    
    # Dynamically import user's model from bundle
    module_path, class_name = import_path.rsplit('.', 1)
    module = importlib.import_module(module_path)
    model_class = getattr(module, class_name)
    
    # Instantiate and run simulation
    model = model_class()
    result = model.simulate(params, seed, scenario=scenario)
    
    # Serialize to Arrow IPC or Parquet bytes
    return serialize_to_bytes(result)
```

### 7.5 Benefits of This Design

1. **True Dependency Inversion**
   - ModelOps depends only on the protocol, not Calabaria
   - Discovered at runtime via standard Python mechanisms

2. **Science Bundles Stay Pure**
   - No infrastructure code whatsoever
   - Just list Calabaria as a dependency
   - Focus entirely on scientific modeling

3. **Framework Flexibility**
   - While we only support Calabaria now, the pattern allows future frameworks
   - Any framework can provide a `modelops.wire` entry point

4. **Standard Python Patterns**
   - Uses well-established entry points mechanism
   - Same pattern as pytest plugins, flask extensions, etc.
   - Well-documented and understood by Python developers

---

## 8. Components to Remove

With the WorkerPlugin architecture, we can remove significant complexity:

### 8.1 Old Runner Code
```python
# DELETE these files entirely:
src/modelops/runtime/runners.py  # DirectRunner, all runner classes
src/modelops/runtime/environment.py  # BundleRunner, CachedBundleRunner
src/modelops/runtime/user_runner.py  # Unused user-specific runner

# These were never properly integrated and represent incomplete designs
```

### 8.2 Singleton Bootstrap Code
```python
# DELETE this pattern everywhere:
@lru_cache(maxsize=1)
def _worker_warm_executor() -> IsolatedWarmProcessExecutor:
    return IsolatedWarmProcessExecutor(...)  # No longer needed!

# DELETE global singletons:
_worker_runtime: Optional[Callable] = None  # Gone!
```

### 8.3 Complex Bootstrap Functions
```python
# SIMPLIFY or DELETE:
def get_worker_runtime() -> Callable:  # No longer needed
def _bootstrap_runtime(config) -> Callable:  # Moved to WorkerPlugin.setup()
```

### 8.4 Configuration Hashing Workarounds
```python
# DELETE hashability workarounds:
@dataclass(frozen=True)  # for LRU cache
class WorkerConfig:  # No longer needs to be hashable!
    ...
```

---

## 9. State Management & Bundle Invariants

### 9.1 The Bundle Digest Guarantee

**Invariant**: When user changes code and pushes a new bundle, we use new processes.

This is **guaranteed** by our architecture:

```
User pushes new code → New bundle → New digest

Bundle v1 (digest: abc123)
  → Warm process #1 cached at key "abc123"
  → Venv at /tmp/venvs/abc123/
  
User changes code → Bundle v2 (digest: def456)  
  → Creates NEW process #2 at key "def456"
  → NEW venv at /tmp/venvs/def456/
  → Old process still exists but won't be used for new tasks
```

### 9.2 Worker State Structure

Each Dask worker maintains:

```
Worker Instance
├── modelops_runtime (SimulationExecutor)
│   └── exec_env (ExecutionEnvironment)
│       ├── bundle_repo (stateless - just fetches)
│       ├── cas (stateless - just stores)
│       └── process_manager
│           └── _processes: OrderedDict[digest → WarmProcess]
│                 ├── Key is bundle digest (content hash)
│                 ├── LRU eviction when full
│                 └── Each process has its own venv
```

### 9.3 Cache Invalidation

**Automatic invalidation** happens when:
- Bundle content changes → new digest → new cache entry
- Process dies → removed from cache on next access
- Cache full → LRU eviction of oldest processes

**No manual invalidation needed** because:
- Digest is content-based (deterministic)
- Same code always produces same digest
- Different code always produces different digest

### 9.4 Process Isolation

Each warm process is isolated:
- **Own virtual environment** at `/tmp/venvs/{digest}/`
- **Own Python interpreter** via subprocess
- **Own dependencies** from bundle's requirements.txt/pyproject.toml
- **Own memory space** (no GIL contention)

### 9.5 Concurrency Model

```
Dask Worker (single process)
├── Can handle multiple tasks concurrently
├── Each task runs in its own warm subprocess
└── Subprocesses are reused across tasks with same bundle

Task A (bundle abc123) → Process #1
Task B (bundle abc123) → Process #1 (reused)
Task C (bundle def456) → Process #2 (different bundle)
Task D (bundle abc123) → Process #1 (reused again)
```

### 9.6 State Consistency

**No shared mutable state** between:
- Different workers (each has own process cache)
- Different tasks (each gets fresh execution)
- Different bundles (each has own process)

**Deterministic execution** because:
- Bundle digest determines process
- Seed determines randomness
- Parameters determine computation
- No hidden state carries between executions

---

## 10. Engineering Tradeoffs

### 10.1 WorkerPlugin vs Manual Bootstrap

**Decision:** Use Dask's native WorkerPlugin

**Rationale:**
- **Native lifecycle:** Dask manages setup/teardown
- **Framework integration:** Works with Dask, not against it
- **Clean separation:** Plugin is composition root
- **No singletons:** No global state needed

**Tradeoff:**
- Dask-specific solution (but we're using Dask anyway)
- Must register plugin from client side

### 10.2 Single Process vs Process Pool

**Decision:** Keep single warm process per bundle (unchanged)

**Rationale:**
- **Simplicity:** No work distribution
- **Memory efficiency:** One process = 200-500MB
- **Dask parallelism:** Scale via workers, not processes

### 10.3 Configuration in Plugin Constructor

**Decision:** Pass config to plugin, not to setup()

**Rationale:**
- **Clean API:** Setup has standard signature
- **Testability:** Can inject test config
- **Flexibility:** Can override per deployment

---

## 10. Implementation Plan

### Phase 1: Contracts Extension (2 days)
1. Add `shutdown()` to ExecutionEnvironment port
2. Ensure all ports are in modelops-contracts
3. Version and release contracts

### Phase 2: WorkerPlugin Implementation (3 days)
1. Create `ModelOpsWorkerPlugin` class
2. Implement factories for all adapters
3. Add RuntimeConfig with env loading
4. Test plugin lifecycle

### Phase 3: Simplify Adapters (2 days)
1. Remove singleton patterns from adapters
2. Add proper shutdown methods
3. Simplify worker task function
4. Remove bootstrap complexity

### Phase 4: Integration Testing (3 days)
1. Test plugin registration
2. Test worker initialization
3. Test task execution
4. Test clean shutdown

### Phase 5: Remove Old Code (1 day)
1. Delete old runner classes
2. Remove singleton patterns
3. Clean up unused imports
4. Update documentation

### Phase 6: Deployment (2 days)
1. Update Dask worker images
2. Modify client initialization
3. Test in staging environment
4. Roll out to production

**Total: ~2 weeks**

---

## 11. Migration Strategy

### 11.1 Backward Compatibility

During migration, support both patterns:

```python
# In client code
def create_simulation_service(use_plugin: bool = False):
    client = Client(scheduler_address)
    
    if use_plugin:
        # New way - WorkerPlugin
        plugin = ModelOpsWorkerPlugin()
        client.register_plugin(plugin)
        return DaskSimulationService(client)
    else:
        # Old way - singleton bootstrap
        # Workers use get_worker_runtime()
        return LegacyDaskSimulationService(client)
```

### 11.2 Staged Rollout

1. **Deploy plugin code** alongside old code
2. **Test with feature flag** in staging
3. **Gradual rollout** by updating client initialization
4. **Remove old code** after validation

### 11.3 Rollback Plan

If issues arise:
1. Don't register plugin (falls back to old bootstrap)
2. Workers still have old code available
3. No data migration needed

---

## 12. Unresolved Issues

### MVP vs Production Features

**Deferred for Post-MVP (Keep Code Clean):**
- **RPC Timeouts**: Not needed for MVP - add if hangs become an issue
- **Dask Resource Constraints**: Skip `resources={"modelops": 1}` for now
- **Advanced Memory Limits**: RLIMIT_AS is sufficient for MVP, enhance for K8s cgroups later
- **Bundle Fetch Retries**: Start with simple timeouts, add retry logic if needed

**Focus on MVP Essentials:**
- ✅ WorkerPlugin composition root
- ✅ OrderedDict process management  
- ✅ Minimal JSON-RPC with Content-Length framing
- ✅ uv for reproducible environments
- ✅ Stderr handling to prevent deadlocks

### Remaining Open Questions

1. **Secret Management**
   - How to pass registry credentials to workers?
   - Options: K8s secrets, environment variables, secret stores

2. **Bundle Caching Strategy**
   - When to evict cached bundles from disk?
   - How to handle bundle updates with same tag?

3. **Process Health Monitoring**
   - Active health checks vs passive detection?
   - How to report unhealthy processes?

4. **Metrics Collection**
   - Where to send telemetry data?
   - What metrics are most valuable?

5. **Multi-Region CAS**
   - How to handle geo-distributed storage?
   - Replication vs regional buckets?

6. **Bundle Fetch Timeouts**
   - How long to wait for OCI pulls?
   - Retry strategy for network issues?

7. **Memory Limit Enforcement**
   - How to handle processes exceeding limits?
   - Graceful degradation vs hard kill?

8. **Worker Affinity**
   - Should certain bundles prefer certain workers?
   - How to implement soft affinity?

9. **Future: Failure Tracking**
   - Could add circuit breaker pattern later if needed
   - Redis vs in-memory?

10. **Configuration Validation**
    - How to validate config before worker startup?
    - Schema validation vs runtime checks?

11. **Graceful Shutdown Ordering**
    - What order to shutdown resources?
    - How long to wait for in-flight tasks?

---

## Appendix: Key Improvements from v3

| Aspect | v3 (Previous) | v3.1 (This Document) |
|--------|---------------|----------------------|
| **Composition Root** | Complex bootstrap functions | Clean WorkerPlugin.setup() |
| **Configuration** | Hashability issues with LRU | Simple config in plugin |
| **Lifecycle** | Manual management | Native Dask hooks |
| **Worker State** | Global singletons | Attached to worker instance |
| **Shutdown** | Unclear/manual | plugin.teardown() |
| **Code Complexity** | Many workarounds | Straightforward |
| **Testing** | Hard to test bootstrap | Easy to test plugin |
| **Framework Fight** | Working against Dask | Working with Dask |

The WorkerPlugin solution eliminates entire categories of problems while providing proper lifecycle management and clean architecture.
