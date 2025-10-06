"""Test fixtures for integration tests."""

import pytest
from pathlib import Path
from modelops.utils.test_bundle_digest import compute_test_bundle_digest, format_test_bundle_ref


@pytest.fixture(scope="session")
def test_bundle_ref():
    """Compute the test bundle digest dynamically.

    This fixture computes the digest of the test_bundle directory
    at test time, ensuring it works consistently across different
    environments (local dev, CI, etc).

    Returns:
        Bundle reference in format "sha256:xxxxx"
    """
    # Find the test_bundle directory relative to this file
    test_dir = Path(__file__).parent.parent.parent  # Go up to repo root
    test_bundle_path = test_dir / "examples" / "test_bundle"

    if not test_bundle_path.exists():
        raise RuntimeError(
            f"Test bundle not found at {test_bundle_path}. "
            "Make sure examples/test_bundle directory exists."
        )

    # Compute deterministic digest
    digest = compute_test_bundle_digest(test_bundle_path)

    # Format as bundle reference
    return format_test_bundle_ref(digest)