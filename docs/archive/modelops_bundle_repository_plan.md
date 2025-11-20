# ModelOpsBundleRepository Implementation Plan

## Overview

Workers need to fetch bundles from the registry to execute simulations. The `ModelOpsBundleRepository` will be the adapter that implements the `BundleRepository` protocol from modelops-contracts, using the modelops-bundle package to handle OCI registry operations.

## Current State

### The Gap

In `modelops/worker/plugin.py:122`:
```python
if config.bundle_source == 'oci':
    # Use entry point discovery for OCI bundle repositories
    from importlib.metadata import entry_points

    # Discover OCI bundle repository via entry points
    eps = entry_points(group="modelops.bundle_repos")
    oci_plugin = None
    for ep in eps:
        if ep.name == "oci":  # Looking for 'oci' entry point
            oci_plugin = ep
            break

    if not oci_plugin:
        raise ValueError(
            "No OCI bundle repository plugin found. "
            "Ensure modelops-bundle is installed with the 'oci' entry point."
        )

    # Load and instantiate the OCI repository
    # TODOHERE
    repo_class = oci_plugin.load()
    return repo_class(
        registry_ref=config.bundle_registry,
        cache_dir=str(Path(config.bundles_cache_dir)),
        cache_structure="digest_short",
        default_tag="latest"
    )
```

### The Protocol to Implement

From `modelops_contracts/ports.py`:
```python
@runtime_checkable
class BundleRepository(Protocol):
    """Repository for fetching and caching bundles."""

    def ensure_local(self, bundle_ref: str) -> Tuple[str, Path]:
        """Ensure bundle is available locally.

        Args:
            bundle_ref: Bundle reference (e.g., sha256:...)

        Returns:
            Tuple of (digest, local_path)
        """
        ...

    def exists(self, bundle_ref: str) -> bool:
        """Check if bundle exists in repository."""
        ...
```

## Implementation Design

### 1. Location: modelops-bundle Package

Create `src/modelops_bundle/repository.py`:

```python
"""ModelOps Bundle Repository for worker bundle fetching."""

from pathlib import Path
from typing import Tuple
import logging

from modelops_contracts.ports import BundleRepository

from .client import BundleClient
from .core.cache import BundleCache
from .core.references import parse_reference

logger = logging.getLogger(__name__)


class ModelOpsBundleRepository:
    """Bundle repository implementation for ModelOps workers.

    This is the adapter that workers use to fetch bundles from
    OCI registries. It implements the BundleRepository protocol
    from modelops-contracts.

    Key responsibilities:
    - Fetch bundles from OCI registry
    - Cache bundles locally for reuse
    - Verify bundle integrity
    - Handle authentication
    """

    def __init__(
        self,
        registry_ref: str,
        cache_dir: str,
        cache_structure: str = "digest_short",
        default_tag: str = "latest",
        insecure: bool = False
    ):
        """Initialize repository with registry connection.

        Args:
            registry_ref: Registry URL (e.g., ghcr.io/org/models)
            cache_dir: Local cache directory for bundles
            cache_structure: How to organize cache ("digest_short", "digest_full", "name")
            default_tag: Default tag if not specified in ref
            insecure: Whether to use insecure HTTP (for local dev)
        """
        self.registry_ref = registry_ref
        self.cache_dir = Path(cache_dir)
        self.cache_structure = cache_structure
        self.default_tag = default_tag

        # Initialize bundle client for OCI operations
        self.client = BundleClient.from_registry_url(
            registry_ref,
            cache_dir=cache_dir,
            insecure=insecure
        )

        # Initialize cache
        self.cache = BundleCache(
            cache_dir=self.cache_dir,
            structure=cache_structure
        )

    def ensure_local(self, bundle_ref: str) -> Tuple[str, Path]:
        """Ensure bundle is available locally.

        This is called by workers to get a bundle. It will:
        1. Check if bundle is in cache
        2. If not, pull from registry
        3. Extract to local directory
        4. Return path to extracted bundle

        Args:
            bundle_ref: Bundle reference (sha256:64-hex-chars)

        Returns:
            Tuple of (digest, local_path_to_bundle)

        Raises:
            BundleNotFoundError: If bundle doesn't exist
            BundleIntegrityError: If bundle fails verification
        """
        # Parse the reference
        if not bundle_ref.startswith("sha256:"):
            raise ValueError(f"Bundle ref must be sha256 digest, got: {bundle_ref}")

        digest = bundle_ref.split(":", 1)[1]

        # Check cache first
        cached_path = self.cache.get_bundle_path(digest)
        if cached_path and cached_path.exists():
            logger.debug(f"Bundle {digest[:12]} found in cache at {cached_path}")
            return bundle_ref, cached_path

        # Not in cache, need to pull
        logger.info(f"Pulling bundle {digest[:12]} from registry")

        # Pull bundle (this also extracts it)
        local_path = self.client.pull_by_digest(digest)

        # Verify we got what we expected
        # The client should have already verified, but double-check
        if not local_path.exists():
            raise RuntimeError(f"Bundle pull succeeded but path doesn't exist: {local_path}")

        # Update cache tracking
        self.cache.add_bundle(digest, local_path)

        return bundle_ref, local_path

    def exists(self, bundle_ref: str) -> bool:
        """Check if bundle exists in repository.

        Args:
            bundle_ref: Bundle reference to check

        Returns:
            True if bundle exists in registry or cache
        """
        # Parse the reference
        if not bundle_ref.startswith("sha256:"):
            return False

        digest = bundle_ref.split(":", 1)[1]

        # Check cache first (fast)
        if self.cache.has_bundle(digest):
            return True

        # Check registry (slower)
        try:
            return self.client.manifest_exists(digest)
        except Exception as e:
            logger.warning(f"Error checking bundle existence: {e}")
            return False
```

