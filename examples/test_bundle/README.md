# Test Bundle

This directory contains the bundle used by integration tests (e.g. `tests/integration/test_critical_regressions.py`).  
It can be regenerated or repackaged with the CLI to keep `.modelops/manifest.json` and the egg-info consistent.

```bash
cd examples/test_bundle
make package      # runs `mops bundle package` via uv
```

The manifest explicitly points to `test_bundle/wire.py:wire_function`, so worker-side discovery
works even if the entry point install is skipped.
