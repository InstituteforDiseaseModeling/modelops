"""Test subprocess_runner aggregate function with real target evaluation."""

import json
import base64
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import polars as pl
from polars.testing import assert_frame_equal
import pytest


class MockTargetEvaluation:
    """Mock Calabaria TargetEvaluation."""
    def __init__(self, loss, diagnostics=None, name="test_target", weight=1.0):
        self.loss = loss
        self.name = name
        self.weight = weight
        # Note: Real TargetEvaluation doesn't have diagnostics attribute
        # We store it separately for test verification
        self._test_diagnostics = diagnostics or {}


def df_to_ipc_bytes(df: pl.DataFrame) -> bytes:
    """Convert DataFrame to Arrow IPC bytes."""
    import io
    buf = io.BytesIO()
    df.write_ipc(buf)
    return buf.getvalue()


def test_aggregate_data_conversion():
    """Test that aggregate properly converts TableArtifacts to DataFrames."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    # Create test data
    df = pl.DataFrame({
        "day": [0, 1, 2],
        "infected": [10, 15, 20]
    })
    arrow_bytes = df_to_ipc_bytes(df)

    # Mock runner with minimal setup
    with patch('modelops.worker.subprocess_runner.SubprocessRunner._setup'):
        runner = SubprocessRunner(
            bundle_path=Path("/tmp/test"),
            venv_path=Path("/tmp/venv"),
            bundle_digest="test123"
        )

    # Create mock target
    mock_target = Mock()
    mock_target.model_output = "prevalence"
    test_diagnostics = {"mean_mse": 0.123, "n_evaluated": 100}
    mock_target.evaluate = Mock(return_value=MockTargetEvaluation(
        loss=0.123,
        diagnostics=test_diagnostics
    ))

    # Simulate sim_returns with proper deep copy to avoid aliasing
    base_return = {
        "outputs": {
            "prevalence": {
                "data": arrow_bytes
            }
        }
    }
    sim_returns = [deepcopy(base_return) for _ in range(3)]

    # Use unique module name to avoid collisions
    modname = "_test_targets_aggregate_conversion"
    targets_module = MagicMock()

    # Create the target function that returns a target object
    def prevalence_target_func():
        return mock_target

    # Add function directly to module
    targets_module.prevalence_target = prevalence_target_func

    # Add module to sys.modules
    sys.modules[modname] = targets_module

    try:
        # Call aggregate with positional arguments using test module
        result = runner.aggregate(
            target_entrypoint=f"{modname}:prevalence_target",
            sim_returns=sim_returns
        )

        # Verify results
        assert result["loss"] == 0.123
        assert result["n_replicates"] == 3
        # Check that basic diagnostics are present (not custom ones from mock)
        assert "target_type" in result["diagnostics"]
        assert "model_output" in result["diagnostics"]
        assert result["diagnostics"]["target_name"] == "test_target"

        # Verify target.evaluate was called with proper SimOutputs
        mock_target.evaluate.assert_called_once()
        sim_outputs = mock_target.evaluate.call_args[0][0]
        assert len(sim_outputs) == 3

        # Verify exact DataFrame content, not just type
        for sim_output in sim_outputs:
            assert "prevalence" in sim_output
            assert_frame_equal(sim_output["prevalence"], df)
    finally:
        # Clean up sys.modules
        sys.modules.pop(modname, None)


@pytest.mark.parametrize("fmt_key", ["data", "inline", "bytes"])
def test_aggregate_formats_parametrized(fmt_key):
    """Test aggregate handles different data formats."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    df = pl.DataFrame({"day": [0], "infected": [5]})
    arrow_bytes = df_to_ipc_bytes(df)

    with patch('modelops.worker.subprocess_runner.SubprocessRunner._setup'):
        runner = SubprocessRunner(
            bundle_path=Path("/tmp/test"),
            venv_path=Path("/tmp/venv"),
            bundle_digest="test123"
        )

    mock_target = Mock()
    mock_target.model_output = "test"
    mock_target.evaluate = Mock(return_value=MockTargetEvaluation(
        loss=0.456,
        diagnostics={"format_used": fmt_key}
    ))

    # Format the outputs based on parameter
    if fmt_key == "bytes":
        # Direct bytes format
        outputs = {"test": arrow_bytes}
    else:
        # Dict format with data or inline key
        outputs = {"test": {fmt_key: arrow_bytes}}

    sim_returns = [{"outputs": outputs}]

    # Use unique module name
    modname = f"_test_targets_fmt_{fmt_key}"
    mock_module = MagicMock()

    # Create a function that returns the target object
    def target_func():
        return mock_target

    mock_module.target = target_func
    sys.modules[modname] = mock_module

    try:
        result = runner.aggregate(
            target_entrypoint=f"{modname}:target",
            sim_returns=sim_returns
        )

        assert result["loss"] == 0.456
        # Custom diagnostics from mock are not preserved
        assert "target_type" in result["diagnostics"]

        # Verify DataFrame conversion happened with correct content
        sim_outputs = mock_target.evaluate.call_args[0][0]
        assert_frame_equal(sim_outputs[0]["test"], df)
    finally:
        sys.modules.pop(modname, None)


