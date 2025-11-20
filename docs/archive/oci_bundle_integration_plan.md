# OCI Bundle Integration Implementation Plan

## Overview
Enable ModelOps workers to fetch and execute bundles from OCI registries (Azure Container Registry).

## Current State (from audit)
- ✅ ModelOpsBundleRepository implemented with LocalCAS
- ✅ Worker plugin discovers modelops-bundle via entry points
- ❌ Registry URL NOT passed to workers
- ❌ Job submission hardcodes registry
- ❌ No smoke test for bundle fetching

## Implementation Plan

### Phase 1: Core Infrastructure Updates (30 min)

#### 1.1 Update DaskWorkspace to pass registry URL
**File**: `src/modelops/infra/components/workspace.py`

```python
# Around line 40, after getting infra outputs:
infra = pulumi.StackReference(infra_stack_ref)
kubeconfig = infra.require_output("kubeconfig")
registry_url = infra.require_output("acr_login_server")  # ADD THIS

# Around line 350, in worker container env:
k8s.EnvVarArgs(
    name="MODELOPS_BUNDLE_REGISTRY",
    value=registry_url  # ADD THIS
)
```

#### 1.2 Fix job submission to use dynamic registry
**File**: `src/modelops/client/job_submission.py`

Replace hardcoded registry (line 284) with:
```python
# Get registry from BundleEnvironment or env
def get_registry_url(env: str = "dev") -> str:
    # Try environment variable first (set by workspace)
    registry = os.environ.get("MODELOPS_BUNDLE_REGISTRY")
    if registry:
        return registry

    # Try BundleEnvironment file
    bundle_env_path = Path.home() / ".modelops" / "bundle-env" / f"{env}.yaml"
    if bundle_env_path.exists():
        with open(bundle_env_path) as f:
            bundle_env = yaml.safe_load(f)
            return bundle_env.get("registry", {}).get("login_server")

    raise ValueError("No registry URL found")

# Use in create_kubernetes_job:
registry = get_registry_url(env)
image = f"{registry}/modelops/dask-worker:latest"
```

### Phase 2: Dev CLI Command (45 min)

#### 2.1 Create dev CLI module
**File**: `src/modelops/cli/dev.py`

