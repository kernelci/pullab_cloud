"""Download and analyze kernel benchmark results from S3."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

import types
from pathlib import Path

import boto3


def upload_analysis_to_s3(bucket, run_prefix, output_dir, combined_csv, regression_csv, region):
    """Upload analysis results to S3."""
    s3 = boto3.client("s3", region_name=region)
    output_path = Path(output_dir)

    files_to_upload = [
        (output_path / combined_csv, f"{run_prefix}/analysis/{combined_csv}"),
        (output_path / regression_csv, f"{run_prefix}/analysis/{regression_csv}"),
    ]

    plots_dir = output_path / "plots"
    if plots_dir.exists():
        for plot_file in plots_dir.glob("*.png"):
            files_to_upload.append((plot_file, f"{run_prefix}/analysis/plots/{plot_file.name}"))

    uploaded = 0
    for local_file, s3_key in files_to_upload:
        if not local_file.exists():
            continue
        try:
            s3.upload_file(str(local_file), bucket, s3_key)
            print(f"  ✓ Uploaded: {s3_key}")
            uploaded += 1
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"  ✗ Failed to upload {local_file.name}: {e}")

    print(f"\nUploaded {uploaded} files to s3://{bucket}/{run_prefix}/analysis/")


def main(args):
    """Orchestrate download and analysis of kernel benchmark results.

    Args:
        args: Namespace with bucket, run_prefix, output_dir, region,
              file_pattern, combined_csv, regression_csv, upload_analysis.
    """
    from kernel_ci_cloud_labs.analysis.analyze_regressions import main as analyze_main
    from kernel_ci_cloud_labs.analysis.download_results import main as download_main

    output_dir = args.output_dir or f"analysis/data/{args.run_prefix}"
    combined_csv = getattr(args, "combined_csv", "combined_results.csv")
    regression_csv = getattr(args, "regression_csv", "regression_results.csv")
    file_pattern = getattr(args, "file_pattern", "benchmark-*.csv")

    combined_csv_path = f"{output_dir}/{combined_csv}"
    regression_csv_path = f"{output_dir}/{regression_csv}"

    # Step 1: Download
    print("=" * 80)
    print("STEP 1: Downloading results from S3")
    print("=" * 80)

    download_args = types.SimpleNamespace(
        bucket=args.bucket,
        run_prefix=args.run_prefix,
        output_dir=output_dir,
        output_csv=combined_csv_path,
        region=args.region,
        file_pattern=file_pattern,
    )

    if download_main(download_args) != 0:
        print("Error: Download failed")
        return 1

    # Step 2: Analyze
    print("\n" + "=" * 80)
    print("STEP 2: Analyzing regressions")
    print("=" * 80)

    analyze_args = types.SimpleNamespace(
        input_csv=combined_csv_path,
        output_dir=output_dir,
        output_csv=regression_csv_path,
    )

    if analyze_main(analyze_args) != 0:
        print("Error: Analysis failed")
        return 1

    print(f"\n✓ Complete! All results saved to {output_dir}/")

    # Step 3: Upload if requested
    if getattr(args, "upload_analysis", False):
        upload_analysis_to_s3(args.bucket, args.run_prefix, output_dir, combined_csv, regression_csv, args.region)

    return 0