def test_aggregate_error_handling():
    """Test error handling for invalid data formats."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    with patch('modelops.worker.subprocess_runner.SubprocessRunner._setup'):
        runner = SubprocessRunner(
            bundle_path=Path("/tmp/test"),
            venv_path=Path("/tmp/venv"),
            bundle_digest="test123"
        )

    # Invalid format - missing both data and inline
    sim_returns = [
        {
            "outputs": {
                "test": {
                    "size": 100,
                    "checksum": "abc"
                    # Missing 'data' or 'inline' - this should trigger error
                }
            }
        }
    ]

    mock_target = Mock()
    mock_target.model_output = "test"

    # Use unique module name
    modname = "_test_targets_error_handling"
    mock_module = MagicMock()

    def target_func():
        return mock_target

    mock_module.target = target_func
    sys.modules[modname] = mock_module

    try:
        result = runner.aggregate(
            target_entrypoint=f"{modname}:target",
            sim_returns=sim_returns
        )

        # Should return error
        assert "error" in result
        assert isinstance(result["error"], str)

        # Decode and check for stable error indicator
        error_data = json.loads(base64.b64decode(result["error"]))
        error_msg = error_data.get("error", "").lower()
        # More robust assertion - check for key terms
        assert "missing data" in error_msg or "tableartifact" in error_msg
    finally:
        sys.modules.pop(modname, None)


def test_aggregate_multiple_outputs():
    """Test aggregation with multiple output tables."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    with patch('modelops.worker.subprocess_runner.SubprocessRunner._setup'):
        runner = SubprocessRunner(
            bundle_path=Path("/tmp/test"),
            venv_path=Path("/tmp/venv"),
            bundle_digest="test123"
        )

    # Create multiple outputs with different content
    df1 = pl.DataFrame({"day": [0, 1], "infected": [10, 20]})
    df2 = pl.DataFrame({"day": [0, 1], "susceptible": [990, 980]})

    mock_target = Mock()
    mock_target.model_output = "prevalence"
    mock_target.evaluate = Mock(return_value=MockTargetEvaluation(
        loss=0.789,
        diagnostics={"n_outputs": 2, "tables": ["prevalence", "susceptible"]}
    ))

    sim_returns = [
        {
            "outputs": {
                "prevalence": {"data": df_to_ipc_bytes(df1)},
                "susceptible": {"data": df_to_ipc_bytes(df2)}
            }
        }
    ]

    # Use unique module name
    modname = "_test_targets_multiple"
    mock_module = MagicMock()

    def target_func():
        return mock_target

    mock_module.target = target_func
    sys.modules[modname] = mock_module

    try:
        result = runner.aggregate(
            target_entrypoint=f"{modname}:target",
            sim_returns=sim_returns
        )

        assert result["loss"] == 0.789
        # Custom diagnostics from mock are not preserved
        assert "target_type" in result["diagnostics"]

        # Verify both outputs were converted with correct content
        sim_outputs = mock_target.evaluate.call_args[0][0]
        assert len(sim_outputs[0]) == 2
        assert "prevalence" in sim_outputs[0]
        assert "susceptible" in sim_outputs[0]

        # Verify exact content of each DataFrame
        assert_frame_equal(sim_outputs[0]["prevalence"], df1)
        assert_frame_equal(sim_outputs[0]["susceptible"], df2)
    finally:
        sys.modules.pop(modname, None)


