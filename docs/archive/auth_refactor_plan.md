# Authentication Architecture Refactor Plan

## Executive Summary
Fix the broken authentication architecture between modelops, modelops-bundle, and modelops-contracts. Currently, modelops-bundle CLI is broken due to missing auth_providers module, and the integration between modelops and modelops-bundle has a constructor mismatch.

## Current Problems

### 1. Broken Imports
- `modelops-bundle/src/modelops_bundle/ops.py:25` imports non-existent `from .auth_providers import get_auth_provider`
- `modelops-bundle/src/modelops_bundle/bundle_service.py:35` imports non-existent `from .auth_providers import get_auth_provider`
- `modelops-bundle/src/modelops_bundle/cli.py:26` imports non-existent `from .auth_providers import get_auth_provider`

### 2. Constructor Mismatch
- `modelops/src/modelops/client/job_submission.py:246` tries to pass `auth_provider` to BundleService
- But `BundleService.__init__` expects `BundleDeps` object, not `auth_provider`

### 3. Duplicate Code
- `AuthError` exception defined in both:
  - `modelops/src/modelops/errors.py`
  - `modelops-bundle/src/modelops_bundle/errors.py`

### 4. Azure-Specific Code in modelops-bundle
- `modelops-bundle/src/modelops_bundle/storage/azure.py` - Azure blob implementation (this is OK, storage is different from auth)
- Storage factory checks `AZURE_STORAGE_CONNECTION_STRING` environment variable

## Proposed Architecture

### Design Principles
1. **modelops** → **modelops-bundle** dependency is OK (orchestration → domain service)
2. **modelops-bundle** should work standalone for CLI users
3. **Auth injection** pattern: caller provides auth, never created internally
4. **Minimal code duplication**: share protocols via modelops-contracts

### Layer Responsibilities

#### modelops-contracts (Protocols Only)
- Defines `AuthProvider` protocol
- Defines `Credential` dataclass
- No implementation code

#### modelops (Orchestration Layer)
- Keeps auth providers for when it needs to interact with registries
- Provides auth to modelops-bundle when using it
- Handles job submission with integrated bundle operations

#### modelops-bundle (Domain Service)
- Has minimal auth implementation for standalone CLI use
- Accepts auth injection when called from modelops
- Pure bundle operations, no infrastructure concerns

## Implementation Plan

### Phase 1: Fix modelops-bundle Auth

#### 1.1 Create Minimal Auth Module
**File: `modelops-bundle/src/modelops_bundle/auth/__init__.py`**
```python
"""Minimal authentication for standalone CLI usage."""

import os
import json
import subprocess
from typing import Optional
from modelops_contracts import AuthProvider, Credential


class AzureCliAuth(AuthProvider):
    """Minimal Azure CLI auth for ACR operations."""

    def get_registry_credential(self, registry: str) -> Credential:
        """Get ACR token using Azure CLI."""
        registry_name = registry.split('.')[0]

        try:
            result = subprocess.run(
                ["az", "acr", "login", "--name", registry_name, "--expose-token"],
                capture_output=True,
                text=True,
                check=True
            )
            token_info = json.loads(result.stdout)

            return Credential(
                username="00000000-0000-0000-0000-000000000000",
                secret=token_info["accessToken"],
                expires_at=None  # Could parse expiry if needed
            )
        except subprocess.CalledProcessError as e:
            from ..errors import AuthError
            raise AuthError(f"Azure CLI auth failed: {e.stderr}")

    def get_storage_credential(self, account: str, container: str) -> Credential:
        """Storage auth not needed for bundle CLI operations."""
        raise NotImplementedError("Storage auth handled via environment")


class StaticAuth(AuthProvider):
    """Static auth from environment variables."""

    def get_registry_credential(self, registry: str) -> Credential:
        """Get credentials from environment."""
        username = os.environ.get("REGISTRY_USERNAME", "")
        password = os.environ.get("REGISTRY_PASSWORD", "")

        if not password:
            return Credential(username="", secret="")  # Anonymous

        return Credential(username=username, secret=password)

    def get_storage_credential(self, account: str, container: str) -> Credential:
        """Not implemented for static auth."""
        raise NotImplementedError()


def get_auth_provider(registry_ref: str) -> AuthProvider:
    """Get auth provider for standalone CLI use."""
    # In K8s/CI, use env vars
    if os.environ.get("REGISTRY_USERNAME") and os.environ.get("REGISTRY_PASSWORD"):
        return StaticAuth()

    # On workstation with Azure CLI
    if ".azurecr.io" in registry_ref.lower():
        if subprocess.run(["which", "az"], capture_output=True).returncode == 0:
            return AzureCliAuth()

    # Default to static/anonymous
    return StaticAuth()
```

