# TODO.md

- [ ] Consolidate `mops infra status` and `mops status`.
- [ ] Need better observability, ideally via modern cloud-friendly library
  rather than `logging`.
- [ ] Initially we built infra component-specific provisioning CLI (e.g.
  `registry up`, `storage up`, `workspace up`, etc.). Then we added `infra up`
  with dependency graph-based order and wiring info through each component as
  needed. But now we should either deprecate previous stuff or have it work
  with wiring info like `infra` CLI.
- [ ] Standardize makefile make build --> make deploy, like make deploy-worker 


- [ ] Add the following in configurable section:

```
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
MKL_NUM_THREADS=1
NUMEXPR_NUM_THREADS=1
```

- [ ] **Remove Calabaria from worker images** once dependency management is solved:
  - **Current issue**: Bundles need `modelops-calabaria` for wire function, but can't install from private GitHub repos in isolated venvs without PAT/credentials
  - **Current workaround**: Include Calabaria in worker Docker images (redundant but works)
  - **Solution paths**:
    1. Publish modelops-calabaria to PyPI or private registry (Azure Artifacts)
    2. Bundle as OCI artifact in ACR alongside bundles
    3. Vendor wire function directly in bundles (self-contained)
  - **Goal**: Clean separation - workers only have infra (ModelOps), bundles bring science (Calabaria)

