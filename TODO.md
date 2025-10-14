# TODO

## CLI Improvements
- [ ] Consolidate `mops infra status` and `mops status`
- [ ] Standardize makefile: `make build` → `make deploy` pattern (e.g., `make deploy-worker`)
- [ ] Either deprecate component-specific CLI (`registry up`, `storage up`) or update to use wiring info like `infra` CLI

## Observability
- [ ] Consider [structlog](https://www.structlog.org/en/stable/why.html)
- [ ] Add modern cloud-friendly observability library (replace `logging`)

## Performance
- [ ] Add configurable thread limits for numerical libraries:
  ```bash
  OMP_NUM_THREADS=1
  OPENBLAS_NUM_THREADS=1
  MKL_NUM_THREADS=1
  NUMEXPR_NUM_THREADS=1
  ```

## Architecture
- [ ] **Remove Calabaria from worker images** (dependency management required)
  - **Issue**: Bundles need `modelops-calabaria` for wire function but can't install from private GitHub
  - **Current workaround**: Include Calabaria in worker Docker images (redundant)
  - **Solution paths**:
    1. Publish modelops-calabaria to PyPI or private registry (Azure Artifacts)
    2. Bundle as OCI artifact in ACR alongside bundles
    3. Vendor wire function directly in bundles (self-contained)
  - **Goal**: Clean separation - workers only have infra (ModelOps), bundles bring science (Calabaria)
