# Starsim SIR Workflow

This directory shows the full ModelOps workflow (bundle → sampling → calibration)
for the Starsim-based SIR model. All commands are wrapped in the Makefile.

## Prerequisites

- Docker/Kubernetes access to your ModelOps cluster
- `uv` 0.4+ on your PATH

## Quick Start

```bash
# one-time setup
make setup            # create venv + install local repos
make bundle-init      # initialize .modelops-bundle metadata

# register model + targets (also defines the target set used by cb/mops)
make bundle-register

# generate Sobol study + submit it (auto-push bundle)
make study
make submit

# create calibration spec using the registered target set and submit
make calib-gen
make calib-submit
```

The calibration targets are registered once and grouped into a named target set
(`incidence`) via `mops-bundle target-set set …`. Both `cb calibration optuna`
and `mops jobs submit` consume that metadata so you never specify entrypoints
manually.

## Useful Targets

```
make data                # regenerate synthetic observations
make study-tiny          # quick 8-task Sobol study
make submit-grid         # submit grid study
make calib-gen-per-replicate
make calib-submit-per-replicate
make status              # check Kubernetes jobs/pods
make logs                # tail the first job pod
make clean               # remove venv + generated files
```

See the Makefile for more details and additional utilities (result sync, cleanup, etc.).
