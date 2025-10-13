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

While tests normally create their own Dask clusters, you can use an external cluster for debugging:

```bash
# Start external Dask cluster
make dask-local        # Starts at tcp://localhost:8786

# Tests will detect and use the external cluster
make test-integration

# Stop when done
make dask-stop
```

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