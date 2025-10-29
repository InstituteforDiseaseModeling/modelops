"""
Incidence target for SIR model calibration.

This target compares simulated daily new infections against observed data.
"""

import polars as pl
import modelops_calabaria as cb
from modelops_calabaria.core.target import Target
from modelops_calabaria.core.alignment import JoinAlignment
from modelops_calabaria.core.evaluation import replicate_mean_mse


@cb.calibration_target(
    model_output="incidence",
    data={
        'observed': "data/observed_incidence.csv"  # Use clean or noisy version
    }
)
def incidence_target(data_paths):
    """
    Target comparing simulated vs observed daily incidence.

    Args:
        data_paths: Dict with paths to data files from decorator

    Returns:
        Target: Configured target for incidence evaluation
    """
    # Load the observation data
    observed_data = pl.read_csv(data_paths['observed'])

    # Create and return the target
    return Target(
        model_output="incidence",
        data=observed_data,
        alignment=JoinAlignment(
            on_cols="day",
            mode="exact"
        ),
        evaluation=replicate_mean_mse(col="infected"),
        weight=1.0
    )