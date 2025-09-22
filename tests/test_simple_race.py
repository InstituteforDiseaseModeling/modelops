"""Simple test to debug process manager hanging."""

import pytest
from pathlib import Path
from modelops.worker.process_manager import WarmProcessManager


@pytest.fixture
def test_bundle_path(tmp_path):
    """Create a minimal test bundle."""
    bundle_path = tmp_path / "test_bundle"
    bundle_path.mkdir()

    # Create a simple wire function
    (bundle_path / "wire.py").write_text("""
def wire(entrypoint, params, seed):
    return {"result": b"ok"}
""")

    # Create pyproject.toml
    (bundle_path / "pyproject.toml").write_text("""
[project]
name = "test-bundle"
version = "0.1.0"

[project.entry-points."modelops.bundle"]
test_bundle = "wire:wire"
""")

    return bundle_path


@pytest.fixture
def process_manager(tmp_path):
    """Create a WarmProcessManager for testing."""
    venvs_dir = tmp_path / "venvs"
    venvs_dir.mkdir()

    manager = WarmProcessManager(
        venvs_dir=venvs_dir,
        max_processes=5,
        force_fresh_venv=False
    )

    yield manager

    # Cleanup
    manager.shutdown_all()


def test_single_process_works(process_manager, test_bundle_path):
    """Test that a single process creation works."""
    bundle_digest = "test123"

    print("Creating process...")
    process = process_manager.get_process(bundle_digest, test_bundle_path)
    print(f"Process created: {process}")

    print("Calling ready...")
    result = process.safe_call("ready", {})
    print(f"Result: {result}")

    assert result.get("ready") is True
    assert result.get("bundle_digest") == bundle_digest


def test_two_sequential_calls(process_manager, test_bundle_path):
    """Test that two sequential calls work."""
    bundle_digest = "test456"

    print("First call...")
    process1 = process_manager.get_process(bundle_digest, test_bundle_path)
    result1 = process1.safe_call("ready", {})
    print(f"First result: {result1}")

    print("Second call...")
    process2 = process_manager.get_process(bundle_digest, test_bundle_path)
    result2 = process2.safe_call("ready", {})
    print(f"Second result: {result2}")

    print(f"Same process? {process1 is process2}")

    assert result1.get("ready") is True
    assert result2.get("ready") is True