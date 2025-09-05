"""Tests for simulation runner implementations."""

import pytest
from unittest.mock import Mock, patch, MagicMock
import os
from pathlib import Path

from modelops.runtime.runners import (
    DirectRunner,
    BundleRunner,
    CachedBundleRunner,
    get_runner,
)


def sample_simulation_func(params: dict, seed: int) -> dict:
    """Sample simulation function for DirectRunner tests."""
    return {
        "result": f"x={params.get('x', 0)},seed={seed}".encode(),
        "value": str(params.get('x', 0) * seed).encode(),
    }


class TestDirectRunner:
    """Tests for DirectRunner."""
    
    def test_direct_runner_simple(self):
        """Test DirectRunner with a simple function."""
        runner = DirectRunner()
        
        # Run with test function in this module
        result = runner.run(
            "tests.test_runners:sample_simulation_func",
            {"x": 42},
            seed=123,
            bundle_ref=""
        )
        
        # Check result structure
        assert isinstance(result, dict)
        assert "result" in result
        assert "value" in result
        assert result["result"] == b"x=42,seed=123"
        assert result["value"] == b"5166"  # 42 * 123
    
    def test_direct_runner_missing_module(self):
        """Test DirectRunner with missing module."""
        runner = DirectRunner()
        
        with pytest.raises(ImportError):
            runner.run(
                "nonexistent.module:func",
                {},
                seed=0,
                bundle_ref=""
            )
    
    def test_direct_runner_missing_function(self):
        """Test DirectRunner with missing function."""
        runner = DirectRunner()
        
        with pytest.raises(AttributeError):
            runner.run(
                "tests.test_runners:nonexistent_func",
                {},
                seed=0,
                bundle_ref=""
            )


class TestBundleRunner:
    """Tests for BundleRunner."""
    
    @patch('modelops.runtime.runners.ensure_bundle')
    @patch('modelops.runtime.runners.ensure_venv')
    @patch('modelops.runtime.runners.run_in_env')
    def test_bundle_runner_basic(self, mock_run, mock_venv, mock_bundle):
        """Test BundleRunner with mocked operations."""
        # Setup mocks
        mock_bundle.return_value = Path("/cache/bundles/sha256_abc")
        mock_venv.return_value = Path("/cache/venvs/sha256_abc")
        mock_run.return_value = {"result": b"test_output"}
        
        runner = BundleRunner()
        
        # Run simulation
        result = runner.run(
            "module:func",
            {"x": 1},
            seed=42,
            bundle_ref="sha256:abc123"
        )
        
        # Verify calls
        mock_bundle.assert_called_once_with("sha256:abc123")
        mock_venv.assert_called_once_with(
            "sha256:abc123",
            Path("/cache/bundles/sha256_abc")
        )
        mock_run.assert_called_once()
        
        assert result == {"result": b"test_output"}
    
    def test_bundle_runner_requires_ref(self):
        """Test BundleRunner requires bundle_ref."""
        runner = BundleRunner()
        
        with pytest.raises(ValueError, match="requires a bundle_ref"):
            runner.run("module:func", {}, seed=0, bundle_ref="")


class TestCachedBundleRunner:
    """Tests for CachedBundleRunner."""
    
    @patch('modelops.runtime.runners.ensure_bundle')
    @patch('modelops.runtime.runners.ensure_venv')
    @patch('modelops.runtime.runners.run_in_env')
    def test_cached_runner_caching(self, mock_run, mock_venv, mock_bundle):
        """Test CachedBundleRunner caches environments."""
        # Setup mocks
        mock_bundle.return_value = Path("/cache/bundles/sha256_abc")
        mock_venv.return_value = Path("/cache/venvs/sha256_abc")
        mock_run.return_value = {"result": b"cached"}
        
        runner = CachedBundleRunner(max_cache_size=2)
        
        # First run - should create cache entries
        result1 = runner.run(
            "module:func", {"x": 1}, seed=1, bundle_ref="sha256:abc"
        )
        
        # Second run with same bundle - should use cache
        result2 = runner.run(
            "module:func", {"x": 2}, seed=2, bundle_ref="sha256:abc"
        )
        
        # Bundle and venv should only be called once (cached)
        assert mock_bundle.call_count == 1
        assert mock_venv.call_count == 1
        # But run_in_env should be called twice (different params)
        assert mock_run.call_count == 2
        
        assert result1 == {"result": b"cached"}
        assert result2 == {"result": b"cached"}
    
    @patch('modelops.runtime.runners.ensure_bundle')
    @patch('modelops.runtime.runners.ensure_venv')
    @patch('modelops.runtime.runners.run_in_env')
    def test_cached_runner_eviction(self, mock_run, mock_venv, mock_bundle):
        """Test cache eviction when size exceeded."""
        mock_bundle.side_effect = lambda d: Path(f"/cache/bundles/{d}")
        mock_venv.side_effect = lambda d, p: Path(f"/cache/venvs/{d}")
        mock_run.return_value = {"result": b"test"}
        
        runner = CachedBundleRunner(max_cache_size=2)
        
        # Fill cache
        runner.run("m:f", {}, 0, bundle_ref="sha256:aaa")
        runner.run("m:f", {}, 0, bundle_ref="sha256:bbb")
        
        # This should evict the first entry
        runner.run("m:f", {}, 0, bundle_ref="sha256:ccc")
        
        # Check that aaa is no longer in cache
        assert "sha256:aaa" not in runner._bundle_cache
        assert "sha256:bbb" in runner._bundle_cache
        assert "sha256:ccc" in runner._bundle_cache
    
    def test_cached_runner_clear_cache(self):
        """Test clearing the cache."""
        runner = CachedBundleRunner()
        runner._bundle_cache = {"test": Path("/test")}
        runner._venv_cache = {"test": Path("/test/venv")}
        
        runner.clear_cache()
        
        assert len(runner._bundle_cache) == 0
        assert len(runner._venv_cache) == 0


class TestGetRunner:
    """Tests for get_runner factory function."""
    
    def test_get_runner_direct(self):
        """Test get_runner returns DirectRunner."""
        runner = get_runner("direct")
        assert isinstance(runner, DirectRunner)
    
    def test_get_runner_bundle(self):
        """Test get_runner returns BundleRunner."""
        runner = get_runner("bundle")
        assert isinstance(runner, BundleRunner)
    
    def test_get_runner_cached(self):
        """Test get_runner returns CachedBundleRunner."""
        runner = get_runner("cached")
        assert isinstance(runner, CachedBundleRunner)
    
    def test_get_runner_from_env(self):
        """Test get_runner uses environment variable."""
        with patch.dict(os.environ, {"MODELOPS_RUNNER_TYPE": "bundle"}):
            runner = get_runner()
            assert isinstance(runner, BundleRunner)
    
    def test_get_runner_default(self):
        """Test get_runner defaults to DirectRunner."""
        with patch.dict(os.environ, {}, clear=True):
            runner = get_runner()
            assert isinstance(runner, DirectRunner)
    
    def test_get_runner_invalid(self):
        """Test get_runner with invalid type."""
        with pytest.raises(ValueError, match="Unknown runner type"):
            get_runner("invalid")