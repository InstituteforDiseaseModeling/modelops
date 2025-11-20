# Implementation Plan — Ultra-Simple Cold Runner & Adapter

**Status**: Ready for implementation
**Owner**: TBD
**Created**: 2025-11-16
**Related**: See `EXECUTION_ENVIRONMENTS_TECHNICAL_OVERVIEW.md` for bug analysis

---

## Objectives (crisp)

- Deliver a standalone cold executor for isolation/debugging (fresh process + fresh environment per task).
- Keep Warm Executor as production default.
- Provide a thin adapter (ColdDebugExecEnv) that shells out to the standalone runner.
- Do not import ModelOps inside the child process. Logs ≠ result stream.
- Ship with acceptance tests and CI.

## Non-goals

- No venv reuse logic in v1 (optional REUSE=1 flag is okay).
- No dependency "verification" step (install, run, exit).

## Definition of Done (DoD)

- `ultra_cold_runner.py` runs a single task and prints exactly one JSON line to stdout; logs go to stderr.
- `ColdDebugExecEnv` integrates runner with existing contracts and passes tests.
- Feature-flag wired; warm remains default.
- Legacy cold path marked deprecated and hidden behind `MODELOPS_ENABLE_LEGACY_COLD=1`.
- CI runs acceptance tests.

---

## 1) Add the standalone runner (stdlib-only)

Use the "Ultra-Simple Cold Runner (Ephemeral venv per task)" script you already have. Place it under:

```
tools/ultra_cold_runner.py
```

Make it executable, no repository imports, single-file.

### Key guarantees to keep (enforced by review):

- Launch child with `python -I -u` (isolated, unbuffered).
- After modifying sys.path, call `importlib.invalidate_caches()`.
- Return structure:
  - Success (simulation): `{"task_id": "...","outputs": {name:{size,checksum,inline}}}`
  - Success (aggregation): `{"loss": float, ...}`
  - Failure (non-zero exit): print minimal `{"_fatal_error": {...}}` to stdout, details to stderr.
- Default fresh venv; optional `REUSE=1` uses strict deps hash (pyproject/requirements + py version).
- Delete temp venv unless `PRESERVE_TMP=1`.

---

## 2) Thin adapter: ColdDebugExecEnv (shells out to runner)

Create:

```
src/modelops/adapters/exec_env/cold_debug.py
```

### Skeleton:

