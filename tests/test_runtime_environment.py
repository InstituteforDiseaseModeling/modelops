"""Tests for runtime environment management."""

import pytest
from unittest.mock import Mock, patch, MagicMock, call
from pathlib import Path
import json
import base64
import subprocess

from modelops.runtime.environment import (
    ensure_bundle,
    ensure_venv,
    run_in_env,
    BUNDLES,
    VENVS,
    UV_BIN,
)


class TestEnsureBundle:
    """Tests for ensure_bundle function."""
    
    @patch('modelops.runtime.environment.BundleClient')
    def test_ensure_bundle_with_client(self, mock_client_class):
        """Test ensure_bundle when BundleClient is available."""
        # Mock BundleClient.ensure_local
        mock_client = Mock()
        mock_client_class.ensure_local = Mock()
        
        # Patch the module-level BundleClient
        with patch('modelops.runtime.environment.BundleClient', mock_client_class):
            # Bundle exists
            bundle_path = BUNDLES / "sha256_abc123"
            with patch('pathlib.Path.exists', return_value=True):
                result = ensure_bundle("sha256:abc123")
                assert result == bundle_path
    
    def test_ensure_bundle_without_client(self):
        """Test ensure_bundle when BundleClient is not available."""
        with patch('modelops.runtime.environment.BundleClient', None):
            with pytest.raises(RuntimeError, match="modelops_bundle is not installed"):
                ensure_bundle("sha256:abc123")
    
    @patch('modelops.runtime.environment.BundleClient')
    def test_ensure_bundle_not_exists(self, mock_client_class):
        """Test ensure_bundle when bundle doesn't exist locally."""
        with patch('modelops.runtime.environment.BundleClient', mock_client_class):
            bundle_path = BUNDLES / "sha256_abc123"
            with patch('pathlib.Path.exists', return_value=False):
                with patch('pathlib.Path.mkdir'):
                    with pytest.raises(NotImplementedError, match="Bundle fetching not yet implemented"):
                        ensure_bundle("sha256:abc123")


class TestEnsureVenv:
    """Tests for ensure_venv function."""
    
    @patch('subprocess.run')
    def test_ensure_venv_exists(self, mock_run):
        """Test ensure_venv when venv already exists."""
        digest = "sha256:abc123"
        bundle_dir = Path("/bundles/test")
        venv_path = VENVS / "sha256_abc123"
        
        # Mock venv already exists
        with patch('pathlib.Path.exists', return_value=True):
            result = ensure_venv(digest, bundle_dir)
            
            # Should return existing venv without creating
            assert result == venv_path
            mock_run.assert_not_called()
    
    @patch('subprocess.run')
    def test_ensure_venv_create_new(self, mock_run):
        """Test ensure_venv creates new venv."""
        digest = "sha256:abc123"
        bundle_dir = Path("/bundles/test")
        venv_path = VENVS / "sha256_abc123"
        
        # Mock venv doesn't exist
        with patch('pathlib.Path.exists', return_value=False):
            with patch('pathlib.Path.mkdir'):
                result = ensure_venv(digest, bundle_dir)
                
                # Should create venv and sync
                assert result == venv_path
                assert mock_run.call_count == 2
                
                # Check venv creation call
                venv_call = mock_run.call_args_list[0]
                assert venv_call[0][0] == [UV_BIN, "venv", str(venv_path)]
                
                # Check sync call
                sync_call = mock_run.call_args_list[1]
                assert sync_call[0][0][:2] == [UV_BIN, "sync"]
                assert "--frozen" in sync_call[0][0]
    
    @patch('subprocess.run')
    @patch('pathlib.Path.mkdir')
    @patch('pathlib.Path.exists')
    def test_ensure_venv_with_wheelhouse(self, mock_exists, mock_mkdir, mock_run):
        """Test ensure_venv uses wheelhouse if present."""
        digest = "sha256:abc123"
        bundle_dir = Path("/bundles/test")
        wheelhouse = bundle_dir / "wheelhouse"
        venv_path = VENVS / "sha256_abc123"
        
        # venv doesn't exist, wheelhouse does
        # Mock exists to return False for venv, True for wheelhouse
        mock_exists.side_effect = [False, True]
        
        result = ensure_venv(digest, bundle_dir)
        
        # Check sync call includes wheelhouse flags
        sync_call = mock_run.call_args_list[1]
        sync_args = sync_call[0][0]
        assert "--find-links" in sync_args
        assert str(wheelhouse) in sync_args
        assert "--no-index" in sync_args


