"""Analyze kernel performance regressions by comparing metrics between versions."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from matplotlib.patches import Patch


def calculate_regression_simple(df, kernel_a, kernel_b):
    """Calculate regression without architecture breakdown."""
    results = []

    for metric in df["metric"].unique():
        metric_data = df[df["metric"] == metric]
        unit = metric_data["unit"].iloc[0] if "unit" in metric_data.columns else ""
        more_is_better = metric_data["more_is_better"].iloc[0] if "more_is_better" in metric_data.columns else True

        mean_a = metric_data[metric_data["kernel_base"] == kernel_a]["value"].mean()
        mean_b = metric_data[metric_data["kernel_base"] == kernel_b]["value"].mean()
        pct_change = ((mean_b - mean_a) / mean_a) * 100 if mean_a > 0 else 0
        is_regression = pct_change < 0 if more_is_better else pct_change > 0

        results.append(
            {
                "metric": metric,
                "arch": "all",
                "unit": unit,
                "more_is_better": more_is_better,
                f"{kernel_a}_mean": mean_a,
                f"{kernel_b}_mean": mean_b,
                "absolute_change": mean_b - mean_a,
                "percent_change": pct_change,
                "is_regression": is_regression,
            }
        )

    results_df = pd.DataFrame(results).sort_values("percent_change")
    # Drop metrics where either kernel has no data (nan means metric only exists for one kernel)
    results_df = results_df.dropna(subset=[f"{kernel_a}_mean", f"{kernel_b}_mean"])
    results_for_plot = results_df[abs(results_df["percent_change"]) > 1.0].copy()
    return results_df, results_for_plot, kernel_a, kernel_b


def plot_regression_comparison(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    results_df, kernel_a, kernel_b, output_path, title_suffix="", unixbench_version=""
):
    """Create a comparison plot showing regressions."""
    if results_df.empty:
        print(f"  No significant changes (>1%) found for {title_suffix}")
        return

    sns.set_style("whitegrid")
    _, ax = plt.subplots(figsize=(12, max(5, len(results_df) * 0.35)))
    palette = sns.color_palette()
    colors = [palette[3] if reg else palette[2] for reg in results_df["is_regression"]]

    sns.barplot(data=results_df, y="metric", x="percent_change", palette=colors, ax=ax, alpha=0.8)

    ax.set_xlabel("Performance Change (%)", fontsize=11, fontweight="bold")
    ax.set_ylabel("")
    ax.tick_params(axis="y", labelsize=11)
    ax.tick_params(axis="x", labelsize=11)

    title = f"Kernel Performance Comparison {title_suffix}\n{kernel_a} → {kernel_b}"
    if unixbench_version:
        title += f"\n{unixbench_version}"
    ax.set_title(title, fontsize=14, fontweight="bold", pad=20)
    ax.axvline(x=0, color="black", linestyle="-", linewidth=1.5, zorder=0)

    for i, pct in enumerate(results_df["percent_change"]):
        ax.text(pct / 2, i, f"{pct:+.1f}%", va="center", ha="center", fontsize=11, fontweight="bold")

    legend_elements = [
        Patch(facecolor=palette[3], alpha=0.8, label="Regression (>1%)"),
        Patch(facecolor=palette[2], alpha=0.8, label="Improvement (>1%)"),
    ]
    ax.legend(handles=legend_elements, loc="best", fontsize=11, frameon=True)

    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches="tight")
    print(f"  ✓ Saved: {output_path}")
    plt.close()


def print_regression_summary(results_df, kernel_a, kernel_b):
    """Print summary of regressions."""
    regressions = results_df[results_df["is_regression"]]
    improvements = results_df[~results_df["is_regression"]]

    print(
        f"\n  Total Metrics: {len(results_df)}, Regressions: {len(regressions)}, " f"Improvements: {len(improvements)}"
    )

    for label, subset in [("REGRESSIONS", regressions), ("IMPROVEMENTS", improvements)]:
        if subset.empty:
            continue
        print(f"\n  {label}:")
        for _, row in subset.iterrows():
            print(
                f"    {row['metric']}: {row[f'{kernel_a}_mean']:.2f} → "
                f"{row[f'{kernel_b}_mean']:.2f} {row['unit']} ({row['percent_change']:+.2f}%)"
            )


def _analyze_arch(df, kernel_a, kernel_b, plots_dir, label, unixbench_version):
    """Analyze and plot for a single architecture slice."""
    results_full, results_plot, _, _ = calculate_regression_simple(df, kernel_a, kernel_b)
    plot_regression_comparison(
        results_plot,
        kernel_a,
        kernel_b,
        plots_dir / f"regression_{label.lower().replace(' ', '_')}.png",
        title_suffix=f"({label})",
        unixbench_version=unixbench_version,
    )
    print_regression_summary(results_full, kernel_a, kernel_b)
    return results_full


def main(args):
    """Analyze kernel performance regressions from a combined CSV.

    Args:
        args: Namespace with input_csv, output_dir, output_csv.
    """
    if not Path(args.input_csv).exists():
        print(f"Error: Input CSV not found: {args.input_csv}")
        return 1

    input_dir = Path(args.input_csv).parent
    output_dir = getattr(args, "output_dir", None) or str(input_dir)
    output_csv = getattr(args, "output_csv", None) or f"{output_dir}/regression_results.csv"

    print(f"Loading data from {args.input_csv}...")
    df = pd.read_csv(args.input_csv)
    print(f"✓ Loaded {len(df)} rows")

    df["kernel_base"] = df["kernel_version"].str.replace(r"\.(x86_64|aarch64|arm64)$", "", regex=True)
    kernels = sorted(df["kernel_base"].unique())
    if len(kernels) < 2:
        print("Error: Need at least 2 kernel versions to compare")
        return 1

    kernel_a, kernel_b = kernels[0], kernels[1]
    print(f"Comparing: {kernel_a} vs {kernel_b}")

    plots_dir = Path(output_dir) / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    unixbench_version = ""
    if "unixbench_version" in df.columns:
        versions = df["unixbench_version"].dropna().unique()
        if len(versions) > 0:
            unixbench_version = versions[0]

    # Overall
    print("\nOverall (All Architectures):")
    all_results = [_analyze_arch(df, kernel_a, kernel_b, plots_dir, "overall", unixbench_version)]

    # Per-architecture
    if "arch" in df.columns:
        for arch_pattern, label in [("x86_64", "x86_64"), ("aarch64|arm64", "aarch64")]:
            arch_df = df[df["arch"].str.contains(arch_pattern, case=False, na=False)]
            if not arch_df.empty:
                print(f"\n{label}:")
                result = _analyze_arch(arch_df, kernel_a, kernel_b, plots_dir, label, unixbench_version)
                result["arch"] = label
                all_results.append(result)

    combined_results = pd.concat(all_results, ignore_index=True)
    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    combined_results.to_csv(output_csv, index=False)
    print(f"\n✓ Saved regression analysis to {output_csv}")

    return 0