```python
# src/modelops/adapters/exec_env/cold_debug.py
from __future__ import annotations
import base64, json, logging, os, subprocess, sys
from pathlib import Path
from typing import Any
from modelops_contracts import SimReturn, SimTask, TableArtifact
from modelops_contracts.simulation import AggregationReturn, AggregationTask
from modelops_contracts.ports import ExecutionEnvironment, BundleRepository

LOG = logging.getLogger(__name__)

RUNNER_PATH = Path(__file__).parents[4] / "tools" / "ultra_cold_runner.py"  # adjust if needed

class ColdDebugExecEnv(ExecutionEnvironment):
    """Cold executor for isolation debugging; shells out to ultra_cold_runner.py."""

    def __init__(
        self,
        bundle_repo: BundleRepository,
        runner_path: Path | None = None,
        timeout_seconds: int = 600,
        env: dict[str, str] | None = None,
    ):
        self.bundle_repo = bundle_repo
        self.runner_path = Path(runner_path or RUNNER_PATH)
        self.timeout_seconds = timeout_seconds
        self.base_env = os.environ.copy()
        if env:
            self.base_env.update(env)

    def run(self, task: SimTask) -> SimReturn:
        digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)
        payload = json.dumps({
            "entrypoint": str(task.entrypoint) if task.entrypoint else "main",
            "params": dict(task.params.params),
            "seed": task.seed,
        })

        out = self._invoke_runner(bundle_path, payload, aggregation=False)
        # Fatal path
        if "_fatal_error" in out:
            return self._mk_error_return(task, out["_fatal_error"])

        outputs = {}
        for name, meta in out["outputs"].items():
            raw = base64.b64decode(meta["inline"]) if isinstance(meta["inline"], str) else (meta["inline"] or b"")
            outputs[name] = TableArtifact(size=len(raw), inline=raw, checksum=meta["checksum"])

        # Simple task_id; rehash param_id + seed + names
        from hashlib import blake2b
        comp = f"{task.params.param_id[:16]}-{task.seed}-{','.join(sorted(outputs.keys()))}".encode()
        tid = blake2b(comp, digest_size=32).hexdigest()
        return SimReturn(task_id=tid, outputs=outputs)

    def run_aggregation(self, task: AggregationTask) -> AggregationReturn:
        digest, bundle_path = self.bundle_repo.ensure_local(task.bundle_ref)
        payload = json.dumps({
            "target_entrypoint": str(task.target_entrypoint),
            "sim_returns": [
                {
                    "task_id": sr.task_id,
                    "outputs": {
                        n: {
                            "size": art.size,
                            "checksum": art.checksum,
                            "inline": base64.b64encode(art.inline).decode("ascii"),
                        } for n, art in sr.outputs.items()
                    }
                } for sr in task.sim_returns
            ],
            "target_data": task.target_data,
        })

        out = self._invoke_runner(bundle_path, payload, aggregation=True)
        if "_fatal_error" in out:
            raise RuntimeError(f"Aggregation failed: {out['_fatal_error']}")

        return AggregationReturn(
            aggregation_id=task.aggregation_id(),
            loss=float(out["loss"]),
            diagnostics=out.get("diagnostics", {}),
            outputs={},
            n_replicates=out.get("n_replicates", len(task.sim_returns)),
        )

    # ---- internals ---------------------------------------------------------

    def _invoke_runner(self, bundle_path: Path, payload: str, aggregation: bool) -> dict[str, Any]:
        args = [sys.executable, "-u", str(self.runner_path), "--bundle-path", str(bundle_path)]
        if aggregation:
            args.append("--aggregation")

        LOG.info("Invoking ultra_cold_runner.py (aggregation=%s)", aggregation)
        res = subprocess.run(
            args,
            input=payload,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            env=self.base_env,
            cwd=str(bundle_path),
        )
        # Forward child logs
        if res.stderr:
            sys.stderr.write(res.stderr)

        # Always try to parse stdout for structured info
        try:
            out = json.loads(res.stdout.strip() or "{}")
        except Exception:
            out = {"_fatal_error": {"code": res.returncode, "stdout": res.stdout}}

        if res.returncode != 0 and "_fatal_error" not in out:
            out = {"_fatal_error": {"code": res.returncode}}

        return out

    def _mk_error_return(self, task: SimTask, err: dict[str, Any]) -> SimReturn:
        from hashlib import blake2b
        tid = blake2b(f"{task.params.param_id[:16]}-{task.seed}-error".encode(), digest_size=32).hexdigest()
        # Pack error details as an inline artifact for parity with warm path
        import json as _json
        data = _json.dumps(err).encode()
        from modelops_contracts import ErrorInfo, TableArtifact
        return SimReturn(
            task_id=tid,
            outputs={},
            error=ErrorInfo(error_type="ColdDebugError", message=_json.dumps(err), retryable=False),
            error_details=TableArtifact(size=len(data), inline=data),
        )
```

### Wiring (example usage in service):

```python
# src/modelops/services/dask_simulation.py (where env is chosen)
use_cold_debug = os.environ.get("MODELOPS_COLD_DEBUG", "0") in ("1", "true", "TRUE")
if use_cold_debug:
    from ..adapters.exec_env.cold_debug import ColdDebugExecEnv
    exec_env = ColdDebugExecEnv(bundle_repo=bundle_repo)
else:
    from ..adapters.exec_env.warm import IsolatedWarmExecEnv
    exec_env = IsolatedWarmExecEnv(bundle_repo=bundle_repo, venvs_dir=venvs_dir, storage_dir=storage_dir)
```

---

## 3) Acceptance tests (must pass locally and in CI)