```python
"""Developer tools and testing utilities."""

import typer
import json
import tempfile
import subprocess
from pathlib import Path
from typing import Optional
from dask.distributed import Client
from modelops_contracts import SimTask
from ..services.dask_simulation import DaskSimulationService
from ..worker.config import RuntimeConfig
from .display import console, success, error, info

app = typer.Typer(help="🧪 Developer tools and testing utilities")

def create_test_bundle() -> Path:
    """Create minimal test bundle for smoke testing."""
    bundle_dir = Path(tempfile.mkdtemp()) / "smoke-test"
    bundle_dir.mkdir()

    # Minimal simulation
    (bundle_dir / "simulate.py").write_text("""
def simulate(params, seed):
    return {"status": "success", "seed": seed, "params": params, "test": True}
""")

    # Empty requirements
    (bundle_dir / "requirements.txt").write_text("")

    # Manifest
    manifest = {
        "name": "smoke-test",
        "version": "1.0.0",
        "entrypoint": "simulate:simulate"
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

    return bundle_dir

def push_bundle(bundle_dir: Path, registry: str) -> str:
    """Push bundle to registry and return digest."""
    result = subprocess.run([
        "modelops-bundle", "push",
        "--source", str(bundle_dir),
        "--registry", registry,
        "--tag", "smoke-test"
    ], capture_output=True, text=True)

    # Extract digest from output
    for line in result.stdout.split("\n"):
        if "sha256:" in line:
            import re
            match = re.search(r"sha256:[a-f0-9]{64}", line)
            if match:
                return match.group(0)

    raise ValueError(f"Failed to push: {result.stderr}")

@app.command()
def smoke_test(
    bundle_path: Optional[Path] = typer.Option(None, "--bundle", "-b"),
    registry: Optional[str] = typer.Option(None, "--registry", "-r"),
    env: str = typer.Option("dev", "--env", "-e"),
    timeout: int = typer.Option(30, "--timeout", "-t")
):
    """Run smoke test for OCI bundle fetching."""
    info("🧪 Starting OCI bundle smoke test...")

    try:
        # 1. Create or use bundle
        if bundle_path:
            info(f"Using provided bundle: {bundle_path}")
            bundle_dir = bundle_path
        else:
            info("Creating test bundle...")
            bundle_dir = create_test_bundle()

        # 2. Get registry
        if not registry:
            from ..client.job_submission import get_registry_url
            registry = get_registry_url(env)
        info(f"Using registry: {registry}")

        # 3. Push bundle
        info("Pushing bundle to registry...")
        bundle_ref = push_bundle(bundle_dir, registry)
        success(f"Pushed bundle: {bundle_ref[:20]}...")

        # 4. Connect to Dask
        scheduler_url = os.environ.get("DASK_SCHEDULER", "tcp://localhost:8786")
        info(f"Connecting to Dask: {scheduler_url}")
        client = Client(scheduler_url)

        # 5. Create SimulationService
        config = RuntimeConfig.from_env()
        sim_service = DaskSimulationService(client, config)

        # 6. Submit task
        info("Submitting simulation task...")
        task = SimTask(
            fn_ref="simulate:simulate",
            params={"test": "smoke"},
            seed=12345,
            bundle_ref=bundle_ref
        )
        future = sim_service.submit(task)

        # 7. Get result
        result = future.result(timeout=timeout)
        success(f"Task completed: {result}")

        success("✅ Smoke test PASSED!")
        return True

    except Exception as e:
        error(f"❌ Smoke test FAILED: {e}")
        return False
    finally:
        if 'client' in locals():
            client.close()

@app.command()
def test_connection(
    scheduler: Optional[str] = typer.Option(None, "--scheduler", "-s")
):
    """Test connection to Dask cluster."""
    scheduler_url = scheduler or os.environ.get("DASK_SCHEDULER", "tcp://localhost:8786")
    info(f"Testing connection to {scheduler_url}...")

    try:
        client = Client(scheduler_url, timeout='5s')
        n_workers = len(client.scheduler_info()['workers'])
        success(f"✅ Connected! Found {n_workers} workers")
        client.close()
    except Exception as e:
        error(f"❌ Connection failed: {e}")

@app.command()
def validate_bundle(
    path: Path = typer.Argument(..., help="Path to bundle directory")
):
    """Validate bundle structure."""
    info(f"Validating bundle at {path}...")

    errors = []

    # Check manifest
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        errors.append("Missing manifest.json")
    else:
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
                if "entrypoint" not in manifest:
                    errors.append("Manifest missing entrypoint")
        except Exception as e:
            errors.append(f"Invalid manifest: {e}")

    # Check entrypoint exists
    if manifest_path.exists():
        entrypoint = manifest.get("entrypoint", "")
        if ":" in entrypoint:
            module, _ = entrypoint.split(":", 1)
            module_file = path / f"{module.replace('.', '/')}.py"
            if not module_file.exists():
                errors.append(f"Entrypoint module not found: {module_file}")

    # Check requirements
    req_path = path / "requirements.txt"
    if not req_path.exists():
        info("Note: No requirements.txt (optional)")

    if errors:
        for err in errors:
            error(f"  ❌ {err}")
        error("Bundle validation FAILED")
    else:
        success("✅ Bundle validation PASSED")
```

#### 2.2 Wire into main CLI
**File**: `src/modelops/cli/main.py`

Add after line 22:
```python
from . import dev
```

Add after line 79:
```python
# Developer tools
app.add_typer(
    dev.app,
    name="dev",
    help="🧪 Developer tools and testing utilities"
)
```

### Phase 3: Testing & Validation (30 min)

#### 3.1 Test locally with mock registry
```bash
# Start local registry
docker run -d -p 5000:5000 --name registry registry:2

# Set insecure mode
export MODELOPS_BUNDLE_INSECURE=true
export MODELOPS_BUNDLE_REGISTRY=localhost:5000

# Run smoke test
mops dev smoke-test --registry localhost:5000
```

#### 3.2 Test on Azure infrastructure
```bash
# Ensure infra and workspace are up
mops infra status
mops workspace status

# Run smoke test (will use ACR)
mops dev smoke-test

# Test specific bundle
mops dev validate-bundle examples/epi_model
mops dev smoke-test --bundle examples/epi_model
```

## Success Criteria

1. ✅ Workers receive MODELOPS_BUNDLE_REGISTRY environment variable
2. ✅ Job submission uses dynamic registry lookup
3. ✅ Smoke test command successfully:
   - Creates/pushes test bundle to ACR
   - Submits simulation via DaskSimulationService
   - Worker fetches bundle from ACR
   - Simulation executes and returns result
4. ✅ Dev CLI provides quick validation tools

## Files to Modify

1. `src/modelops/infra/components/workspace.py` - Add registry env var
2. `src/modelops/client/job_submission.py` - Dynamic registry lookup
3. `src/modelops/cli/dev.py` - NEW: Dev tools CLI
4. `src/modelops/cli/main.py` - Register dev subcommand

## Estimated Time: 2 hours

- Phase 1: 30 minutes
- Phase 2: 45 minutes
- Phase 3: 30 minutes
- Buffer: 15 minutes

## Next Steps After Implementation

1. Update worker Docker image to include modelops-bundle
2. Add CI/CD pipeline with smoke test
3. Document bundle creation process
4. Add more dev tools as needed (logs, debug, profile)