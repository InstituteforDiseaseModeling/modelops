#!/usr/bin/env bash
#
# Smoke tests for ultra_cold_runner.py
#
# Tests:
# 1. Simulation task execution
# 2. Aggregation task execution (old-style)
# 3. Fatal error handling
# 4. Calabaria-style target (if polars available)
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="${SCRIPT_DIR}/ultra_cold_runner.py"
PYTHON="${PYTHON:-python3}"

echo "==> ultra_cold_runner smoke tests"
echo ""

# Create temp bundle
TMP=$(mktemp -d)
trap "rm -rf $TMP" EXIT

B="$TMP/bundle"
mkdir -p "$B/src/mywire"

# pyproject.toml
cat > "$B/pyproject.toml" <<'EOF'
[project]
name = "mywire"
version = "0.0.1"
dependencies = []

[project.entry-points."modelops.wire"]
execute = "mywire.wire:wire"

[tool.setuptools.package-dir]
"" = "src"

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
EOF

# Wire function
cat > "$B/src/mywire/__init__.py" <<'EOF'
EOF

cat > "$B/src/mywire/wire.py" <<'EOF'
def wire(entrypoint, params, seed):
    """Simple wire function for testing."""
    return {"raw": f"ok-seed{seed}".encode()}
EOF

echo "✓ Created test bundle at $B"
echo ""

# Test 1: Simulation
echo "==> Test 1: Simulation task"
OUT=$(echo '{"entrypoint":"main","params":{"beta":0.5},"seed":7}' \
  | $PYTHON "$RUNNER" --bundle-path "$B")

$PYTHON - "$OUT" <<'PY'
import sys, json
d = json.loads(sys.argv[1])
assert "task_id" in d, "Missing task_id"
assert "outputs" in d, "Missing outputs"
assert "raw" in d["outputs"], "Missing raw output"
assert d["outputs"]["raw"]["size"] > 0, "Empty output"
print(f"✓ Got task_id={d['task_id'][:8]}...")
PY

echo ""

# Test 2: Aggregation (old-style)
echo "==> Test 2: Aggregation task (old-style)"
mkdir -p "$B/src/targets"
cat > "$B/src/targets/__init__.py" <<'EOF'
EOF

cat > "$B/src/targets/agg.py" <<'EOF'
def loss_one(sim_returns):
    """Simple aggregation that returns loss=1.0."""
    return {"loss": 1.0, "n_replicates": len(sim_returns)}
EOF

OUT=$(echo '{"target_entrypoint":"targets.agg:loss_one","sim_returns":[]}' \
  | $PYTHON "$RUNNER" --bundle-path "$B" --aggregation)

$PYTHON - "$OUT" <<'PY'
import sys, json
d = json.loads(sys.argv[1])
assert "loss" in d, "Missing loss"
assert d["loss"] == 1.0, f"Wrong loss: {d['loss']}"
print(f"✓ Got loss={d['loss']}")
PY

echo ""

# Test 3: Fatal error path (missing wire entry point)
echo "==> Test 3: Fatal error handling"
# Temporarily break the entry point
cat > "$B/pyproject.toml" <<'EOF'
[project]
name = "mywire"
version = "0.0.1"
dependencies = []

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
EOF

BAD=$(echo '{"entrypoint":"main","params":{},"seed":1}' \
  | $PYTHON "$RUNNER" --bundle-path "$B" || true)

# Restore entry point
cat > "$B/pyproject.toml" <<'EOF'
[project]
name = "mywire"
version = "0.0.1"
dependencies = []

[project.entry-points."modelops.wire"]
execute = "mywire.wire:wire"

[tool.setuptools.package-dir]
"" = "src"

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
EOF

$PYTHON - "$BAD" <<'PY'
import sys, json
d = json.loads(sys.argv[1])
assert "_fatal_error" in d, "Should have _fatal_error"
assert "code" in d["_fatal_error"], "Missing error code"
print(f"✓ Got fatal error: code={d['_fatal_error']['code']}")
PY

echo ""

# Test 4: Invalid JSON input
echo "==> Test 4: Invalid JSON input"
BAD=$(echo 'not valid json' | $PYTHON "$RUNNER" --bundle-path "$B" || true)

$PYTHON - "$BAD" <<'PY'
import sys, json
d = json.loads(sys.argv[1])
assert "_fatal_error" in d, "Should have _fatal_error for invalid JSON"
print("✓ Handled invalid JSON")
PY

echo ""

echo "========================================="
echo "✓ All ultra_cold_runner smoke tests passed!"
echo "========================================="
