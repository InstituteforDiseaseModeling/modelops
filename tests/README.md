# ModelOps Test Suite

## Quick Start

```bash
# Run unit tests (default)
make test

# Run integration tests
make test-integration   # Tests create their own LocalCluster instances
```

## Test Categories

### Unit Tests (221 tests)
- Run by default with `make test` or `uv run pytest`
- No external dependencies required
- Fast execution (~10-20 seconds)

### Integration Tests (31 tests)
- Marked with `@pytest.mark.integration`
- Each test creates its own isolated Dask LocalCluster
- Test end-to-end workflows, bundle operations, and distributed computing
- Resource usage scales based on environment (CI vs local)

### Test Output Meanings

- **PASSED**: Test succeeded
- **SKIPPED**: Test was skipped due to missing runtime dependencies or resource constraints
- **XFAIL**: Expected failure - documents known limitations (e.g., deep package nesting)
- **DESELECTED**: Tests excluded by markers (e.g., integration tests when running unit tests)

## Running Specific Tests

```bash
# Run a specific test file
uv run pytest tests/test_component_dependencies.py

# Run a specific test function
uv run pytest tests/test_dask_serialization.py::test_cloudpickle_simtask

# Run tests matching a pattern
uv run pytest -k "test_validate"

# Run with verbose output
uv run pytest -v

# Run with coverage
uv run pytest --cov=modelops --cov-report=html
```

## CI Behavior

Integration tests run automatically on GitHub Actions:
- **Resource Scaling**: CI uses 1 worker with 1GB memory (vs 2 workers, 2GB locally)
- **Timeouts**: Tests have 60-second timeout per test, 10-minute overall
- **Auto-skip**: Tests skip gracefully when resources are constrained

## Advanced: Using External Dask for Debugging

By default, tests create their own LocalCluster instances. To use an external Dask cluster for debugging:

```bash
# Start external Dask cluster
make dask-local        # Starts at tcp://localhost:8786

# Explicitly use external cluster with --dask-address or DASK_ADDRESS env var
make test-integration-external   # Uses --dask-address=tcp://localhost:8786
# OR
DASK_ADDRESS=tcp://localhost:8786 make test-integration

# Stop when done
make dask-stop
```

**Note**: Tests no longer auto-detect external clusters. You must explicitly opt-in using `--dask-address` or the `DASK_ADDRESS` environment variable.

## Test Markers

- `integration`: Tests requiring Dask cluster or other infrastructure
- `slow`: Long-running tests (not currently used)

## Troubleshooting

**Tests skip with "LocalCluster creation failed"**:
- Check system resources (memory, CPU)
- Ensure no zombie Dask processes: `pkill -f dask`
- On macOS: Ensure OrbStack is running if needed for containers

**Tests hang**:
- Timeout protection will skip after 60 seconds in CI
- Locally, check for zombie processes

**Import errors**: Ensure dependencies are installed with `uv sync`

**Deselected tests**: Use `-m integration` to run integration tests explicitly