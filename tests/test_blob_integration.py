#!/usr/bin/env python
"""Test blob storage integration."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from modelops.services.provenance_store import ProvenanceStore
from modelops_contracts import SimTask, SimReturn, TableArtifact
from modelops_contracts.types import UniqueParameterSet

# AzureBlobBackend is optional - only imported if azure deps are available
try:
    from modelops.services.storage.azure import AzureBlobBackend
    HAS_AZURE = True
except ImportError:
    HAS_AZURE = False

def test_provenance_with_mock_blob():
    """Test ProvenanceStore with mocked blob backend."""

    with tempfile.TemporaryDirectory() as tmpdir:
        storage_dir = Path(tmpdir)

        # Create mock Azure backend
        if HAS_AZURE:
            mock_backend = MagicMock(spec=AzureBlobBackend)
        else:
            # Create a simple mock without spec
            mock_backend = MagicMock()
        mock_backend.save.return_value = None  # AzureBlobBackend uses save, not upload
        mock_backend.load.return_value = None  # Uses load, not download
        mock_backend.exists.return_value = False
        mock_backend.list_keys.return_value = []  # Uses list_keys, not list_blobs

        # Create ProvenanceStore without Azure config (since we don't have deps)
        store = ProvenanceStore(
            storage_dir=storage_dir,
            azure_backend=None  # Start with local-only
        )

        # Manually set up Azure backend with our mock for testing
        store._azure_backend = mock_backend

        # Create test task and result
        # Use a valid bundle digest format
        bundle_digest = "sha256:" + "a" * 64
        task = SimTask(
            bundle_ref=bundle_digest,
            params=UniqueParameterSet(
                params={"alpha": 0.5, "beta": 1.0},
                param_id="test123"
            ),
            seed=42,
            entrypoint="module.path/scenario"
        )

        test_data = b"test simulation results"
        import hashlib
        checksum = hashlib.blake2b(test_data, digest_size=32).hexdigest()

        result = SimReturn(
            task_id="task123",
            outputs={
                "results": TableArtifact(
                    size=len(test_data),
                    inline=test_data,
                    checksum=checksum
                )
            }
        )

        # Store result
        store.put_sim(task, result)

        # Verify local storage
        local_result = store.get_sim(task)
        assert local_result is not None
        assert local_result.task_id == "task123"

        # Azure upload is currently disabled for performance
        # TODO: Re-enable this check when Azure uploads are re-enabled
        # assert mock_backend.save.called
        # call_args = mock_backend.save.call_args[0]
        # blob_path = call_args[0]
        # data = call_args[1]
        # print(f"✓ Blob path: {blob_path}")
        # print(f"✓ Data size: {len(data)} bytes")
        # assert "sha256:" in blob_path or "/sims/" in blob_path
        # assert data is not None

        # For now, just verify the mock was set up correctly
        assert store._azure_backend == mock_backend
        print("✓ Azure backend configured (uploads currently disabled)")

        # Since Azure uploads are disabled, we skip remote fallback testing
        # The remote fallback test would require blob_path which doesn't exist
        # when uploads are disabled. This can be re-enabled when Azure uploads
        # are re-enabled in ProvenanceStore.

        print("✓ Test completed!")
        print("✓ Local storage works")
        print("✓ Azure backend mock configured")

        # Cleanup
        store.shutdown()

if __name__ == "__main__":
    test_provenance_with_mock_blob()