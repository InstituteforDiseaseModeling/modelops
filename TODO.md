# TODO.md

- [x] See tracking of how many process can run in a dask worker.
  - **Finding**: Currently using 1 process with 2 threads per pod (suboptimal for GIL-bound simulations)
  - **Recommendation**: Use `--nprocs 2 --nthreads 1` for better parallelism with pure Python code
  - **Documentation**: See `docs/dask-configuration.md` for detailed analysis


- [ ] Add the following in configurable section:

```
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
MKL_NUM_THREADS=1
NUMEXPR_NUM_THREADS=1
```


- [ ] Can we test the simulation service on local Dask easily? Especially
  aggregations to prevent regressions.

- [ ] Infra dependency graph?

- [ ] Circuit breaker?