### Bash smoke tests (drop into `tools/test_ultra_cold.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail

# Create tiny bundle with entry point
TMP=$(mktemp -d)
B=$TMP/bundle
mkdir -p "$B/src/mywire"
cat > "$B/pyproject.toml" <<'EOF'
[project]
name = "mywire"
version = "0.0.1"
dependencies = []
[project.entry-points."modelops.wire"]
execute = "mywire.wire:wire"
[tool.setuptools.packages.find]
where = ["src"]
EOF
cat > "$B/src/mywire/wire.py" <<'EOF'
def wire(entrypoint, params, seed):
    return {"raw": f"ok-{seed}".encode()}
EOF

# 1. Simulation path
OUT=$(echo '{"entrypoint":"main","params":{},"seed":7}' \
  | python tools/ultra_cold_runner.py --bundle-path "$B")
python - "$OUT" <<'PY'
import sys, json
d=json.loads(sys.argv[1]); assert "task_id" in d and "raw" in d["outputs"]
PY

# 2. Aggregation old-style
mkdir -p "$B/src/targets"
cat > "$B/src/targets/agg.py" <<'EOF'
def loss_one(sim_returns):
    return {"loss": 1.0}
EOF
OUT=$(echo '{"target_entrypoint":"targets.agg:loss_one","sim_returns":[]}' \
  | python tools/ultra_cold_runner.py --bundle-path "$B" --aggregation)
python - "$OUT" <<'PY'
import sys, json
d=json.loads(sys.argv[1]); assert "loss" in d
PY

# 3. Fatal path returns _fatal_error JSON
BAD=$(echo '{"entrypoint":"does.not.exist:fn","params":{},"seed":1}' \
  | python tools/ultra_cold_runner.py --bundle-path "$B" || true)
python - "$BAD" <<'PY'
import sys, json
d=json.loads(sys.argv[1]); assert "_fatal_error" in d
PY

echo "✓ ultra_cold_runner smoke tests passed"
```

### Pytest for adapter (example)

```python
# tests/test_cold_debug_env.py
import json, base64
from pathlib import Path
from modelops_contracts import SimTask, SimParams
from modelops_contracts.simulation import AggregationTask
from modelops_contracts.ports import BundleRepository
from modelops.adapters.exec_env.cold_debug import ColdDebugExecEnv

def test_cold_debug_sim(tmp_path: Path, fake_bundle_repo: BundleRepository):
    env = ColdDebugExecEnv(bundle_repo=fake_bundle_repo)
    task = SimTask(
        bundle_ref="sha256:dummy", entrypoint="main",
        params=SimParams(params={"beta": 0.2}), seed=3
    )
    result = env.run(task)
    assert result.task_id and "table" not in result.outputs  # minimal sanity

def test_cold_debug_agg(tmp_path: Path, fake_bundle_repo: BundleRepository, sim_return_factory):
    env = ColdDebugExecEnv(bundle_repo=fake_bundle_repo)
    sr = sim_return_factory()  # provide at least one output with inline bytes
    agg = AggregationTask(bundle_ref="sha256:dummy", target_entrypoint="targets.agg:loss_one", sim_returns=[sr])
    out = env.run_aggregation(agg)
    assert out.loss >= 0.0
```

Provide a `fake_bundle_repo` fixture that maps "sha256:dummy" to the temporary bundle directory you build in the bash test.

---

## 4) Rollout / flags

- Default remains Warm.
- Enable via `MODELOPS_COLD_DEBUG=1` in environments where you want cold isolation.
- Deprecate legacy cold path behind: `MODELOPS_ENABLE_LEGACY_COLD=1`.
- Document both flags in README.

---

## Cleanup Plan — Legacy Cold Code

**Goal**: make the old cold path impossible to hit by accident, then remove it in a release or two.

### Phase A — Immediate safety & messaging

1. **Block by default in legacy cold entry points**:

```python
# src/modelops/worker/cold_runner.py (legacy file)
import os, sys, logging
log = logging.getLogger(__name__)
if os.environ.get("MODELOPS_ENABLE_LEGACY_COLD", "0") not in ("1", "true", "TRUE"):
    log.error("Legacy cold runner is disabled. Use ColdDebugExecEnv/ultra_cold_runner.py.")
    sys.exit(2)
