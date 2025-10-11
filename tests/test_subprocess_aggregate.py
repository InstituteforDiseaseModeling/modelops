"""Test subprocess_runner aggregate function with real target evaluation."""

import json
import base64
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import polars as pl
import pytest


class MockTargetEvaluation:
    """Mock Calabaria TargetEvaluation."""
    def __init__(self, loss, diagnostics):
        self.loss = loss
        self.diagnostics = diagnostics


def test_aggregate_data_conversion():
    """Test that aggregate properly converts TableArtifacts to DataFrames."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    # Create test data
    df = pl.DataFrame({
        "day": [0, 1, 2],
        "infected": [10, 15, 20]
    })
    arrow_bytes = df.write_ipc(None).getvalue()

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
    mock_target.evaluate = Mock(return_value=MockTargetEvaluation(
        loss=0.123,
        diagnostics={"mean_mse": 0.123}
    ))

    # Simulate sim_returns
    sim_returns = [
        {
            "outputs": {
                "prevalence": {
                    "data": arrow_bytes
                }
            }
        }
    ] * 3

    # Create mock module
    targets_module = MagicMock()

    # Create the target function that returns a target object
    def prevalence_target_func():
        return mock_target

    # Add function directly to targets module (simple module path)
    targets_module.prevalence_target = prevalence_target_func

    # Add module to sys.modules
    sys.modules['targets'] = targets_module

    try:
        # Call aggregate with positional arguments using simple path
        result = runner.aggregate(
            target_entrypoint="targets:prevalence_target",
            sim_returns=sim_returns
        )

        # Verify results
        assert result["loss"] == 0.123
        assert result["n_replicates"] == 3
        assert result["diagnostics"]["mean_mse"] == 0.123

        # Verify target.evaluate was called with proper SimOutputs
        mock_target.evaluate.assert_called_once()
        sim_outputs = mock_target.evaluate.call_args[0][0]
        assert len(sim_outputs) == 3
        for sim_output in sim_outputs:
            assert "prevalence" in sim_output
            assert isinstance(sim_output["prevalence"], pl.DataFrame)
    finally:
        # Clean up sys.modules
        sys.modules.pop('targets', None)


def test_aggregate_inline_data_format():
    """Test aggregate handles inline data format."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    df = pl.DataFrame({"day": [0], "infected": [5]})
    arrow_bytes = df.write_ipc(None).getvalue()

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
        diagnostics={}
    ))

    sim_returns = [
        {
            "outputs": {
                "test": {
                    "inline": arrow_bytes  # inline instead of data
                }
            }
        }
    ]

    # Mock the module with simple path (single part)
    mock_module = MagicMock()

    # Create a function that returns the target object (Calabaria-style)
    def target_func():
        return mock_target

    mock_module.target = target_func
    sys.modules['test'] = mock_module

    try:
        result = runner.aggregate(
            target_entrypoint="test:target",
            sim_returns=sim_returns
        )

        assert result["loss"] == 0.456

        # Verify DataFrame conversion happened
        sim_outputs = mock_target.evaluate.call_args[0][0]
        assert isinstance(sim_outputs[0]["test"], pl.DataFrame)
    finally:
        sys.modules.pop('test', None)


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
                    # Missing 'data' or 'inline'
                }
            }
        }
    ]

    mock_target = Mock()
    mock_target.model_output = "test"

    # Mock the module
    mock_module = MagicMock()

    # Create a function that returns the target object (Calabaria-style)
    def target_func():
        return mock_target

    mock_module.target = target_func
    sys.modules['test'] = mock_module

    try:
        result = runner.aggregate(
            target_entrypoint="test:target",
            sim_returns=sim_returns
        )

        # Should return error
        assert "error" in result
        error_data = json.loads(base64.b64decode(result["error"]))
        assert "TableArtifact missing data" in error_data["error"]
    finally:
        sys.modules.pop('test', None)


def test_aggregate_multiple_outputs():
    """Test aggregation with multiple output tables."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    with patch('modelops.worker.subprocess_runner.SubprocessRunner._setup'):
        runner = SubprocessRunner(
            bundle_path=Path("/tmp/test"),
            venv_path=Path("/tmp/venv"),
            bundle_digest="test123"
        )

    # Create multiple outputs
    df1 = pl.DataFrame({"day": [0, 1], "infected": [10, 20]})
    df2 = pl.DataFrame({"day": [0, 1], "susceptible": [990, 980]})

    mock_target = Mock()
    mock_target.model_output = "prevalence"
    mock_target.evaluate = Mock(return_value=MockTargetEvaluation(
        loss=0.789,
        diagnostics={"n_outputs": 2}
    ))

    sim_returns = [
        {
            "outputs": {
                "prevalence": {"data": df1.write_ipc(None).getvalue()},
                "susceptible": {"data": df2.write_ipc(None).getvalue()}
            }
        }
    ]

    # Mock the module
    mock_module = MagicMock()

    # Create a function that returns the target object (Calabaria-style)
    def target_func():
        return mock_target

    mock_module.target = target_func
    sys.modules['test'] = mock_module

    try:
        result = runner.aggregate(
            target_entrypoint="test:target",
            sim_returns=sim_returns
        )

        assert result["loss"] == 0.789

        # Verify both outputs were converted
        sim_outputs = mock_target.evaluate.call_args[0][0]
        assert len(sim_outputs[0]) == 2
        assert "prevalence" in sim_outputs[0]
        assert "susceptible" in sim_outputs[0]
        assert isinstance(sim_outputs[0]["prevalence"], pl.DataFrame)
        assert isinstance(sim_outputs[0]["susceptible"], pl.DataFrame)
    finally:
        sys.modules.pop('test', None)


def test_aggregate_direct_bytes_format():
    """Test handling of direct bytes (not wrapped in dict)."""
    from modelops.worker.subprocess_runner import SubprocessRunner

    with patch('modelops.worker.subprocess_runner.SubprocessRunner._setup'):
        runner = SubprocessRunner(
            bundle_path=Path("/tmp/test"),
            venv_path=Path("/tmp/venv"),
            bundle_digest="test123"
        )

    df = pl.DataFrame({"value": [1, 2, 3]})
    arrow_bytes = df.write_ipc(None).getvalue()

    mock_target = Mock()
    mock_target.model_output = "output"
    mock_target.evaluate = Mock(return_value=MockTargetEvaluation(
        loss=0.999,
        diagnostics={}
    ))

    # Direct bytes format
    sim_returns = [
        {
            "outputs": {
                "output": arrow_bytes  # Direct bytes, not dict
            }
        }
    ]

    # Mock the module
    mock_module = MagicMock()

    # Create a function that returns the target object (Calabaria-style)
    def target_func():
        return mock_target

    mock_module.target = target_func
    sys.modules['test'] = mock_module

    try:
        result = runner.aggregate(
            target_entrypoint="test:target",
            sim_returns=sim_returns
        )

        assert result["loss"] == 0.999

        # Verify conversion happened
        sim_outputs = mock_target.evaluate.call_args[0][0]
        assert isinstance(sim_outputs[0]["output"], pl.DataFrame)
    finally:
        sys.modules.pop('test', None)