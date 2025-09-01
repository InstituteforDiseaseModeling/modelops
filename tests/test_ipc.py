"""Tests for IPC conversion utilities."""

import pytest
from modelops.services.ipc import to_ipc_tables, from_ipc_tables, validate_sim_return


def test_ipc_roundtrip_dict():
    """Test IPC roundtrip with dict data."""
    data = {
        "results": {"x": [1, 2, 3], "y": [4, 5, 6]},
        "metrics": {"loss": [0.5], "accuracy": [0.95]}
    }
    
    # Convert to IPC
    ipc_data = to_ipc_tables(data)
    
    # All values should be bytes
    assert all(isinstance(v, bytes) for v in ipc_data.values())
    
    # Round trip back
    recovered = from_ipc_tables(ipc_data)
    
    # Check structure is preserved
    assert set(recovered.keys()) == set(data.keys())


def test_ipc_roundtrip_pandas():
    """Test IPC roundtrip with pandas DataFrames."""
    try:
        import pandas as pd
    except ImportError:
        pytest.skip("pandas not installed")
    
    data = {
        "df1": pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]}),
        "df2": pd.DataFrame({"x": [7.0, 8.0], "y": [9.0, 10.0]})
    }
    
    # Convert to IPC
    ipc_data = to_ipc_tables(data)
    
    # All values should be bytes
    assert all(isinstance(v, bytes) for v in ipc_data.values())
    
    # Round trip back
    recovered = from_ipc_tables(ipc_data)
    
    # Check DataFrames are equivalent
    pd.testing.assert_frame_equal(recovered["df1"], data["df1"])
    pd.testing.assert_frame_equal(recovered["df2"], data["df2"])


def test_ipc_roundtrip_polars():
    """Test IPC roundtrip with polars DataFrames."""
    try:
        import polars as pl
    except ImportError:
        pytest.skip("polars not installed")
    
    data = {
        "df1": pl.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]}),
        "df2": pl.DataFrame({"x": [7.0, 8.0], "y": [9.0, 10.0]})
    }
    
    # Convert to IPC
    ipc_data = to_ipc_tables(data)
    
    # All values should be bytes
    assert all(isinstance(v, bytes) for v in ipc_data.values())
    
    # Round trip back (will be pandas or dict)
    recovered = from_ipc_tables(ipc_data)
    
    # Check structure is preserved
    assert set(recovered.keys()) == set(data.keys())


def test_validate_sim_return_dict():
    """Test validation of simulation return values."""
    # Valid dict should convert to IPC
    data = {"results": [1, 2, 3]}
    validated = validate_sim_return(data)
    assert isinstance(validated, dict)
    assert all(isinstance(v, bytes) for v in validated.values())
    
    # Already IPC bytes should pass through
    ipc_data = {"results": b"some_bytes"}
    validated = validate_sim_return(ipc_data)
    assert validated == ipc_data
    
    # Non-dict should raise
    with pytest.raises(TypeError, match="must return dict"):
        validate_sim_return([1, 2, 3])


def test_sim_services_return_bytes():
    """Test that simulation services return Mapping[str, bytes]."""
    from modelops.services.simulation import LocalSimulationService
    
    # Mock simulation function
    def mock_sim(params, seed):
        return {"output": [params["x"] * seed]}
    
    # Monkey-patch the import
    import sys
    import types
    mock_module = types.ModuleType("test_module")
    mock_module.mock_sim = mock_sim
    sys.modules["test_module"] = mock_module
    
    # Test LocalSimulationService
    service = LocalSimulationService()
    result = service.submit("test_module:mock_sim", {"x": 2}, 3, bundle_ref="")
    
    # Should be dict of bytes
    assert isinstance(result, dict)
    assert all(isinstance(v, bytes) for v in result.values())
    
    # Clean up
    del sys.modules["test_module"]