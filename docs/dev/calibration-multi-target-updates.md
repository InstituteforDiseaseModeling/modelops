# Calibration & Target Evaluation Updates (2025‑11)

Recent work on Calabaria + ModelOps introduced several behavioural changes that anyone running calibration jobs or consuming Provenance artifacts should understand. This note captures the engineering choices so we have a durable reference.

## Deterministic Seeds

- `generate_seed_info()` now derives trial/replicate seeds from `blake2b(param_id + base_seed)`.
- Python’s randomized `hash()` was causing different pods to evaluate different parameter sets for the _same_ job. The new derivation keeps seeds stable across interpreter restarts, pods, and platforms.
- Tests (`tests/calibration/test_calibration_wire.py`) assert the exact seeds for a known param id so we lock in the behaviour.

## Asof Alignment Safety

- `AsofJoin.align()` sorts both observed and simulated frames on every call and re-enables Polars’ `check_sortedness`.
- Previously, unsorted data silently produced incorrect alignments; now we either run against sorted input or get a validation error in the logs/tests.

## TrialResult Creation = No Placeholders

- `convert_to_trial_result()` no longer fabricates `float("inf")` losses. If a target or raw bundle fails to emit an explicit loss, we now return a `TrialResult` with `status=FAILED` and structured diagnostics.
- Any consumer of `TrialResult` should inspect `diagnostics["error"]` rather than assuming missing data is harmless.

## Multi‑Target Aggregation Pipeline

- Calibration wire submits a `ReplicateSet` per target entrypoint instead of faking “first target only”.
- `convert_to_trial_result()` aggregates all per-target results for a parameter set and emits a single `TrialResult` with:
  ```json
  {
    "loss": "mean across targets",
    "diagnostics": {
      "targets": {
        "targets/A": {"loss": ..., ...},
        "targets/B": {"loss": ..., ...}
      }
    }
  }
  ```
- Failures in any target mark the entire parameter set as failed with detailed diagnostics per target. Optimizers now have visibility into which target stalled.

## Provenance Upload API (Calabaria + Runtime)

- `ProvenanceStore` exposes `supports_remote_uploads()`, `upload_directory()`, and `get_remote_backend_info()` to eliminate the private `_azure_backend` reach-in pattern.
- Calibration wire, job views, and telemetry storage all use the new API so uploads are optional (local cache continues to work) and easy to introspect.

## Job Runner Multi‑Target Support

- `run_simulation_job()` submits one `ReplicateSet` per target entrypoint and builds `results_by_target` so parquet job views include every target instead of the first one only.
- When no targets are configured we fall back to the legacy `default` key; when targets exist we log the loss per target.

## Tests Added

- `tests/calibration/test_calibration_wire.py::test_calibration_wire_multi_target_combines_results` verifies the end-to-end ask/tell loop aggregates target diagnostics and stops on convergence.
- `tests/test_warm_timeout.py` ensures a hung bundle is evicted after `rpc_timeout_seconds` (see warm runtime note).

These changes make calibration results reproducible, diagnosable, and easier to consume downstream. When writing new docs or tooling, describe TrialResults as “one per param set, containing per-target diagnostics” and make sure job info surfaces the richer structure.
