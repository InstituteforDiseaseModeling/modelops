#!/usr/bin/env python
"""Test script for loss pairplot visualization."""

import sys
import polars as pl
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter
import warnings
warnings.filterwarnings('ignore')


def create_loss_pairplot(df, param_cols=None, figsize=(15, 15), alpha=1, cmap='viridis'):
    """
    Create a pairplot showing loss relationships with parameters.

    Upper triangle: 2D loss surface (contour plots)
    Diagonal: 1D slices through MLE point (showing loss vs each parameter with others fixed at MLE)
    Lower triangle: Scatterplots with transparency
    """

    # Filter to only parameter columns, excluding param_id
    if param_cols is None:
        param_cols = [col for col in df.columns
                      if col.startswith('param_') and col != 'param_id']

    n_params = len(param_cols)

    # Create figure with subplots
    fig = plt.figure(figsize=figsize)
    gs = GridSpec(n_params, n_params, figure=fig, hspace=0.05, wspace=0.05)
    axes = {}

    # Get loss values for coloring
    losses = df['loss'].to_numpy()
    loss_norm = (losses - losses.min()) / (losses.max() - losses.min())

    # Compute consistent axis limits for each parameter
    param_limits = {}
    for col in param_cols:
        data = df[col].to_numpy()
        margin = (data.max() - data.min()) * 0.02  # 2% margin
        param_limits[col] = (data.min() - margin, data.max() + margin)

    # Find the MLE (minimum loss) point
    mle_idx = np.argmin(losses)
    mle_params = {col: df[col].iloc[mle_idx] for col in param_cols}
    mle_loss = losses[mle_idx]

    print(f"MLE found at index {mle_idx}:")
    print(f"  Loss: {mle_loss:,.0f}")
    for col in param_cols:
        print(f"  {col}: {mle_params[col]:.3f}")

    for i in range(n_params):
        for j in range(n_params):
            ax = fig.add_subplot(gs[i, j])
            axes[(i, j)] = ax

            param_i = param_cols[i]
            param_j = param_cols[j]

            x_data = df[param_j].to_numpy()
            y_data = df[param_i].to_numpy()

            if i == j:
                # Diagonal: 1D slice through MLE point
                # Find points that are close to MLE in all other dimensions
                tolerance_factor = 0.15  # Look for points within 15% of MLE values in other dims

                # Create mask for points near MLE in other parameters
                mask = np.ones(len(df), dtype=bool)
                for other_col in param_cols:
                    if other_col != param_j:
                        param_range = df[other_col].max() - df[other_col].min()
                        tolerance = param_range * tolerance_factor
                        other_data = df[other_col].to_numpy()
                        mask &= np.abs(other_data - mle_params[other_col]) < tolerance

                # If we have enough points near MLE, show them
                if np.sum(mask) > 5:
                    x_slice = x_data[mask]
                    loss_slice = losses[mask]

                    # Plot the slice points
                    ax.scatter(x_slice, loss_slice, alpha=0.6, s=20, c='blue', label='Near MLE')

                    # Add smoothed line through slice
                    if len(x_slice) > 3:
                        sorted_indices = np.argsort(x_slice)
                        x_sorted = x_slice[sorted_indices]
                        loss_sorted = loss_slice[sorted_indices]

                        from scipy.ndimage import uniform_filter1d
                        window = min(len(x_sorted) // 3, 5)
                        if window > 1:
                            loss_smooth = uniform_filter1d(loss_sorted, size=window, mode='nearest')
                            ax.plot(x_sorted, loss_smooth, 'b-', linewidth=2, alpha=0.8)

                # Show all points in background for context
                ax.scatter(x_data, losses, alpha=0.1, s=5, c='gray')

                # Mark the MLE point
                ax.scatter([mle_params[param_j]], [mle_loss],
                          color='red', s=100, marker='*', zorder=5, label='MLE')

                # Add vertical line at MLE parameter value
                ax.axvline(mle_params[param_j], color='red', linestyle='--', alpha=0.3)

                # Clean parameter name for label
                clean_name = param_j.replace('param_', '')
                ax.set_xlabel(clean_name, fontsize=10)
                ax.set_ylabel('Loss', fontsize=10)

                # Add legend to first diagonal
                if i == 0:
                    ax.legend(loc='upper right', fontsize=8)

            elif i < j:
                # Upper triangle: 2D loss surface (contour plot)
                try:
                    # Create grid for interpolation
                    xi = np.linspace(x_data.min(), x_data.max(), 30)
                    yi = np.linspace(y_data.min(), y_data.max(), 30)
                    xi_grid, yi_grid = np.meshgrid(xi, yi)

                    # Interpolate loss values onto grid
                    zi = griddata((x_data, y_data), losses,
                                 (xi_grid, yi_grid), method='linear', fill_value=np.mean(losses))

                    # Smooth the surface
                    zi_smooth = gaussian_filter(zi, sigma=1.0)

                    # Create contour plot
                    levels = np.percentile(losses, np.linspace(0, 100, 10))
                    contour = ax.contourf(xi_grid, yi_grid, zi_smooth, levels=levels,
                                         cmap=cmap, alpha=0.6)
                    ax.contour(xi_grid, yi_grid, zi_smooth, levels=levels[::2],
                              colors='black', alpha=0.2, linewidths=0.5)

                    # Add scatter points on top
                    ax.scatter(x_data, y_data, c=losses, s=3, alpha=0.2,
                             cmap=cmap, edgecolors='none')

                    # Mark MLE point with star
                    ax.scatter([mle_params[param_j]], [mle_params[param_i]],
                              color='red', s=200, marker='*', zorder=10,
                              edgecolors='white', linewidth=1)

                    # Add crosshairs at MLE
                    ax.axvline(mle_params[param_j], color='red', linestyle='--', alpha=0.3, linewidth=1)
                    ax.axhline(mle_params[param_i], color='red', linestyle='--', alpha=0.3, linewidth=1)

                except Exception as e:
                    # Fallback to scatter if interpolation fails
                    print(f"Contour failed for {param_i} vs {param_j}: {e}")
                    ax.scatter(x_data, y_data, c=losses, s=10, alpha=alpha, cmap=cmap)
                    # Still mark MLE in fallback
                    ax.scatter([mle_params[param_j]], [mle_params[param_i]],
                              color='red', s=200, marker='*', zorder=10)

            else:
                # Lower triangle: scatter plots
                ax.scatter(x_data, y_data, c=losses, s=10, alpha=alpha,
                         cmap=cmap, edgecolors='none')

                # Mark MLE point with star
                ax.scatter([mle_params[param_j]], [mle_params[param_i]],
                          color='red', s=200, marker='*', zorder=10,
                          edgecolors='white', linewidth=1)

                # Add crosshairs at MLE
                ax.axvline(mle_params[param_j], color='red', linestyle='--', alpha=0.3, linewidth=1)
                ax.axhline(mle_params[param_i], color='red', linestyle='--', alpha=0.3, linewidth=1)

            # Handle tick labels and axis labels more carefully
            if i < n_params - 1:
                # Not bottom row - remove x tick labels
                ax.set_xticklabels([])
            else:
                # Bottom row - add x axis label
                clean_name = param_j.replace('param_', '')
                ax.set_xlabel(clean_name, fontsize=10)
                # Rotate and format tick labels
                labels = ax.get_xticklabels()
                for label in labels:
                    label.set_rotation(45)
                    label.set_fontsize(8)
                    label.set_ha('right')

            if j > 0:
                # Not first column - remove y tick labels
                ax.set_yticklabels([])
            else:
                # First column - add y axis label (except diagonal)
                if i != j:
                    clean_name = param_i.replace('param_', '')
                    ax.set_ylabel(clean_name, fontsize=10)
                # Format tick labels
                labels = ax.get_yticklabels()
                for label in labels:
                    label.set_fontsize(8)

            # Set consistent axis limits for each parameter
            if i != j:
                ax.set_xlim(param_limits[param_j])
                ax.set_ylim(param_limits[param_i])
            else:
                # For diagonal plots, set x limits
                ax.set_xlim(param_limits[param_j])

            # Format tick values to avoid scientific notation for small ranges
            # Only apply to numeric axes (not string types)
            try:
                if i == n_params - 1:  # Bottom row
                    ax.ticklabel_format(axis='x', style='plain', useOffset=False)
                if j == 0 and i != j:  # First column
                    ax.ticklabel_format(axis='y', style='plain', useOffset=False)
            except (AttributeError, TypeError):
                # Skip if not using ScalarFormatter (e.g., for categorical data)
                pass

    # Add colorbar for loss
    cbar_ax = fig.add_axes([0.93, 0.15, 0.02, 0.7])
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(vmin=losses.min(), vmax=losses.max()))
    sm.set_array([])
    cbar = plt.colorbar(sm, cax=cbar_ax)
    cbar.set_label('Loss', rotation=270, labelpad=20, fontsize=12)

    # Add title
    fig.suptitle('Parameter-Loss Pairplot Analysis', fontsize=16, y=0.995)

    # Adjust layout to prevent overlap
    plt.subplots_adjust(left=0.08, right=0.92, top=0.97, bottom=0.08)

    return fig


def main():
    """Load data and create pairplot."""

    # File path
    FILE = sys.argv[1] if len(sys.argv) > 1 else "starsim_sir_results.parquet"

    print(f"Loading data from {FILE}...")

    try:
        # Load the parquet file
        df = pl.read_parquet(FILE)
        print(f"Loaded {len(df)} rows with columns: {df.columns}")

        # Convert to pandas for easier plotting (matplotlib works better with pandas)
        df_pandas = df.to_pandas()

        # Create the pairplot
        print("Creating pairplot...")
        fig = create_loss_pairplot(df_pandas, figsize=(12, 12))

        # Save to PDF
        output_file = "loss_pairplot.pdf"
        print(f"Saving to {output_file}...")
        fig.savefig(output_file, dpi=150, bbox_inches='tight')
        print(f"Saved to {output_file}")

        # Also save as PNG for quick viewing
        output_png = "loss_pairplot.png"
        fig.savefig(output_png, dpi=100, bbox_inches='tight')
        print(f"Also saved as {output_png}")

        # Display if in interactive mode
        # plt.show()

    except FileNotFoundError:
        print(f"Error: File {FILE} not found!")
        print("Make sure you've downloaded the job results first.")
        print("Run: mops results download job-e4142f9f")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
