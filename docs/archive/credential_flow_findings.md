# Credential Flow and Bundle Fetch Investigation Results

## Summary
✅ **Both ACR and blob storage credentials are properly supported and flowing to worker pods**
✅ **Bundle fetching works when using correct repository name**
❌ **ProvenanceStore only implements local filesystem storage, not blob**

## Key Findings

### 1. Credential Flow Verified
Credentials flow from Pulumi stacks → K8s secrets → Worker pods:
- **ACR Credentials**: Created in `bundle-credentials` secret with REGISTRY_USERNAME/PASSWORD
- **Blob Storage**: Created in `modelops-storage` secret with AZURE_STORAGE_CONNECTION_STRING
- Both secrets mounted via `envFrom` in worker deployments (workspace.py:267-293)

### 2. Bundle Repository Issue Resolved
- **Problem**: Bundles were being fetched from wrong repository name
- **Error**: "Expecting value: line 1 column 1 (char 0)" - HTML login page instead of JSON
- **Solution**: Use `simulation-workflow@sha256:digest` instead of `modelops-bundles@sha256:digest`
- **Verified**: Successfully fetched bundle `simulation-workflow@sha256:a7671b13481871066dde8a541dcbca5781fd5eb3234d43df7cae96ffe8147965`

### 3. ProvenanceStore Architecture Gap
- **Current**: Only uses local filesystem (`/tmp/modelops/provenance/`)
- **Missing**: No Azure blob backend implementation
- **Impact**: Results never persisted to blob storage, only local disk
- **Required**: Implement blob backend with toggle between local/blob modes

## New CLI Commands Added

### `mops dev check-credentials`
Checks if worker pods have all required credentials:
```bash
uv run mops dev check-credentials
```
Shows:
- ✓/✗ for each required credential
- Values (partially masked for security)

### `mops dev test-bundle-fetch <bundle-ref>`
Tests bundle fetching from worker pods:
```bash
uv run mops dev test-bundle-fetch simulation-workflow@sha256:a7671b13...
```
Verifies:
- Registry authentication
- Bundle download
- Wire function discovery

## Next Steps
1. Update job submission code to use correct repository names
2. Implement blob storage backend for ProvenanceStore
3. Add MODELOPS_STORAGE_BACKEND environment variable for local/blob toggle