```

2. **Loud deprecation in adapter**:

```python
# src/modelops/adapters/exec_env/cold.py (legacy)
import warnings
warnings.warn(
    "Legacy ColdExecEnv is deprecated and disabled by default. "
    "Use ColdDebugExecEnv (ultra_cold_runner). Set MODELOPS_ENABLE_LEGACY_COLD=1 "
    "only for temporary fallback.",
    DeprecationWarning,
    stacklevel=2
)
```

3. **Docs**: add an upgrade note and show the new flag.

### Phase B — If you must keep legacy cold briefly (surgical fixes only)

Only if a consumer absolutely needs it during the transition; otherwise skip to removal.

- **Fix digest/venv mismatch** (full digest + marker check):

```python
# src/modelops/adapters/exec_env/cold.py:_get_or_create_venv
venv_name = f"{digest}-py{sys.version_info.major}.{sys.version_info.minor}"
venv_path = self.venvs_dir / venv_name
marker = venv_path / ".bundle_digest"
if venv_path.exists():
    if marker.exists() and marker.read_text().strip() == digest:
        return venv_path
    import shutil; shutil.rmtree(venv_path)
# (re)create
subprocess.run(["uv", "venv", str(venv_path)], check=True)
marker.write_text(digest)
return venv_path
```

- **Bundle-scoped deps marker** (avoid cross-bundle reuse):

```python
# src/modelops/worker/cold_runner.py: ensure_dependencies_installed
bundle_digest = compute_bundle_digest(bundle_path)  # same algo as new runner
deps_marker = Path(sys.prefix) / f".deps_installed_{bundle_digest}"
# if exists: return; else install; write marker with digest
```

- **Remove ModelOps imports** from the legacy child path if feasible; keep data as plain dicts until parent reconstructs contracts.

- **Fix doubled path** (normalize in repo):

```python
# bundle_repo.ensure_local
local = (Path(self.base_dir) / "bundles" / digest).resolve()
return digest, local
```

### Phase C — Removal

- Delete files:
  - `src/modelops/worker/cold_runner.py` (legacy)
  - `src/modelops/adapters/exec_env/cold.py`
- Remove references from build/config/exports.
- Purge tests that target legacy cold.
- Release notes: removal + replacement details.

---

## 5) CI & tooling

- Add `tools/test_ultra_cold.sh` to CI (Linux).
- Cache pip/uv wheels between jobs to stabilize timings.
- Lint gate to ensure no ModelOps imports in `tools/ultra_cold_runner.py`:

```bash
grep -R "modelops" tools/ultra_cold_runner.py && { echo "Runner must be standalone"; exit 1; }
```

---

## 6) Risk checklist & mitigations

- **Stdout contamination** → Child shim redirects print() to stderr; single JSON write at end.
- **Site-packages bleed** → Use `python -I`; rewrite sys.path to [bundle] + rest; invalidate caches.
- **Long install times** → Optional `REUSE=1` cache by strong hash; keep default fresh for correctness.
- **Windows path** → Use Scripts vs bin.
- **Large outputs** → Base64 inline only; warn on >1MB if desired (adapter).

---

## 7) Developer quickstart

```bash
# Run smoke tests
bash tools/test_ultra_cold.sh

# Use ColdDebugExecEnv at runtime
MODELOPS_COLD_DEBUG=1 python -m modelops.services.dask_simulation ...

# Temporary legacy fallback (not recommended)
MODELOPS_ENABLE_LEGACY_COLD=1 python -m modelops.worker.cold_runner  # deprecated
```

---

**This gives an agent a zero-ambiguity path to implement the minimal, reliable cold executor, integrate it, and cleanly retire the brittle legacy code—with enough snippets to code against immediately.**