#### 1.2 Fix BundleService Constructor
**File: `modelops-bundle/src/modelops_bundle/bundle_service.py`**
```python
# REMOVE broken import line 35:
# from .auth_providers import get_auth_provider

from typing import Optional
from modelops_contracts import AuthProvider

class BundleService:
    def __init__(
        self,
        auth_provider: Optional[AuthProvider] = None,
        deps: Optional[BundleDeps] = None
    ):
        """Initialize with auth provider OR deps.

        Args:
            auth_provider: Auth provider for registry/storage operations
            deps: Full dependency injection (for testing)
        """
        if deps is None:
            ctx = ProjectContext()

            # Use provided auth or get from local module
            if auth_provider is None:
                from .auth import get_auth_provider
                # Get auth based on environment config
                config = load_config(ctx)
                auth_provider = get_auth_provider(config.registry_ref)

            deps = BundleDeps(
                ctx=ctx,
                adapter=OrasAdapter(auth_provider=auth_provider)
            )
        self.deps = deps

    # REMOVE _get_auth_provider method (lines 81-99)
```

#### 1.3 Fix ops.py
**File: `modelops-bundle/src/modelops_bundle/ops.py`**
```python
# CHANGE line 25-26 from:
# from .auth_providers import get_auth_provider
# TO:
from .auth import get_auth_provider

# Update _get_auth_provider function (line 35):
def _get_auth_provider(config: BundleConfig, ctx: ProjectContext):
    """Get auth provider for standalone operations."""
    # Use local auth module
    from .auth import get_auth_provider
    return get_auth_provider(config.registry_ref)
```

#### 1.4 Fix cli.py
**File: `modelops-bundle/src/modelops_bundle/cli.py`**
```python
# REMOVE line 26:
# from .auth_providers import get_auth_provider
# (Not needed if ops.py handles auth)
```

### Phase 2: Fix modelops Integration

#### 2.1 Fix JobSubmissionClient
**File: `modelops/src/modelops/client/job_submission.py`**
```python
from modelops_bundle.bundle_service import BundleService

class JobSubmissionClient:
    def __init__(self, env: str = "dev", namespace: str = "modelops-dask-dev"):
        self.env = env
        self.namespace = namespace
        self._bundle_service = None

        # Keep auth provider for our use
        from ..auth_providers import get_auth_provider
        self.auth_provider = get_auth_provider("azure")

        # Storage setup remains the same
        connection_string = self._get_storage_connection()
        self.storage = AzureBlobBackend(
            container="tasks",
            connection_string=connection_string
        )

    @property
    def bundle_service(self) -> BundleService:
        """Lazy initialization of BundleService with injected auth."""
        if self._bundle_service is None:
            # Pass auth_provider to the new constructor signature
            self._bundle_service = BundleService(auth_provider=self.auth_provider)
        return self._bundle_service
```

### Phase 3: Clean Up Duplicates

#### 3.1 Keep Both AuthError Classes (Different Hierarchies)
- `modelops.errors.AuthError` → ModelOpsError
- `modelops_bundle.errors.AuthError` → BundleError → RegistryError

These serve different purposes and have different inheritance chains. Keep both.

### Phase 4: Optional Enhancements

#### 4.1 Add Scheme to Credential (modelops-contracts)
```python
# modelops-contracts/src/modelops_contracts/auth.py
from typing import Literal, Optional
from dataclasses import dataclass

Scheme = Literal["basic", "bearer", "sas", "connection_string"]

@dataclass(frozen=True)
class Credential:
    username: str
    secret: str
    expires_at: Optional[float] = None
    scheme: Scheme = "basic"  # New field with default
```

## Files to Change

### modelops-bundle
1. ✅ Create: `src/modelops_bundle/auth/__init__.py` (new minimal auth module)
2. ✅ Modify: `src/modelops_bundle/bundle_service.py` (fix constructor, remove broken import)
3. ✅ Modify: `src/modelops_bundle/ops.py` (fix import)
4. ✅ Modify: `src/modelops_bundle/cli.py` (remove broken import)

### modelops
1. ✅ Modify: `src/modelops/client/job_submission.py` (fix BundleService usage)
2. ✅ Keep: `src/modelops/auth_providers/*` (still needed for modelops operations)

### modelops-contracts
1. ⚠️ Optional: `src/modelops_contracts/auth.py` (add scheme field to Credential)

## Testing Plan

### Test 1: Standalone Bundle CLI
```bash
cd modelops-bundle/dev/sample_projects/epi_model
uv run mops-bundle push
# Should authenticate using Azure CLI
```

### Test 2: Job Submission with Bundle
```python
from modelops.client import JobSubmissionClient

client = JobSubmissionClient()
client.submit_sim_job(study, bundle_strategy="build", build_path="./model")
# Should build, push bundle, then submit job
```

### Test 3: K8s Pod Authentication
```bash
# Set env vars to simulate K8s
export REGISTRY_USERNAME=user
export REGISTRY_PASSWORD=token
uv run mops-bundle push
# Should use static auth from env vars
```

## Benefits

1. **Clean Separation**: Each package has clear responsibilities
2. **No Circular Dependencies**: modelops → modelops-bundle is one-way
3. **Works Standalone**: modelops-bundle CLI works without modelops
4. **Minimal Code**: ~50 lines for auth in modelops-bundle
5. **Azure MVP**: Focused on ACR/Azure, easy to extend later
6. **Better UX**: Single-step job submission with automatic bundle handling

## Migration Notes

- No database migrations needed
- No Pulumi state changes
- Backward compatible with existing environment files
- Auth providers in modelops are kept (not removed)

## Future Extensions

Easy to add later:
- AWS ECR auth (~20 lines)
- GCP GCR auth (~20 lines)
- Token caching and refresh
- Workload identity support