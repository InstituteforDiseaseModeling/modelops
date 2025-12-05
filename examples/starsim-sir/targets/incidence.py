"""
Incidence targets for SIR model calibration.

This module provides two targets with different evaluation strategies:
1. mean_signal_mse: Averages replicates first, then computes MSE (no variance penalty)
2. annealed_mse: Computes MSE per replicate, then averages (includes variance penalty)
"""

import polars as pl
import modelops_calabaria as cb
from modelops_calabaria.core.target import LossTarget
from modelops_calabaria.core.alignment import JoinAlignment
from modelops_calabaria.core.evaluation import mean_signal_mse, annealed_mse


@cb.calibration_target(
    model_output="incidence",
    data={
        'observed': "data/observed_incidence.csv"  # Use clean or noisy version
    }
)
def incidence_mean_signal_target(data_paths):
    """
    Target comparing simulated vs observed daily incidence using mean-signal MSE.

    This evaluation strategy first averages the simulation results across all
    replicates, then computes MSE against the observed data.

    Formula: (y - E_ε[sim])²

    This approach reduces noise from individual replicates and targets the
    observation-level mean WITHOUT penalizing simulator variance.

    Args:
        data_paths: Dict with paths to data files from decorator

    Returns:
        LossTarget: Configured target for incidence evaluation with mean-signal MSE
    """
    # Load the observation data
    observed_data = pl.read_csv(data_paths['observed'])

    # Create and return the target
    return LossTarget(
        name="incidence_mean_signal",
        model_output="incidence",
        data=observed_data,
        alignment=JoinAlignment(
            on_cols="day",
            mode="exact"
        ),
        evaluator=mean_signal_mse(col="infected", weight=1.0),
    )


@cb.calibration_target(
    model_output="incidence",
    data={
        'observed': "data/observed_incidence.csv"  # Use clean or noisy version
    }
)
def incidence_annealed_target(data_paths):
    """
    Target comparing simulated vs observed daily incidence using annealed MSE.

    This evaluation strategy computes MSE for each simulation replicate separately,
    then takes the mean of these MSE values.

    Formula: E_ε[(y - sim)²]

    This approach gives equal weight to each replicate and INCLUDES a variance
    penalty, meaning it penalizes simulator stochasticity.

    Args:
        data_paths: Dict with paths to data files from decorator

    Returns:
        LossTarget: Configured target for incidence evaluation with annealed MSE
    """
    # Load the observation data
    observed_data = pl.read_csv(data_paths['observed'])

    # Create and return the target
    return LossTarget(
        name="incidence_annealed",
        model_output="incidence",
        data=observed_data,
        alignment=JoinAlignment(
            on_cols="day",
            mode="exact"
        ),
        evaluator=annealed_mse(col="infected", weight=1.0),
    )