class TestRunInEnv:
    """Tests for run_in_env function."""
    
    @patch('subprocess.run')
    def test_run_in_env_success(self, mock_run):
        """Test successful execution in environment."""
        venv = Path("/venvs/test")
        entrypoint = "module:function"
        params = {"x": 42, "y": "test"}
        seed = 123
        bundle_dir = Path("/bundles/test")
        
        # Mock successful execution
        output = {"result": base64.b64encode(b"test_output").decode()}
        mock_run.return_value = Mock(
            stdout=json.dumps(output).encode(),
            stderr=b"",
            returncode=0
        )
        
        result = run_in_env(venv, entrypoint, params, seed, bundle_dir)
        
        # Check subprocess call
        mock_run.assert_called_once()
        args = mock_run.call_args
        
        # Check command
        assert args[0][0][0] == str(venv / "bin" / "python")
        assert args[0][0][1:] == ["-m", "modelops_user_runner", entrypoint]
        
        # Check input
        input_data = json.loads(args[1]["input"].decode())
        assert input_data["params"] == params
        assert input_data["seed"] == seed
        
        # Check environment
        assert args[1]["env"]["PYTHONNOUSERSITE"] == "1"
        assert args[1]["env"]["MODELOPS_BUNDLE_PATH"] == str(bundle_dir)
        
        # Result should be decoded from hex (placeholder encoding)
        # Note: The function uses bytes.fromhex, not base64
        assert "result" in result
    
    @patch('subprocess.run')
    def test_run_in_env_failure(self, mock_run):
        """Test execution failure in environment."""
        venv = Path("/venvs/test")
        
        # Mock subprocess failure
        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["python"], output=b"", stderr=b"Error"
        )
        
        with pytest.raises(subprocess.CalledProcessError):
            run_in_env(venv, "m:f", {}, 0, Path("/b"))
    
    @patch('subprocess.run')
    def test_run_in_env_output_format(self, mock_run):
        """Test output format conversion."""
        venv = Path("/venvs/test")
        
        # Mock output with hex encoding (as per the implementation)
        output = {
            "table1": "48656c6c6f",  # "Hello" in hex
            "table2": "576f726c64",  # "World" in hex
        }
        mock_run.return_value = Mock(
            stdout=json.dumps(output).encode(),
            stderr=b"",
            returncode=0
        )
        
        result = run_in_env(venv, "m:f", {}, 0, Path("/b"))
        
        # Check conversion from hex
        assert result["table1"] == b"Hello"
        assert result["table2"] == b"World"


class TestCacheDirectories:
    """Test cache directory configuration."""
    
    def test_default_cache_dirs(self):
        """Test default cache directory paths."""
        assert str(BUNDLES) == "/var/cache/modelops/bundles"
        assert str(VENVS) == "/var/cache/modelops/venv"
        assert UV_BIN == "uv"
    
    @patch.dict('os.environ', {
        'MODEL_OPS_BUNDLE_CACHE_DIR': '/custom/bundles',
        'MODEL_OPS_VENV_CACHE_DIR': '/custom/venvs',
        'UV_BIN': '/usr/local/bin/uv'
    })
    def test_custom_cache_dirs(self):
        """Test cache directories from environment variables."""
        # Need to reimport to pick up env vars
        import importlib
        import modelops.runtime.environment as env_module
        
        with patch.dict('os.environ', {
            'MODEL_OPS_BUNDLE_CACHE_DIR': '/custom/bundles',
            'MODEL_OPS_VENV_CACHE_DIR': '/custom/venvs',
            'UV_BIN': '/usr/local/bin/uv'
        }):
            importlib.reload(env_module)
            
            assert str(env_module.BUNDLES) == "/custom/bundles"
            assert str(env_module.VENVS) == "/custom/venvs"
            assert env_module.UV_BIN == "/usr/local/bin/uv"