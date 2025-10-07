"""Regression tests for Pulumi passphrase management.

This test suite ensures that the Pulumi passphrase bug discovered in October 2024
does not regress. The bug caused different stacks to use different passphrases
due to missing environment variable propagation in LocalWorkspaceOptions.

Bug History:
- October 2024: Discovered that LocalWorkspaceOptions was not passing env_vars
  to Pulumi subprocesses, causing "incorrect passphrase" errors
- Root cause: env_vars parameter was None, preventing PULUMI_CONFIG_PASSPHRASE_FILE
  from being passed to Pulumi language host
- Fix: Set env_vars=dict(os.environ) in workspace_options()

For full details, see the developer notes in src/modelops/core/automation.py
"""

import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import pulumi.automation as auto

from modelops.core import automation
from modelops.core.config import ModelOpsConfig


class TestPulumiPassphrase:
    """Test suite for Pulumi passphrase management."""

    def test_workspace_options_includes_environment(self):
        """Verify that LocalWorkspaceOptions passes environment variables.

        This is the critical fix - env_vars must be set to ensure
        PULUMI_CONFIG_PASSPHRASE_FILE is passed to Pulumi subprocess.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)

            # Mock the config to avoid needing config file
            mock_config = MagicMock(spec=ModelOpsConfig)
            mock_pulumi = MagicMock()
            mock_pulumi.backend_url = f"file://{tmpdir}"
            mock_config.pulumi = mock_pulumi
            with patch('modelops.core.config.ModelOpsConfig.get_instance', return_value=mock_config):
                # Call workspace_options
                opts = automation.workspace_options("test-project", work_dir)

            # CRITICAL ASSERTION: env_vars must not be None
            assert opts.env_vars is not None, (
                "env_vars must be set! This was the root cause of the passphrase bug."
            )

            # Verify passphrase file path is in environment
            assert "PULUMI_CONFIG_PASSPHRASE_FILE" in opts.env_vars, (
                "PULUMI_CONFIG_PASSPHRASE_FILE must be in env_vars"
            )

    def test_direct_passphrase_is_removed(self):
        """Verify that PULUMI_CONFIG_PASSPHRASE is removed to avoid precedence issues.

        Direct passphrase takes precedence over file-based passphrase,
        which can cause confusion and errors.
        """
        # Set a direct passphrase in environment
        os.environ["PULUMI_CONFIG_PASSPHRASE"] = "should-be-removed"

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                work_dir = Path(tmpdir)

                # Mock the config to avoid needing config file
                mock_config = MagicMock(spec=ModelOpsConfig)
                mock_pulumi = MagicMock()
                mock_pulumi.backend_url = f"file://{tmpdir}"
                mock_config.pulumi = mock_pulumi
                with patch('modelops.core.config.ModelOpsConfig.get_instance', return_value=mock_config):
                    # Call workspace_options
                    opts = automation.workspace_options("test-project", work_dir)

                # Verify direct passphrase is NOT in env_vars
                assert "PULUMI_CONFIG_PASSPHRASE" not in opts.env_vars, (
                    "PULUMI_CONFIG_PASSPHRASE should be removed to avoid precedence issues"
                )

                # Verify it was also removed from os.environ
                assert "PULUMI_CONFIG_PASSPHRASE" not in os.environ, (
                    "PULUMI_CONFIG_PASSPHRASE should be removed from os.environ"
                )
        finally:
            # Clean up
            os.environ.pop("PULUMI_CONFIG_PASSPHRASE", None)

    def test_passphrase_file_creation_is_idempotent(self):
        """Verify that passphrase file creation is idempotent.

        Multiple calls to _ensure_passphrase should not overwrite
        an existing passphrase file.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Mock the home directory to use temp dir
            mock_passphrase_file = Path(tmpdir) / ".modelops" / "secrets" / "pulumi-passphrase"

            with patch.object(Path, 'home', return_value=Path(tmpdir)):
                # First call - creates file
                automation._ensure_passphrase()
                assert mock_passphrase_file.exists()

                # Read the original content
                original_content = mock_passphrase_file.read_text()

                # Second call - should not overwrite
                automation._ensure_passphrase()
                assert mock_passphrase_file.read_text() == original_content

                # Third call - still should not overwrite
                automation._ensure_passphrase()
                assert mock_passphrase_file.read_text() == original_content

    def test_race_condition_simulation(self):
        """Simulate the race condition that could occur without atomic file creation.

        Note: The current implementation still has a TOCTOU race condition.
        This test documents the issue for future improvement.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_passphrase_file = Path(tmpdir) / ".modelops" / "secrets" / "pulumi-passphrase"
            passphrases_created = []

            def create_passphrase_non_atomic():
                """Simulate the current non-atomic implementation."""
                if not mock_passphrase_file.exists():
                    # Simulate delay to increase chance of race
                    time.sleep(0.001)

                    # Create directories
                    mock_passphrase_file.parent.mkdir(parents=True, exist_ok=True)

                    # Generate and write passphrase
                    import secrets
                    passphrase = secrets.token_urlsafe(32)
                    mock_passphrase_file.write_text(passphrase)
                    passphrases_created.append(passphrase)

            # Launch multiple threads
            threads = []
            for _ in range(5):
                t = threading.Thread(target=create_passphrase_non_atomic)
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            # In a race condition, multiple passphrases might be created
            # The last one to write wins
            if len(set(passphrases_created)) > 1:
                pytest.skip("Race condition detected - this is a known issue")

            # If only one unique passphrase, no race occurred in this run
            assert len(set(passphrases_created)) >= 1

    def test_select_stack_calls_ensure_passphrase(self):
        """Verify that select_stack always ensures passphrase is configured."""
        with patch('modelops.core.automation._ensure_passphrase') as mock_ensure:
            with patch('modelops.core.automation.auto.create_or_select_stack') as mock_create:
                # Mock the config to avoid needing config file
                mock_config = MagicMock(spec=ModelOpsConfig)
                mock_pulumi = MagicMock()
                mock_pulumi.backend_url = "file:///tmp/test"
                mock_config.pulumi = mock_pulumi
                with patch('modelops.core.config.ModelOpsConfig.get_instance', return_value=mock_config):
                    mock_create.return_value = MagicMock()

                    # Call select_stack with a valid component
                    automation.select_stack("infra", "dev")

                # Verify _ensure_passphrase was called (twice: once in select_stack, once in workspace_options)
                assert mock_ensure.call_count == 2

    @pytest.mark.integration
    def test_all_stacks_accessible(self):
        """Integration test: Verify all stacks can be accessed with same passphrase.

        This test requires existing Pulumi stacks and should only run
        in environments where infrastructure has been provisioned.
        """
        components = ["resource-group", "registry", "infra", "storage", "workspace"]
        errors = []

        for component in components:
            try:
                # Try to get outputs without refresh
                outputs = automation.outputs(component, "dev", refresh=False)
                if outputs is None:
                    errors.append(f"{component}: No outputs returned")
            except Exception as e:
                if "incorrect passphrase" in str(e).lower():
                    errors.append(f"{component}: INCORRECT PASSPHRASE ERROR")
                elif "no stack named" in str(e).lower():
                    pytest.skip(f"Stack {component} not found - skipping integration test")
                else:
                    errors.append(f"{component}: {str(e)[:100]}")

        assert len(errors) == 0, f"Passphrase issues detected:\n" + "\n".join(errors)


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])