### 2. Entry Point Registration

In `modelops-bundle/pyproject.toml`:
```toml
[project.entry-points."modelops.bundle_repos"]
modelops_bundle = "modelops_bundle.repository:ModelOpsBundleRepository"  # Alternative name
```

### 3. Integration with Existing modelops-bundle

We need to ensure the following components exist or are created:

#### BundleClient Updates
```python
class BundleClient:
    """Main client for bundle operations."""

    @classmethod
    def from_registry_url(cls, registry_url: str, **kwargs):
        """Create client from registry URL.

        This factory method is for workers who just have a registry URL,
        not a full bundle project.
        """
        # Parse registry URL to get config
        # Create minimal client for pull operations
        pass

    def pull_by_digest(self, digest: str) -> Path:
        """Pull bundle by digest only.

        Workers use digests, not tags.
        """
        # Implement pull logic
        # Extract to cache directory
        # Return path to extracted bundle
        pass

    def manifest_exists(self, digest: str) -> bool:
        """Check if manifest exists by digest."""
        pass
```

#### BundleCache Helper
```python
class BundleCache:
    """Manage local bundle cache for workers."""

    def __init__(self, cache_dir: Path, structure: str = "digest_short"):
        self.cache_dir = cache_dir
        self.structure = structure
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_bundle_path(self, digest: str) -> Optional[Path]:
        """Get path to cached bundle if it exists."""
        if self.structure == "digest_short":
            path = self.cache_dir / digest[:12]
        elif self.structure == "digest_full":
            path = self.cache_dir / digest
        else:
            # name structure not applicable for digest-only refs
            path = self.cache_dir / digest[:12]

        return path if path.exists() else None

    def has_bundle(self, digest: str) -> bool:
        """Check if bundle is in cache."""
        return self.get_bundle_path(digest) is not None

    def add_bundle(self, digest: str, path: Path):
        """Record bundle in cache."""
        # Could maintain an index file for faster lookups
        pass
```

## Testing Strategy

### Unit Tests

1. **Mock Registry Tests**
   - Test `ensure_local` with cached bundle
   - Test `ensure_local` with registry pull
   - Test `exists` with various scenarios

2. **Integration Tests**
   - Test with local Docker registry
   - Test with real bundle push/pull cycle
   - Test cache behavior

3. **Worker Integration Test**
   ```python
   def test_worker_can_fetch_bundle():
       # Start local registry
       # Push test bundle
       # Create worker with ModelOpsBundleRepository
       # Execute task requiring bundle
       # Verify bundle was fetched and used
   ```

## Implementation Steps

### Phase 1: Basic Implementation (Week 1)
1. Create `ModelOpsBundleRepository` class in modelops-bundle
2. Add entry point registration
3. Implement `ensure_local` with basic pull logic
4. Implement `exists` check

### Phase 2: Integration (Week 2)
1. Update BundleClient with `from_registry_url` factory
2. Implement `pull_by_digest` method
3. Add BundleCache helper class
4. Test with modelops worker plugin

### Phase 3: Production Hardening (Week 3)
1. Add retry logic for network failures
2. Implement cache eviction policies
3. Add metrics and logging
4. Handle authentication properly
5. Add integrity verification

## Configuration

Workers will configure the repository via environment:

```bash
# For OCI registry bundles
MODELOPS_BUNDLE_SOURCE=oci
MODELOPS_BUNDLE_REGISTRY=ghcr.io/org/models
MODELOPS_BUNDLES_CACHE_DIR=/tmp/modelops/bundle-cache
MODELOPS_BUNDLE_INSECURE=false  # For local dev

# For file-based bundles (existing)
MODELOPS_BUNDLE_SOURCE=file
MODELOPS_BUNDLES_DIR=/path/to/bundles
```

## Success Criteria

1. ✅ Workers can fetch bundles using only digest references
2. ✅ Bundles are cached locally for reuse
3. ✅ Entry point discovery works (`modelops.bundle_repos`)
4. ✅ Integration tests pass with real registry
5. ✅ No changes needed to worker code (just install modelops-bundle)

## Next Steps After Implementation

1. Test end-to-end: Calabaria → SimulationStudy → SimJob → Worker → Bundle fetch → Execute
2. Add observability: metrics for cache hit rate, pull latency
3. Optimize: parallel pulls, compression, deduplication
4. Security: signed bundles, attestations