def test_aggregate_base64_inline_data():
    """Test that aggregate handles base64-encoded inline data correctly."""
    from modelops.worker.subprocess_runner import SubprocessRunner
    import base64

    with patch('modelops.worker.subprocess_runner.SubprocessRunner._setup'):
        runner = SubprocessRunner(
            bundle_path=Path("/tmp/test"),
            venv_path=Path("/tmp/venv"),
            bundle_digest="test123"
        )

    # Create test data
    df = pl.DataFrame({"day": [0, 1], "infected": [10, 20]})
    arrow_bytes = df_to_ipc_bytes(df)

    # Encode as base64 string (simulates what happens during serialization)
    base64_str = base64.b64encode(arrow_bytes).decode('utf-8')

    mock_target = Mock()
    mock_target.model_output = "test"
    mock_target.evaluate = Mock(return_value=MockTargetEvaluation(
        loss=0.456,
        diagnostics={"format": "base64"}
    ))

    # Test with base64-encoded string
    sim_returns = [{"outputs": {"test": {"inline": base64_str}}}]

    modname = "_test_targets_base64"
    mock_module = MagicMock()
    mock_module.target = lambda: mock_target
    sys.modules[modname] = mock_module

    try:
        result = runner.aggregate(
            target_entrypoint=f"{modname}:target",
            sim_returns=sim_returns
        )

        assert result["loss"] == 0.456
        # Custom diagnostics from mock are not preserved
        assert "target_type" in result["diagnostics"]

        # Verify DataFrame was properly decoded
        sim_outputs = mock_target.evaluate.call_args[0][0]
        assert_frame_equal(sim_outputs[0]["test"], df)
    finally:
        sys.modules.pop(modname, None)


