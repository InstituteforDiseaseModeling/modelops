"""Developer tools and testing utilities."""

import typer
import json
import tempfile
import subprocess
import os
import sys
from pathlib import Path
from typing import Optional
from dask.distributed import Client
from modelops_contracts import SimTask
from ..services.dask_simulation import DaskSimulationService
from ..worker.config import RuntimeConfig
from ..core import automation
from .display import console, success, error, info, warning
from ..images import get_image_config

app = typer.Typer(help="🧪 Developer tools and testing utilities")


def get_test_bundle() -> Path:
    """Get test bundle for smoke testing.

    Returns:
        Path to test bundle directory

    Uses the permanent test fixture if available, otherwise creates temporary.
    """
    # First try to use the permanent test fixture
    fixture_path = Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "smoke_bundle"
    if fixture_path.exists():
        return fixture_path

    # Fall back to creating temporary bundle
    bundle_dir = Path(tempfile.mkdtemp()) / "smoke-test"
    bundle_dir.mkdir()

    # Minimal simulation function
    (bundle_dir / "simulate.py").write_text("""
def simulate(params, seed):
    '''Minimal simulation for smoke testing.'''
    return {
        "status": "success",
        "seed": seed,
        "params": params,
        "test": True,
        "message": "OCI bundle fetch successful!"
    }
""")

    # Empty requirements (no additional dependencies needed)
    (bundle_dir / "requirements.txt").write_text("")

    # Bundle manifest
    manifest = {
        "name": "smoke-test",
        "version": "1.0.0",
        "entrypoint": "simulate:simulate"
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    return bundle_dir


def push_bundle(bundle_dir: Path, registry: str) -> str:
    """Push bundle to registry and return digest.

    Args:
        bundle_dir: Directory containing bundle to push
        registry: Registry URL to push to

    Returns:
        Bundle reference (sha256:...)

    Raises:
        ValueError: If push fails
    """
    # modelops-bundle needs to be initialized first
    # Set registry via environment variable
    env = os.environ.copy()
    env["MODELOPS_BUNDLE_REGISTRY"] = registry

    # Use modelops-bundle's Python directly
    bundle_python = "/Users/vsb/projects/work/modelops-bundle/.venv/bin/python"

    # Check if already initialized (has .modelops-bundle directory)
    if not (bundle_dir / ".modelops-bundle").exists():
        # Initialize the bundle directory
        init_result = subprocess.run([
            bundle_python, "-m", "modelops_bundle.cli", "init"
        ], capture_output=True, text=True, cwd=str(bundle_dir), env=env)

        if init_result.returncode != 0:
            raise ValueError(f"Failed to initialize bundle: {init_result.stderr}")

    # Add all files to tracking
    # modelops-bundle needs files to be explicitly added
    add_result = subprocess.run([
        bundle_python, "-m", "modelops_bundle.cli", "add", "."
    ], capture_output=True, text=True, cwd=str(bundle_dir), env=env)

    if add_result.returncode != 0:
        raise ValueError(f"Failed to add files: {add_result.stderr}")

    # Run push from the bundle directory
    result = subprocess.run([
        bundle_python, "-m", "modelops_bundle.cli", "push",
        "--tag", "smoke-test"
    ], capture_output=True, text=True, cwd=str(bundle_dir), env=env)

    # Check if push succeeded (even if nothing changed)
    if result.returncode != 0:
        raise ValueError(f"Failed to push bundle: {result.stderr}")

    # Extract digest from output if present
    import re
    for line in result.stdout.split("\n"):
        if "sha256:" in line:
            match = re.search(r"sha256:[a-f0-9]{64}", line)
            if match:
                return match.group(0)

    # If no digest in push output (e.g., everything up to date),
    # get it from manifest command
    manifest_result = subprocess.run([
        bundle_python, "-m", "modelops_bundle.cli", "manifest",
        "smoke-test", "--full"
    ], capture_output=True, text=True, cwd=str(bundle_dir), env=env)

    if manifest_result.returncode == 0:
        for line in manifest_result.stdout.split("\n"):
            if line.startswith("Digest:"):
                match = re.search(r"sha256:[a-f0-9]{64}", line)
                if match:
                    return match.group(0)

    # If we still couldn't find digest, show error
    error_msg = f"Push output: {result.stdout}\nManifest output: {manifest_result.stdout}"
    raise ValueError(f"Failed to get bundle digest: {error_msg}")


def get_registry_url(env: str) -> str:
    """Get registry URL from various sources.

    Args:
        env: Environment name

    Returns:
        Registry URL

    Raises:
        ValueError: If registry URL not found
    """
    # Try environment variable first
    registry = os.environ.get("MODELOPS_BUNDLE_REGISTRY")
    if registry:
        return registry

    # Try BundleEnvironment file
    bundle_env_path = Path.home() / ".modelops" / "bundle-env" / f"{env}.yaml"
    if bundle_env_path.exists():
        import yaml
        with open(bundle_env_path) as f:
            bundle_env = yaml.safe_load(f)
            registry = bundle_env.get("registry", {}).get("login_server")
            if registry:
                return registry

    # Try Pulumi infrastructure stack
    try:
        outputs = automation.outputs("infra", env, refresh=False)
        if outputs and "acr_login_server" in outputs:
            registry = automation.get_output_value(outputs, "acr_login_server")
            if registry:
                return registry
    except Exception:
        pass

    raise ValueError(
        "No registry URL found. Please ensure infrastructure is deployed "
        "or set MODELOPS_BUNDLE_REGISTRY environment variable."
    )


@app.command()
def smoke_test(
    bundle_path: Optional[Path] = typer.Option(
        None,
        "--bundle", "-b",
        help="Path to test bundle directory (creates minimal if not provided)"
    ),
    registry: Optional[str] = typer.Option(
        None,
        "--registry", "-r",
        help="Registry URL (uses environment/stack if not provided)"
    ),
    env: str = typer.Option(
        "dev",
        "--env", "-e",
        help="Environment name"
    ),
    scheduler: Optional[str] = typer.Option(
        None,
        "--scheduler", "-s",
        help="Dask scheduler URL (uses environment if not provided)"
    ),
    timeout: int = typer.Option(
        30,
        "--timeout", "-t",
        help="Timeout in seconds"
    ),
    skip_port_forward: bool = typer.Option(
        False,
        "--skip-port-forward",
        help="Skip automatic port-forwarding (if already set up)"
    )
):
    """Run smoke test for OCI bundle fetching on workers.

    This command:
    1. Creates/uses a minimal test bundle
    2. Pushes it to the registry
    3. Sets up port-forwarding if needed
    4. Submits a simulation via DaskSimulationService
    5. Verifies the worker can fetch and execute the bundle

    Example:
        mops dev smoke-test
        mops dev smoke-test --bundle ./my-test-bundle
        mops dev smoke-test --registry localhost:5000
    """
    info("🧪 Starting OCI bundle smoke test...")

    client = None
    port_forward_proc = None
    try:
        # 1. Create or use bundle
        if bundle_path:
            info(f"Using provided bundle: {bundle_path}")
            bundle_dir = bundle_path
            if not bundle_dir.exists():
                error(f"Bundle directory not found: {bundle_dir}")
                raise typer.Exit(1)
        else:
            info("Creating test bundle...")
            bundle_dir = get_test_bundle()
            success(f"Created test bundle at: {bundle_dir}")

        # 2. Get registry
        if not registry:
            try:
                registry = get_registry_url(env)
            except ValueError as e:
                error(str(e))
                raise typer.Exit(1)
        info(f"Using registry: {registry}")

        # 3. Push bundle
        info("Pushing bundle to registry...")
        try:
            digest = push_bundle(bundle_dir, registry)
            # Prepend repository name for the bundle_ref
            bundle_ref = f"smoke_bundle@{digest}"
            success(f"Pushed bundle: {bundle_ref}")
        except ValueError as e:
            error(f"Failed to push bundle: {e}")
            raise typer.Exit(1)

        # 4. Set up port-forwarding if needed
        scheduler_url = scheduler or os.environ.get("DASK_SCHEDULER", "tcp://localhost:8786")

        if not skip_port_forward and scheduler_url.startswith("tcp://localhost:"):
            # Kill any existing port-forward processes
            info("Cleaning up any existing port-forward processes...")
            subprocess.run(
                ["pkill", "-f", "kubectl port-forward.*dask-scheduler"],
                capture_output=True,
                check=False
            )

            # Wait for Dask deployment to be ready
            namespace = f"modelops-dask-{env}"
            info(f"Checking Dask deployment status in namespace {namespace}...")

            # Wait for deployment to be ready
            max_wait = 60  # seconds
            for i in range(max_wait // 5):
                result = subprocess.run(
                    ["kubectl", "get", "deployment", "dask-scheduler", "-n", namespace, "-o", "jsonpath={.status.readyReplicas}"],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0 and result.stdout.strip() == "1":
                    success("Dask scheduler deployment is ready")
                    break
                if i == 0:
                    info("Waiting for Dask scheduler deployment to be ready...")
                import time
                time.sleep(5)
            else:
                error("Dask scheduler deployment not ready after 60 seconds")
                raise typer.Exit(1)

            # Start port-forward
            info("Starting kubectl port-forward...")
            port_forward_proc = subprocess.Popen(
                ["kubectl", "port-forward", "-n", namespace, "svc/dask-scheduler", "8786:8786"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Wait a bit for port-forward to establish
            import time
            for i in range(10):
                time.sleep(1)
                # Check if port is listening
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                try:
                    result = sock.connect_ex(('localhost', 8786))
                    sock.close()
                    if result == 0:
                        success("Port-forward established successfully")
                        break
                except:
                    pass
            else:
                error("Port-forward failed to establish after 10 seconds")
                if port_forward_proc:
                    port_forward_proc.terminate()
                raise typer.Exit(1)

        # 5. Connect to Dask with retries
        info(f"Connecting to Dask: {scheduler_url}")
        client_connected = False
        for attempt in range(3):
            try:
                client = Client(scheduler_url, timeout='10s')
                n_workers = len(client.scheduler_info()['workers'])
                if n_workers == 0:
                    if attempt < 2:
                        warning(f"No workers available (attempt {attempt+1}/3), retrying...")
                        import time
                        time.sleep(5)
                        continue
                    else:
                        error("No workers available in Dask cluster after 3 attempts")
                        raise typer.Exit(1)
                success(f"Connected to Dask cluster with {n_workers} workers")
                client_connected = True
                break
            except Exception as e:
                if attempt < 2:
                    warning(f"Connection failed (attempt {attempt+1}/3): {e}, retrying...")
                    import time
                    time.sleep(5)
                else:
                    error(f"Failed to connect to Dask after 3 attempts: {e}")
                    raise typer.Exit(1)

        if not client_connected:
            error("Could not connect to Dask cluster")
            raise typer.Exit(1)

        # 6. Create SimulationService
        info("Initializing simulation service...")
        # Set the registry in environment for RuntimeConfig
        os.environ["MODELOPS_BUNDLE_REGISTRY"] = registry
        config = RuntimeConfig.from_env()
        sim_service = DaskSimulationService(client, config)

        # 7. Submit task
        info("Submitting simulation task...")
        from modelops_contracts import UniqueParameterSet
        import time
        # Add timestamp to force new task (bypass cache)
        param_set = UniqueParameterSet.from_dict({
            "test": "smoke",
            "message": "Testing OCI bundle fetch",
            "timestamp": int(time.time())
        })
        task = SimTask(
            entrypoint="simulate.simulate/smoke",  # module.function/scenario format
            params=param_set,
            seed=12345,
            bundle_ref=bundle_ref
        )
        future = sim_service.submit(task)

        # 8. Get result
        info(f"Waiting for result (timeout: {timeout}s)...")
        result = future.result(timeout=timeout)

        # Display result
        success(f"Task completed successfully!")
        info("Result:")
        # Convert SimReturn to dict for display
        if hasattr(result, '__dict__'):
            result_dict = result.__dict__
        else:
            result_dict = result
        console.print(json.dumps(result_dict, indent=2, default=str))

        # Verify result structure - check for SimReturn with outputs
        if hasattr(result, 'outputs') and result.outputs:
            # SimReturn format - check if we have the expected outputs
            if 'result' in result.outputs and 'metadata' in result.outputs:
                success("✅ Smoke test PASSED! Workers can fetch and execute OCI bundles.")
            else:
                warning(f"Missing expected outputs in SimReturn: {list(result.outputs.keys())}")
                error("❌ Smoke test FAILED! Result doesn't contain expected outputs.")
                raise typer.Exit(1)
        elif isinstance(result, dict) and result.get("status") == "completed":
            # Legacy dict format
            success("✅ Smoke test PASSED! Workers can fetch and execute OCI bundles.")
        else:
            warning(f"Unexpected result format: {result}")
            error("❌ Smoke test FAILED! Result doesn't match expected format.")
            raise typer.Exit(1)

    except Exception as e:
        error(f"❌ Smoke test FAILED: {e}")
        raise typer.Exit(1)
    finally:
        # Clean up client connection
        if client:
            client.close()

        # Clean up port-forward process
        if port_forward_proc:
            info("Cleaning up port-forward process...")
            port_forward_proc.terminate()
            try:
                port_forward_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                port_forward_proc.kill()
                port_forward_proc.wait()


@app.command()
def test_connection(
    scheduler: Optional[str] = typer.Option(
        None,
        "--scheduler", "-s",
        help="Dask scheduler URL (uses environment if not provided)"
    ),
    timeout: int = typer.Option(
        5,
        "--timeout", "-t",
        help="Connection timeout in seconds"
    )
):
    """Test connection to Dask cluster.

    Example:
        mops dev test-connection
        mops dev test-connection --scheduler tcp://10.0.0.4:8786
    """
    scheduler_url = scheduler or os.environ.get("DASK_SCHEDULER", "tcp://localhost:8786")
    info(f"Testing connection to {scheduler_url}...")

    try:
        client = Client(scheduler_url, timeout=f'{timeout}s')
        scheduler_info = client.scheduler_info()
        n_workers = len(scheduler_info.get('workers', {}))

        success(f"✅ Connected successfully!")
        info(f"Scheduler: {scheduler_url}")
        info(f"Workers: {n_workers}")

        # Show worker details
        if n_workers > 0:
            info("\nWorker details:")
            for worker_id, worker_info in scheduler_info['workers'].items():
                worker_name = worker_info.get('name', worker_id)
                worker_threads = worker_info.get('nthreads', 0)
                worker_memory = worker_info.get('memory_limit', 0) / (1024**3)  # Convert to GB
                console.print(f"  • {worker_name}: {worker_threads} threads, {worker_memory:.1f} GB memory")

        client.close()

    except Exception as e:
        error(f"❌ Connection failed: {e}")
        raise typer.Exit(1)


@app.command()
def diagnose_auth():
    """Diagnose bundle authentication setup.

    Checks that workers have proper environment variables and can authenticate
    to the container registry and storage.
    """
    import subprocess

    section("Bundle Authentication Diagnostics")

    # Get a worker pod
    result = subprocess.run(
        ["kubectl", "get", "pod", "-n", "modelops-dask-dev",
         "-l", "app=dask-worker", "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True
    )

    if result.returncode != 0 or not result.stdout.strip():
        error("No worker pods found. Is the workspace running?")
        info("Run: mops workspace status")
        raise typer.Exit(1)

    pod = result.stdout.strip()
    info(f"Checking pod: {pod}")

    # Check environment variables
    info("\nEnvironment variables:")
    result = subprocess.run(
        ["kubectl", "exec", pod, "-n", "modelops-dask-dev", "--", "env"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        error(f"Failed to check environment: {result.stderr}")
        raise typer.Exit(1)

    env_vars = result.stdout
    has_registry = "MODELOPS_BUNDLE_REGISTRY" in env_vars
    has_username = "REGISTRY_USERNAME" in env_vars
    has_password = "REGISTRY_PASSWORD" in env_vars
    has_storage = "AZURE_STORAGE_CONNECTION_STRING" in env_vars

    # Show status
    if has_registry:
        # Extract the actual value
        for line in env_vars.split('\n'):
            if line.startswith("MODELOPS_BUNDLE_REGISTRY="):
                registry_val = line.split('=', 1)[1]
                success(f"  MODELOPS_BUNDLE_REGISTRY: {registry_val}")
                break
    else:
        error("  MODELOPS_BUNDLE_REGISTRY: ✗ (missing)")

    if has_username:
        success("  REGISTRY_USERNAME: ✓ (set)")
    else:
        error("  REGISTRY_USERNAME: ✗ (missing)")

    if has_password:
        success("  REGISTRY_PASSWORD: ✓ (set)")
    else:
        error("  REGISTRY_PASSWORD: ✗ (missing)")

    if has_storage:
        success("  AZURE_STORAGE_CONNECTION_STRING: ✓ (set)")
    else:
        warning("  AZURE_STORAGE_CONNECTION_STRING: ✗ (missing - OK if not using blob storage)")

    # Test ACR authentication if all vars present
    if all([has_registry, has_username, has_password]):
        info("\nTesting ACR authentication...")
        test_script = '''
import os, urllib.request, base64, json

registry = os.environ.get("MODELOPS_BUNDLE_REGISTRY", "").split("/")[0]
username = os.environ["REGISTRY_USERNAME"]
password = os.environ["REGISTRY_PASSWORD"]

if not registry:
    print("✗ No registry URL found")
    exit(1)

url = f"https://{registry}/v2/"
auth = base64.b64encode(f"{username}:{password}".encode()).decode()
req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})

try:
    with urllib.request.urlopen(req) as resp:
        print(f"✓ Auth successful! Status: {resp.status}")
        # Try to check if we can list repositories
        catalog_url = f"https://{registry}/v2/_catalog"
        catalog_req = urllib.request.Request(catalog_url, headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(catalog_req) as catalog_resp:
                data = json.loads(catalog_resp.read())
                repos = data.get("repositories", [])
                if repos:
                    print(f"✓ Can list repositories. Found {len(repos)} repos")
                else:
                    print("⚠ No repositories found in registry")
        except Exception as e:
            print(f"⚠ Cannot list repositories: {e}")
except urllib.error.HTTPError as e:
    print(f"✗ Auth failed: HTTP {e.code} - {e.reason}")
    if e.code == 401:
        print("  Check that ACR admin user is enabled or token is valid")
    elif e.code == 404:
        print("  Check that the registry URL is correct")
except Exception as e:
    print(f"✗ Auth failed: {e}")
'''
        result = subprocess.run(
            ["kubectl", "exec", pod, "-n", "modelops-dask-dev", "--",
             "python", "-c", test_script],
            capture_output=True, text=True
        )

        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if line.strip():
                    if line.startswith('✓'):
                        success(f"  {line}")
                    elif line.startswith('✗'):
                        error(f"  {line}")
                    elif line.startswith('⚠'):
                        warning(f"  {line}")
                    else:
                        info(f"    {line}")
        else:
            error(f"  Test failed: {result.stderr}")
    else:
        warning("\nSkipping ACR auth test - missing required environment variables")
        info("This usually means the bundle-credentials secret is not mounted")
        info("Check: kubectl describe deployment dask-workers -n modelops-dask-dev | grep envFrom")

    # Summary
    info("\nSummary:")
    if all([has_registry, has_username, has_password]):
        success("✓ Bundle authentication is properly configured")
        info("Workers should be able to pull bundles from the registry")
    else:
        error("✗ Bundle authentication is not properly configured")
        info("Run: mops infra up --component registry storage workspace")
        info("This should create and mount the bundle-credentials secret automatically")


@app.command()
def validate_bundle(
    path: Path = typer.Argument(..., help="Path to bundle directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed validation")
):
    """Validate a bundle directory structure.

    Checks:
    - manifest.json exists and is valid
    - Entrypoint module exists
    - requirements.txt format (if present)

    Example:
        mops dev validate-bundle ./my-bundle
        mops dev validate-bundle ./examples/epi_model --verbose
    """
    info(f"Validating bundle at {path}...")

    errors = []
    warnings = []
    manifest = None

    # Check directory exists
    if not path.exists():
        error(f"Directory not found: {path}")
        raise typer.Exit(1)

    # Check manifest
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        errors.append("Missing manifest.json")
    else:
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
                if verbose:
                    info("Manifest content:")
                    console.print(json.dumps(manifest, indent=2))

                # Check required fields
                if "name" not in manifest:
                    errors.append("Manifest missing 'name' field")
                if "entrypoint" not in manifest:
                    errors.append("Manifest missing 'entrypoint' field")
                if "version" not in manifest:
                    warnings.append("Manifest missing 'version' field (optional but recommended)")

        except json.JSONDecodeError as e:
            errors.append(f"Invalid manifest JSON: {e}")
        except Exception as e:
            errors.append(f"Error reading manifest: {e}")

    # Check entrypoint exists
    if manifest and "entrypoint" in manifest:
        entrypoint = manifest["entrypoint"]
        if ":" in entrypoint:
            module_path, function = entrypoint.split(":", 1)
            module_file = path / f"{module_path.replace('.', '/')}.py"
            if not module_file.exists():
                errors.append(f"Entrypoint module not found: {module_file}")
            elif verbose:
                # Try to import and check function exists
                try:
                    import ast
                    with open(module_file) as f:
                        tree = ast.parse(f.read())
                        functions = [node.name for node in ast.walk(tree)
                                   if isinstance(node, ast.FunctionDef)]
                        if function not in functions:
                            errors.append(f"Function '{function}' not found in {module_file}")
                        else:
                            success(f"✓ Entrypoint function '{function}' found")
                except Exception as e:
                    warnings.append(f"Could not parse module for validation: {e}")
        else:
            errors.append(f"Invalid entrypoint format (expected module:function): {entrypoint}")

    # Check requirements.txt
    req_path = path / "requirements.txt"
    if req_path.exists():
        try:
            with open(req_path) as f:
                lines = f.readlines()
                if verbose:
                    info(f"Found {len(lines)} requirements")
        except Exception as e:
            warnings.append(f"Could not read requirements.txt: {e}")
    else:
        if verbose:
            info("No requirements.txt (optional)")

    # Report results
    if errors:
        error("❌ Bundle validation FAILED")
        for err in errors:
            console.print(f"  [red]✗[/red] {err}")
    else:
        success("✅ Bundle validation PASSED")

    if warnings:
        warning("⚠️  Warnings:")
        for warn in warnings:
            console.print(f"  [yellow]![/yellow] {warn}")

    if errors:
        raise typer.Exit(1)


@app.command()
def check_credentials(
    env: str = typer.Option("dev", "--env", "-e", help="Environment"),
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help="K8s namespace")
):
    """Check if worker pods have all required credentials.

    Verifies that workers have the necessary environment variables
    for bundle fetching and storage operations.

    Example:
        mops dev check-credentials
        mops dev check-credentials --env prod
    """
    if not namespace:
        namespace = f"modelops-dask-{env}"

    info(f"Checking credentials in namespace: {namespace}")

    # Required environment variables to check
    required_vars = [
        ("REGISTRY_USERNAME", "ACR username for bundle pulls"),
        ("REGISTRY_PASSWORD", "ACR password for bundle pulls"),
        ("AZURE_STORAGE_CONNECTION_STRING", "Azure blob storage access"),
        ("AZURE_STORAGE_ACCOUNT", "Azure storage account name"),
        ("MODELOPS_BUNDLE_REGISTRY", "Registry URL for bundles")
    ]

    # Optional but useful variables
    optional_vars = [
        ("MODELOPS_BUNDLE_INSECURE", "Allow insecure registry (dev only)"),
        ("MODELOPS_STORAGE_BACKEND", "Storage backend type (local/azure)"),
        ("GITHUB_TOKEN", "GitHub PAT for private repo access"),
        ("GIT_USERNAME", "Git username for HTTPS operations"),
        ("GIT_PASSWORD", "Git password for HTTPS operations")
    ]

    info("\n✓ = Set, ✗ = Missing\n")

    missing_required = []

    # Check required variables
    console.print("[bold]Required Variables:[/bold]")
    for var_name, description in required_vars:
        result = subprocess.run(
            ["kubectl", "-n", namespace, "exec", "deployment/dask-workers",
             "--", "sh", "-c", f"echo ${{{var_name}}}"],
            capture_output=True, text=True
        )

        if result.returncode == 0 and result.stdout.strip():
            # Variable is set
            if "PASSWORD" in var_name or "CONNECTION_STRING" in var_name:
                # Don't show sensitive values
                console.print(f"  [green]✓[/green] {var_name}: [dim]{description}[/dim]")
            else:
                # Show non-sensitive values
                value = result.stdout.strip()
                console.print(f"  [green]✓[/green] {var_name}: {value} [dim]({description})[/dim]")
        else:
            # Variable is missing
            console.print(f"  [red]✗[/red] {var_name}: [red]Missing[/red] [dim]({description})[/dim]")
            missing_required.append(var_name)

    # Check optional variables
    console.print("\n[bold]Optional Variables:[/bold]")
    for var_name, description in optional_vars:
        result = subprocess.run(
            ["kubectl", "-n", namespace, "exec", "deployment/dask-workers",
             "--", "sh", "-c", f"echo ${{{var_name}}}"],
            capture_output=True, text=True
        )

        if result.returncode == 0 and result.stdout.strip():
            value = result.stdout.strip()
            console.print(f"  [green]✓[/green] {var_name}: {value} [dim]({description})[/dim]")
        else:
            console.print(f"  [dim]✗ {var_name}: Not set ({description})[/dim]")

    # Summary
    console.print("\n" + "─" * 60)
    if missing_required:
        error(f"Missing {len(missing_required)} required credential(s)")
        info("\nTo fix, ensure these secrets are mounted:")
        info("  • bundle-credentials (for registry access)")
        info("  • modelops-storage (for blob storage)")
        info("\nCheck deployment: kubectl describe deployment dask-workers -n " + namespace)
        raise typer.Exit(1)
    else:
        success("All required credentials are present!")


@app.command()
def test_bundle_fetch(
    bundle_ref: str = typer.Argument(..., help="Bundle reference to test (e.g., sha256:abc123...)"),
    env: str = typer.Option("dev", "--env", "-e", help="Environment"),
    namespace: Optional[str] = typer.Option(None, "--namespace", "-n", help="K8s namespace")
):
    """Test if workers can fetch a bundle from the registry.

    This command runs a test script inside a worker pod to verify
    that bundle fetching works with the current credentials.

    Example:
        mops dev test-bundle-fetch sha256:a7671b13481871066dde8a541dcbca5781fd5eb3234d43df7cae96ffe8147965
        mops dev test-bundle-fetch simulation-workflow@sha256:abc123...
    """
    if not namespace:
        namespace = f"modelops-dask-{env}"

    info(f"Testing bundle fetch in namespace: {namespace}")
    info(f"Bundle reference: {bundle_ref}")

    # Create a Python test script to run in the worker
    test_script = f'''
import os
import sys
import traceback

# Check if we have the registry configured
registry = os.getenv("MODELOPS_BUNDLE_REGISTRY")
if not registry:
    print("✗ MODELOPS_BUNDLE_REGISTRY not set", file=sys.stderr)
    sys.exit(1)

print(f"Registry: {{registry}}")
print(f"Testing bundle: {bundle_ref}")

# Check if we have credentials
has_username = bool(os.getenv("REGISTRY_USERNAME"))
has_password = bool(os.getenv("REGISTRY_PASSWORD"))
print(f"Credentials: username={{has_username}}, password={{has_password}}")

try:
    # Import the bundle repository
    from modelops_bundle.repository import ModelOpsBundleRepository

    # Create repository instance
    repo = ModelOpsBundleRepository(
        registry_ref=registry,
        cache_dir="/tmp/modelops/bundles",
        insecure=os.getenv("MODELOPS_BUNDLE_INSECURE", "false").lower() == "true"
    )

    print("\\nAttempting to fetch bundle...")
    result = repo.ensure_local("{bundle_ref}")

    # Handle both tuple (digest, path) and plain path returns
    if isinstance(result, tuple):
        digest, local_path = result
        print(f"✓ Bundle fetched successfully!")
        print(f"  Digest: {{digest}}")
        print(f"  Local path: {{local_path}}")
    else:
        local_path = result
        print(f"✓ Bundle fetched successfully!")
        print(f"  Local path: {{local_path}}")

    # Check if it's a directory
    import os.path
    if os.path.isdir(local_path):
        # List contents
        import os
        contents = os.listdir(local_path)
        print(f"  Contents: {{', '.join(contents[:5])}}")
        if len(contents) > 5:
            print(f"  ... and {{len(contents)-5}} more files")

except ImportError as e:
    print(f"✗ Import error: {{e}}", file=sys.stderr)
    print("  modelops-bundle might not be installed in the worker image", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"✗ Bundle fetch failed: {{e}}", file=sys.stderr)
    print("\\nFull traceback:", file=sys.stderr)
    traceback.print_exc()

    # Check if it's an auth error
    if "401" in str(e) or "Unauthorized" in str(e):
        print("\\nThis appears to be an authentication issue.", file=sys.stderr)
        print("Check that ACR credentials are correct and have pull permissions.", file=sys.stderr)
    elif "404" in str(e) or "Not found" in str(e):
        print("\\nBundle not found in registry.", file=sys.stderr)
        print("Check that the bundle has been pushed to the registry.", file=sys.stderr)
    elif "JSONDecodeError" in str(e) or "Expecting value" in str(e):
        print("\\nReceived non-JSON response (likely an HTML error page).", file=sys.stderr)
        print("This usually indicates an authentication or configuration issue.", file=sys.stderr)

    sys.exit(1)
'''

    # Run the test script in a worker pod
    info("\nRunning test script in worker pod...")
    result = subprocess.run(
        ["kubectl", "-n", namespace, "exec", "deployment/dask-workers",
         "--", "python", "-c", test_script],
        capture_output=True, text=True
    )

    # Display output
    if result.stdout:
        for line in result.stdout.split('\n'):
            if line.strip():
                if '✓' in line:
                    success(line)
                elif '✗' in line:
                    error(line)
                else:
                    console.print(line)

    if result.stderr:
        for line in result.stderr.split('\n'):
            if line.strip():
                error(line)

    # Exit with appropriate code
    if result.returncode != 0:
        console.print("\n" + "─" * 60)
        error("Bundle fetch test failed!")
        info("\nTroubleshooting steps:")
        info("  1. Check credentials: mops dev check-credentials")
        info("  2. Verify bundle exists in registry")
        info("  3. Check worker logs: kubectl logs -n " + namespace + " -l app=dask-worker")
        raise typer.Exit(1)
    else:
        console.print("\n" + "─" * 60)
        success("Bundle fetch test passed! Workers can pull bundles from the registry.")


@app.command()
def quick_sim(
    params: str = typer.Argument(..., help="JSON params string or @file.json"),
    bundle_ref: str = typer.Option(..., "--bundle", help="Bundle reference (sha256:...)"),
    seed: int = typer.Option(12345, "--seed", help="Random seed"),
    fn_ref: str = typer.Option("simulate:simulate", "--function", help="Function reference"),
    scheduler: Optional[str] = typer.Option(None, "--scheduler", help="Dask scheduler URL"),
    timeout: int = typer.Option(30, "--timeout", help="Timeout in seconds")
):
    """Run a quick simulation for testing.

    Submit a single simulation task to test bundle execution.

    Examples:
        mops dev quick-sim '{"alpha": 0.5}' --bundle sha256:abc123...
        mops dev quick-sim @params.json --bundle sha256:abc123...
    """
    info("Running quick simulation...")

    # Parse params
    if params.startswith("@"):
        # Load from file
        params_file = Path(params[1:])
        if not params_file.exists():
            error(f"Params file not found: {params_file}")
            raise typer.Exit(1)
        with open(params_file) as f:
            params_dict = json.load(f)
    else:
        try:
            params_dict = json.loads(params)
        except json.JSONDecodeError as e:
            error(f"Invalid JSON params: {e}")
            raise typer.Exit(1)

    info(f"Parameters: {json.dumps(params_dict, indent=2)}")
    info(f"Bundle: {bundle_ref[:20]}...")
    info(f"Function: {fn_ref}")
    info(f"Seed: {seed}")

    client = None
    try:
        # Connect to Dask
        scheduler_url = scheduler or os.environ.get("DASK_SCHEDULER", "tcp://localhost:8786")
        info(f"Connecting to Dask: {scheduler_url}")
        client = Client(scheduler_url, timeout='10s')

        # Create SimulationService
        config = RuntimeConfig.from_env()
        sim_service = DaskSimulationService(client, config)

        # Submit task
        info("Submitting task...")
        task = SimTask(
            fn_ref=fn_ref,
            params=params_dict,
            seed=seed,
            bundle_ref=bundle_ref
        )
        future = sim_service.submit(task)

        # Get result
        info(f"Waiting for result (timeout: {timeout}s)...")
        result = future.result(timeout=timeout)

        success("✅ Simulation completed!")
        info("Result:")
        console.print(json.dumps(result, indent=2))

    except Exception as e:
        error(f"❌ Simulation failed: {e}")
        raise typer.Exit(1)
    finally:
        if client:
            client.close()


@app.command()
def images(
    action: str = typer.Argument(..., help="Action: print|export-env"),
    key: Optional[str] = typer.Argument(None, help="For 'print': registry_host|registry_org|scheduler|worker|runner|adaptive-worker"),
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Image profile (prod|dev|local)"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to modelops-images.yaml"),
):
    """Manage Docker image references.

    This command provides access to the centralized image configuration,
    allowing you to query and export image references for different profiles.

    Examples:
        # Print the registry host
        mops dev images print registry_host

        # Print the full scheduler image reference
        mops dev images print scheduler

        # Print worker image for dev profile
        mops dev images print worker --profile dev

        # Export all image environment variables
        mops dev images export-env

        # Export for CI with dev profile
        mops dev images export-env --profile dev
    """
    try:
        # Load configuration
        config_path = config or Path("modelops-images.yaml")
        img_config = get_image_config()

        # Override profile if specified
        if profile:
            os.environ["MOPS_IMAGE_PROFILE"] = profile
            # Force reload with new profile
            from ..images import ImageConfig
            img_config = ImageConfig.from_yaml(config_path, profile)

        if action == "print":
            if not key:
                error("Key required for 'print' action")
                info("Available keys: registry_host, registry_org, scheduler, worker, runner, adaptive-worker")
                raise typer.Exit(1)

            # Get the active profile
            active_profile = img_config.get_profile(profile)

            # Handle special keys
            if key == "registry_host":
                print(active_profile.registry.host)
            elif key == "registry_org":
                print(active_profile.registry.org)
            elif key in ["scheduler", "worker", "runner", "adaptive-worker"]:
                print(img_config.ref(key, profile))
            else:
                error(f"Unknown key: {key}")
                info("Available keys: registry_host, registry_org, scheduler, worker, runner, adaptive-worker")
                raise typer.Exit(1)

        elif action == "export-env":
            # Export environment variables for shell/CI
            active_profile = img_config.get_profile(profile)
            print(f"REGISTRY={active_profile.registry.host}")
            print(f"ORG={active_profile.registry.org}")
            print(f"SCHEDULER_IMAGE={img_config.scheduler_image(profile)}")
            print(f"WORKER_IMAGE={img_config.worker_image(profile)}")
            print(f"RUNNER_IMAGE={img_config.runner_image(profile)}")
            print(f"ADAPTIVE_WORKER_IMAGE={img_config.adaptive_worker_image(profile)}")

        else:
            error(f"Unknown action: {action}")
            info("Available actions: print, export-env")
            raise typer.Exit(1)

    except FileNotFoundError as e:
        error(f"Configuration file not found: {e}")
        info("Create modelops-images.yaml in the project root")
        raise typer.Exit(1)
    except ValueError as e:
        error(f"Configuration error: {e}")
        raise typer.Exit(1)
    except Exception as e:
        error(f"Unexpected error: {e}")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()