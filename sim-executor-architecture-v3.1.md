# ModelOps Simulation Execution Architecture v3.1 — WorkerPlugin Solution

**Status:** Implementation blueprint  
**Audience:** ModelOps & Calabaria engineers  
**Architecture:** Hexagonal (Ports & Adapters) with Dask WorkerPlugin  
**Primary goals:** Clean architecture, proper lifecycle, low latency, testability

---

## Executive Summary

This document describes a **Hexagonal Architecture** for ModelOps simulation execution using **Dask's WorkerPlugin** as the composition root:

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
┌────────────────────────────────────────────────────────────┐
│                    DASK SCHEDULER                          │
│  ┌──────────────────────────────────────────────────┐     │
│  │  client.register_plugin(ModelOpsWorkerPlugin())  │     │
│  └──────────────────────┬───────────────────────────┘     │
└─────────────────────────┼──────────────────────────────────┘
                          │ Distributes plugin
                          ▼ to all workers
┌─────────────────────────────────────────────────────────────┐
│                    DASK WORKER                               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │         ModelOpsWorkerPlugin (Composition Root)      │   │
│  │                                                      │   │
│  │  def setup(self, worker):                          │   │
│  │      # Wire ALL dependencies here ONCE             │   │
│  │      config = RuntimeConfig.from_env()             │   │
│  │      cas = create_cas(config)                      │   │
│  │      bundle_repo = create_bundle_repo(config)      │   │
│  │      exec_env = create_exec_env(config, ...)       │   │
│  │      executor = SimulationExecutor(...)            │   │
│  │      worker.modelops_runtime = executor.execute    │   │
│  │                                                      │   │
│  │  def teardown(self, worker):                       │   │
│  │      # Clean shutdown of all resources             │   │
│  │      worker.modelops_runtime.shutdown()            │   │
│  └────────────────────┬─────────────────────────────────┘   │
│                       │                                      │
│  ┌────────────────────▼─────────────────────────────────┐   │
│  │              Task Execution                          │   │
│  │                                                      │   │
│  │  def _worker_run_task(task: SimTask) -> SimReturn:  │   │
│  │      runtime = get_worker(worker_id).modelops_runtime│   │
│  │      return runtime(task)                           │   │
│  └──────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────┘
                          │
                          ▼
     ┌────────────────────────────────────────────────┐
     │           SimulationService Port               │
     │            (Primary/Driving Port)              │
     └────────────────────┬───────────────────────────┘
                          │
     ┌────────────────────▼───────────────────────────┐
     │            CORE DOMAIN (HEXAGON)              │
     │                                                │
     │         Simulation Execution Logic            │
     │   • Task validation                           │
     │   • Execution orchestration                   │
     │   • Result assembly                           │
     │   • Fingerprinting                            │
     │   • Circuit breaking                          │
     └────┬──────────┬──────────┬──────────┬─────────┘
          │          │          │          │
     ┌────▼────┐ ┌──▼───┐ ┌───▼────┐ ┌───▼────┐
     │Execution│ │ CAS  │ │Bundle  │ │Process │
     │Env Port │ │ Port │ │Repo    │ │Mgmt    │
     └─────────┘ └──────┘ └────────┘ └────────┘
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
from modelops.core.executor import SimulationExecutor, CircuitBreaker
from modelops.adapters.exec_env.isolated_warm import IsolatedWarmExecEnv
from modelops.adapters.bundle.oci_repo import OCIBundleRepository
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
        cas = self._create_cas(config)
        
        # Create bundle repository
        bundle_repo = self._create_bundle_repo(config)
        
        # Create execution environment with its dependencies
        exec_env = self._create_exec_env(config, bundle_repo, cas)
        
        # Create circuit breaker
        circuit_breaker = CircuitBreaker(
            failure_threshold=config.circuit_breaker_threshold,
            reset_timeout=config.circuit_breaker_timeout
        )
        
        # Create the core domain executor
        executor = SimulationExecutor(
            exec_env=exec_env,
            bundle_repo=bundle_repo,
            cas=cas,
            circuit_breaker=circuit_breaker
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
        
        # Clean shutdown of warm processes
        if hasattr(worker, 'modelops_exec_env'):
            if hasattr(worker.modelops_exec_env, 'shutdown'):
                worker.modelops_exec_env.shutdown()
        
        # Remove references
        if hasattr(worker, 'modelops_runtime'):
            delattr(worker, 'modelops_runtime')
        if hasattr(worker, 'modelops_exec_env'):
            delattr(worker, 'modelops_exec_env')
    
    def _create_cas(self, config: 'RuntimeConfig') -> CAS:
        """Factory for CAS adapters."""
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
    
    def _create_bundle_repo(self, config: 'RuntimeConfig') -> BundleRepository:
        """Factory for bundle repository."""
        if config.bundle_source == 'oci':
            return OCIBundleRepository(
                cache_dir=Path(config.bundles_dir),
                registry_url=config.registry_url
            )
        elif config.bundle_source == 'file':
            from modelops.adapters.bundle.file_repo import FileBundleRepository
            return FileBundleRepository(Path(config.bundles_dir))
        else:
            raise ValueError(f"Unknown bundle source: {config.bundle_source}")
    
    def _create_exec_env(
        self,
        config: 'RuntimeConfig',
        bundle_repo: BundleRepository,
        cas: CAS
    ) -> ExecutionEnvironment:
        """Factory for execution environment."""
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
    
    # Bundle source
    bundle_source: str = 'oci'  # 'oci' or 'file'
    registry_url: str = 'ghcr.io'
    
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
    
    # Circuit breaker
    circuit_breaker_threshold: int = 3
    circuit_breaker_timeout: int = 60
    
    @classmethod
    def from_env(cls) -> 'RuntimeConfig':
        """Create configuration from environment variables."""
        return cls(
            bundles_dir=os.getenv('MODELOPS_BUNDLES_DIR', cls.bundles_dir),
            venvs_dir=os.getenv('MODELOPS_VENVS_DIR', cls.venvs_dir),
            bundle_source=os.getenv('MODELOPS_BUNDLE_SOURCE', cls.bundle_source),
            registry_url=os.getenv('MODELOPS_REGISTRY_URL', cls.registry_url),
            cas_backend=os.getenv('MODELOPS_CAS_BACKEND', cls.cas_backend),
            cas_bucket=os.getenv('MODELOPS_CAS_BUCKET', cls.cas_bucket),
            cas_prefix=os.getenv('MODELOPS_CAS_PREFIX', cls.cas_prefix),
            cas_region=os.getenv('MODELOPS_CAS_REGION', cls.cas_region),
            azure_storage_account=os.getenv('MODELOPS_AZURE_STORAGE_ACCOUNT'),
            executor_type=os.getenv('MODELOPS_EXECUTOR_TYPE', cls.executor_type),
            mem_limit_bytes=int(os.getenv('MODELOPS_MEM_LIMIT_BYTES', str(cls.mem_limit_bytes))),
            max_warm_processes=int(os.getenv('MODELOPS_MAX_WARM_PROCESSES', str(cls.max_warm_processes))),
            circuit_breaker_threshold=int(os.getenv('MODELOPS_CIRCUIT_BREAKER_THRESHOLD', str(cls.circuit_breaker_threshold))),
            circuit_breaker_timeout=int(os.getenv('MODELOPS_CIRCUIT_BREAKER_TIMEOUT', str(cls.circuit_breaker_timeout)))
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
    client.register_plugin(plugin)
    
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
from typing import Protocol, Generic, TypeVar, Optional, List, Dict, Any
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

## 4. Core Domain

The core domain remains pure business logic with no infrastructure dependencies.

```python
# modelops/core/executor.py
from typing import Dict, Optional
from modelops_contracts import SimTask, SimReturn
from modelops_contracts.ports import (
    ExecutionEnvironment, BundleRepository, CAS
)
from .fingerprint import make_exec_fingerprint
from .artifacts import bytes_to_artifact

class SimulationExecutor:
    """Core domain service for simulation execution.
    
    This is the hexagon - knows nothing about Dask, WorkerPlugin, etc.
    It only orchestrates simulation execution using ports.
    """
    
    def __init__(
        self,
        exec_env: ExecutionEnvironment,
        bundle_repo: BundleRepository,
        cas: CAS,
        circuit_breaker: Optional['CircuitBreaker'] = None
    ):
        self.exec_env = exec_env
        self.bundle_repo = bundle_repo
        self.cas = cas
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
    
    def execute(self, task: SimTask) -> SimReturn:
        """Execute a simulation task.
        
        Core orchestration logic:
        1. Check circuit breaker
        2. Ensure bundle is available
        3. Execute in environment
        4. Convert outputs to artifacts
        5. Add fingerprint
        """
        # Extract canonical digest
        digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)
        
        # Check circuit breaker
        if self.circuit_breaker.is_open(digest):
            raise RuntimeError(f"Circuit open for bundle {digest}")
        
        try:
            # Execute in isolated environment
            result = self.exec_env.run(task)
            
            # Success - reset circuit breaker
            self.circuit_breaker.record_success(digest)
            
            return result
            
        except Exception as e:
            # Record failure for circuit breaker
            self.circuit_breaker.record_failure(digest)
            raise
    
    def shutdown(self):
        """Clean shutdown of executor."""
        if hasattr(self.exec_env, 'shutdown'):
            self.exec_env.shutdown()
```

---

## 5. Secondary Adapters

### 5.1 Isolated Warm Execution Environment

```python
# modelops/adapters/exec_env/isolated_warm.py
import base64
import time
from pathlib import Path
from typing import Dict, Optional, Any
from modelops_contracts import SimTask, SimReturn
from modelops_contracts.ports import ExecutionEnvironment, BundleRepository, CAS
from modelops.core.artifacts import bytes_to_artifact
from modelops.core.fingerprint import make_exec_fingerprint
from .warm_process import WarmProcessManager

class IsolatedWarmExecEnv(ExecutionEnvironment):
    """Production execution environment with warm process caching.
    
    Now with proper lifecycle management - no singleton tricks!
    """
    
    def __init__(
        self,
        bundle_repo: BundleRepository,
        cas: CAS,
        venvs_dir: Path,
        mem_limit_bytes: Optional[int] = None,
        max_warm_processes: int = 128
    ):
        self.bundle_repo = bundle_repo
        self.cas = cas
        self.venvs_dir = venvs_dir
        self.mem_limit_bytes = mem_limit_bytes
        
        # Create process manager - no LRU bootstrap needed!
        self._process_manager = WarmProcessManager(
            venvs_dir=venvs_dir,
            mem_limit_bytes=mem_limit_bytes,
            max_processes=max_warm_processes
        )
    
    def run(self, task: SimTask) -> SimReturn:
        """Execute task in isolated warm process."""
        # Get bundle
        digest, bundle_dir = self.bundle_repo.ensure_local(task.bundle_ref)
        
        # Get or create warm process for this digest
        process = self._process_manager.get_process(digest, bundle_dir)
        
        # Execute
        start = time.perf_counter()
        outputs_b64 = process.execute(
            entrypoint=str(task.entrypoint),
            params=dict(task.params.params),
            seed=task.seed,
            req_id=task.task_id()
        )
        exec_ms = (time.perf_counter() - start) * 1000
        
        # Convert outputs to TableArtifacts
        outputs = {}
        for name, b64_data in outputs_b64.items():
            data = base64.b64decode(b64_data)
            outputs[name] = bytes_to_artifact(name, data, self.cas)
        
        # Build return
        return SimReturn(
            task_id=task.task_id(),
            sim_root=task.sim_root(),
            outputs=outputs,
            exec_fingerprint=make_exec_fingerprint(
                digest, 
                'isolated_warm',
                self._process_manager.get_uv_lock_hash(digest)
            ),
            non_canonical=False,
            metrics={
                'executor': 'isolated_warm',
                'exec_ms': exec_ms,
                'bundle_digest': digest
            }
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

### 5.2 Warm Process Manager (Simplified)

```python
# modelops/adapters/exec_env/warm_process.py
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional
from .process import WarmProcess

class WarmProcessManager:
    """Manages warm processes with LRU eviction.
    
    The LRU cache here is ONLY for process eviction policy,
    not for bootstrap/singleton tricks.
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
        
        # LRU cache for process eviction
        self._get_process_cached = lru_cache(maxsize=max_processes)(
            self._create_process
        )
    
    def get_process(self, digest: str, bundle_dir: Path) -> WarmProcess:
        """Get or create warm process for digest."""
        process = self._get_process_cached(digest, str(bundle_dir))
        
        # Restart if dead
        if not process.is_alive():
            # Clear this entry and recreate
            # Note: In production might want smarter cache invalidation
            self._get_process_cached.cache_clear()
            process = self._get_process_cached(digest, str(bundle_dir))
        
        return process
    
    def _create_process(self, digest: str, bundle_dir_str: str) -> WarmProcess:
        """Create new warm process (called by LRU cache)."""
        bundle_dir = Path(bundle_dir_str)
        venv_dir = self._ensure_venv(digest, bundle_dir)
        
        return WarmProcess(
            python_bin=venv_dir / 'bin' / 'python',
            bundle_dir=bundle_dir,
            mem_limit_bytes=self.mem_limit_bytes
        )
    
    def _ensure_venv(self, digest: str, bundle_dir: Path) -> Path:
        """Ensure venv exists for bundle."""
        venv_dir = self.venvs_dir / digest[:8]
        if not venv_dir.exists():
            # Create venv and install requirements
            import subprocess
            subprocess.run(['python', '-m', 'venv', str(venv_dir)], check=True)
            pip_bin = venv_dir / 'bin' / 'pip'
            req_file = bundle_dir / 'requirements.txt'
            if req_file.exists():
                subprocess.run([str(pip_bin), 'install', '-r', str(req_file)], check=True)
        return venv_dir
    
    def shutdown_all(self):
        """Shutdown all warm processes."""
        # Get all cached processes
        for key in list(self._get_process_cached.cache_info()):
            try:
                process = self._get_process_cached(key)
                process.close()
            except:
                pass
        
        # Clear cache
        self._get_process_cached.cache_clear()
    
    def active_count(self) -> int:
        """Count of active processes."""
        return self._get_process_cached.cache_info().currsize
    
    def get_uv_lock_hash(self, digest: str) -> Optional[str]:
        """Get hash of uv.lock for fingerprinting."""
        # Implementation would hash the uv.lock file
        return None
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

## 7. Wire Module Integration

The wire module integration remains the same - Calabaria implements the contract.

```python
# In Calabaria package: modelops_wire.py
from functools import lru_cache
from typing import Dict, Any
from modelops_contracts import parse_entrypoint

@lru_cache(maxsize=128)
def _compile(entrypoint: str):
    """Compile scenario to wire function (cached)."""
    import_path, scenario = parse_entrypoint(entrypoint)
    
    # Import model class
    module_name, class_name = import_path.rsplit('.', 1)
    module = __import__(module_name, fromlist=[class_name])
    model_class = getattr(module, class_name)
    
    # Instantiate and compile
    model = model_class()
    return model.compile_scenario(scenario)

def execute(entrypoint: str, params: Dict[str, Any], seed: int) -> Dict[str, bytes]:
    """Wire function implementation for ModelOps.
    
    This is what the isolated worker calls.
    """
    compiled_fn = _compile(entrypoint)
    return compiled_fn(params, seed)
```

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

## 9. Engineering Tradeoffs

### 9.1 WorkerPlugin vs Manual Bootstrap

**Decision:** Use Dask's native WorkerPlugin

**Rationale:**
- **Native lifecycle:** Dask manages setup/teardown
- **Framework integration:** Works with Dask, not against it
- **Clean separation:** Plugin is composition root
- **No singletons:** No global state needed

**Tradeoff:**
- Dask-specific solution (but we're using Dask anyway)
- Must register plugin from client side

### 9.2 Single Process vs Process Pool

**Decision:** Keep single warm process per bundle (unchanged)

**Rationale:**
- **Simplicity:** No work distribution
- **Memory efficiency:** One process = 200-500MB
- **Dask parallelism:** Scale via workers, not processes

### 9.3 Configuration in Plugin Constructor

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

9. **Circuit Breaker Persistence**
   - Should circuit breaker state survive worker restarts?
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