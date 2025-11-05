"""Tests for contract compliance."""

import pytest
from modelops.utils.seeds import derive_replicate_seeds, derive_single_seed
from modelops_contracts import TrialResult, TrialStatus, UniqueParameterSet, make_param_id


def test_runner_replicate_seeds_deterministic():
    """Test that replicate seeds are deterministic."""
    param_id = "test_param_123"

    # Generate seeds multiple times
    seeds1 = derive_replicate_seeds(param_id, 5)
    seeds2 = derive_replicate_seeds(param_id, 5)

    # Should be identical
    assert seeds1 == seeds2

    # Should have correct count
    assert len(seeds1) == 5

    # All should be in uint64 range
    for seed in seeds1:
        assert isinstance(seed, int)
        assert 0 <= seed < 2**64

    # Different param_id should give different seeds
    seeds3 = derive_replicate_seeds("different_param", 5)
    assert seeds1 != seeds3


def test_single_seed_derivation():
    """Test single seed derivation."""
    param_id = "test_param"

    # Single seed should match first of replicate seeds
    single = derive_single_seed(param_id, 0)
    multiple = derive_replicate_seeds(param_id, 1)
    assert single == multiple[0]

    # Different indices should give different seeds
    seed0 = derive_single_seed(param_id, 0)
    seed1 = derive_single_seed(param_id, 1)
    assert seed0 != seed1


def test_trialresult_invariants():
    """Test TrialResult contract invariants."""
    # Valid completed result
    result = TrialResult(
        param_id="test_123", loss=0.5, status=TrialStatus.COMPLETED, diagnostics={"metric": 0.95}
    )
    assert result.param_id == "test_123"
    assert result.loss == 0.5

    # Non-finite loss should fail for COMPLETED
    with pytest.raises(Exception, match="finite"):
        TrialResult(param_id="test", loss=float("inf"), status=TrialStatus.COMPLETED)

    # Non-finite loss is OK for FAILED
    result = TrialResult(param_id="test", loss=float("nan"), status=TrialStatus.FAILED)
    assert result.status == TrialStatus.FAILED

    # Empty param_id should fail
    with pytest.raises(Exception, match="param_id"):
        TrialResult(param_id="", loss=0.5, status=TrialStatus.COMPLETED)


def test_diagnostics_size_cap():
    """Test that diagnostics size is capped."""
    from modelops_contracts import MAX_DIAG_BYTES

    # Small diagnostics should work
    small_diag = {"key": "value"}
    result = TrialResult(param_id="test", loss=0.5, diagnostics=small_diag)
    assert result.diagnostics == small_diag

    # Large diagnostics should fail
    large_diag = {"data": "x" * (MAX_DIAG_BYTES + 1000)}
    with pytest.raises(Exception, match="too large"):
        TrialResult(param_id="test", loss=0.5, diagnostics=large_diag)


def test_param_id_stability():
    """Test that param_id generation is stable."""
    params = {"x": 1.5, "y": 2, "name": "test", "flag": True}

    # Generate ID multiple times
    id1 = make_param_id(params)
    id2 = make_param_id(params)

    # Should be identical
    assert id1 == id2

    # Should be hex string
    assert all(c in "0123456789abcdef" for c in id1)

    # Different params should give different IDs
    params2 = {"x": 1.5, "y": 3, "name": "test", "flag": True}
    id3 = make_param_id(params2)
    assert id1 != id3

    # Order shouldn't matter
    params_reordered = {"flag": True, "y": 2, "name": "test", "x": 1.5}
    id4 = make_param_id(params_reordered)
    assert id1 == id4


def test_unique_parameter_set():
    """Test UniqueParameterSet creation and validation."""
    params = {"x": 1.0, "y": 2}

    # Create with auto-generated ID
    ups = UniqueParameterSet.from_dict(params)
    assert ups.param_id == make_param_id(params)
    assert ups.params["x"] == 1.0
    assert ups.params["y"] == 2

    # Params should be immutable
    with pytest.raises(TypeError):
        ups.params["x"] = 2.0

    # Invalid parameter types should fail
    with pytest.raises(Exception):
        UniqueParameterSet.from_dict({"x": [1, 2, 3]})  # list not allowed

    # Non-finite values should fail
    with pytest.raises(Exception):
        UniqueParameterSet.from_dict({"x": float("inf")})
