"""Tests for warm process timeout handling."""

from pathlib import Path

import pytest

from modelops.worker.process_manager import WarmProcessManager
from modelops.utils.test_bundle_digest import compute_test_bundle_digest, format_test_bundle_ref


def _hung_bundle_path() -> Path:
    return Path(__file__).resolve().parent.parent / "examples" / "hung_bundle"


@pytest.mark.timeout(60)
def test_execute_task_times_out_and_evicts_process(tmp_path):
    """Hung wire functions should trigger TimeoutError and process eviction."""
    bundle_path = _hung_bundle_path()
    digest = format_test_bundle_ref(compute_test_bundle_digest(bundle_path))

    manager = WarmProcessManager(
        max_processes=1,
        venvs_dir=tmp_path / "venvs",
        force_fresh_venv=True,
        rpc_timeout_seconds=2,
    )

    with pytest.raises(TimeoutError):
        manager.execute_task(
            bundle_digest=digest,
            bundle_path=bundle_path,
            entrypoint="hung_bundle/block",
            params={},
            seed=123,
        )

    assert not manager._processes, "Hung process should be evicted after timeout"