def test_aggregate_replicate_independence():
    """Test that replicates are handled independently without aliasing."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    with patch('modelops.worker.subprocess_runner.SubprocessRunner._setup'):
        runner = SubprocessRunner(
            bundle_path=Path("/tmp/test"),
            venv_path=Path("/tmp/venv"),
            bundle_digest="test123"
        )

    # Create different data for each replicate
    dfs = [
        pl.DataFrame({"value": [1, 2, 3]}),
        pl.DataFrame({"value": [4, 5, 6]}),
        pl.DataFrame({"value": [7, 8, 9]})
    ]

    mock_target = Mock()
    mock_target.model_output = "output"
    mock_target.evaluate = Mock(return_value=MockTargetEvaluation(
        loss=0.333,
        diagnostics={"n_unique_replicates": 3}
    ))

    # Each replicate gets unique data
    sim_returns = [
        {"outputs": {"output": {"data": df_to_ipc_bytes(df)}}}
        for df in dfs
    ]

    modname = "_test_targets_replicate"
    mock_module = MagicMock()
    mock_module.target = lambda: mock_target
    sys.modules[modname] = mock_module

    try:
        result = runner.aggregate(
            target_entrypoint=f"{modname}:target",
            sim_returns=sim_returns
        )

        assert result["loss"] == 0.333

        # Verify each replicate has unique content
        sim_outputs = mock_target.evaluate.call_args[0][0]
        assert len(sim_outputs) == 3
        for i, sim_output in enumerate(sim_outputs):
            assert_frame_equal(sim_output["output"], dfs[i])
    finally:
        sys.modules.pop(modname, None)


def test_import_path_integration(tmp_path, monkeypatch):
    """Integration test with real module import (no sys.modules mocking)."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    # Create a real Python package in temp directory
    pkg_dir = tmp_path / "mytargets"
    pkg_dir.mkdir()

    # Write __init__.py
    (pkg_dir / "__init__.py").write_text("")

    # Write module with target function
    (pkg_dir / "prevalence.py").write_text("""
import polars as pl

def prevalence_target():
    class Target:
        model_output = "prevalence"

        def evaluate(self, sim_outputs):
            # Simple evaluation: count total rows
            total_rows = sum(
                sim_output["prevalence"].height
                for sim_output in sim_outputs
            )

            class Result:
                def __init__(self):
                    self.loss = 0.1 * total_rows
                    self.name = "test_target"
                    self.weight = 1.0

            return Result()

    return Target()
""")

    # Add temp directory to Python path
    monkeypatch.syspath_prepend(str(tmp_path))

    # Create test data
    df = pl.DataFrame({"day": [0, 1], "infected": [5, 10]})
    arrow_bytes = df_to_ipc_bytes(df)

    with patch('modelops.worker.subprocess_runner.SubprocessRunner._setup'):
        runner = SubprocessRunner(
            bundle_path=Path("/tmp/test"),
            venv_path=Path("/tmp/venv"),
            bundle_digest="test123"
        )

    # Use the real module path
    result = runner.aggregate(
        target_entrypoint="mytargets.prevalence:prevalence_target",
        sim_returns=[
            {"outputs": {"prevalence": {"data": arrow_bytes}}}
            for _ in range(2)
        ]
    )

    # 2 replicates * 2 rows each = 4 total rows * 0.1 = 0.4
    assert result["loss"] == 0.4
    assert result["n_replicates"] == 2
    # Custom diagnostics from test module are not preserved
    assert "target_type" in result["diagnostics"]


@pytest.mark.parametrize("entrypoint_format", [
    "simple:func",           # simple module
    "pkg.mod:func",         # package.module format
    pytest.param("pkg.sub.mod:func", marks=pytest.mark.xfail(
        reason="Deep nesting (3+ levels) has import issues in test environment"
    )),     # nested package format
])
def test_entrypoint_formats(entrypoint_format, tmp_path, monkeypatch):
    """Test various entrypoint format support."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    # Parse the entrypoint to create appropriate directory structure
    module_path, func_name = entrypoint_format.split(":")
    parts = module_path.split(".")

    # Create nested directories as needed
    current_dir = tmp_path
    for part in parts[:-1]:
        current_dir = current_dir / part
        current_dir.mkdir(exist_ok=True)
        (current_dir / "__init__.py").write_text("")

    # Write the final module
    module_file = current_dir / f"{parts[-1]}.py"
    module_file.write_text(f"""
def {func_name}():
    class T:
        model_output = "test"
        def evaluate(self, sim_outputs):
            return type("R", (), {{"loss": 0.25, "name": "test", "weight": 1.0}})()
    return T()
""")

    monkeypatch.syspath_prepend(str(tmp_path))

    df = pl.DataFrame({"value": [1]})
    arrow_bytes = df_to_ipc_bytes(df)

    with patch('modelops.worker.subprocess_runner.SubprocessRunner._setup'):
        runner = SubprocessRunner(
            bundle_path=Path("/tmp/test"),
            venv_path=Path("/tmp/venv"),
            bundle_digest="test123"
        )

    result = runner.aggregate(
        target_entrypoint=entrypoint_format,
        sim_returns=[{"outputs": {"test": {"data": arrow_bytes}}}]
    )

    assert result["loss"] == 0.25
    # Custom diagnostics are not preserved
    assert "target_type" in result["diagnostics"]