"""Download and combine kernel benchmark results from S3."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import boto3
import pandas as pd


def download_csvs_from_s3(bucket, run_prefix, output_dir, file_pattern, region="us-west-2"):
    """Download CSV files from S3 matching the specified pattern."""
    from fnmatch import fnmatch

    s3 = boto3.client("s3", region_name=region)
    output_path = Path(output_dir) / "downloads"
    output_path.mkdir(parents=True, exist_ok=True)

    print(f"Downloading CSVs matching '{file_pattern}' from s3://{bucket}/{run_prefix}/...")

    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=run_prefix)

    csv_files = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if "/output/" in key and fnmatch(Path(key).name, file_pattern):
                parts = key.split("/")
                test_name = parts[1].replace("test_", "") if len(parts) > 1 else "unknown"
                instance_id = parts[3] if len(parts) > 3 else "unknown"
                filename = f"{test_name}_{instance_id}_{Path(key).name}"
                local_path = output_path / filename

                print(f"  Downloading: {key}")
                try:
                    s3.download_file(bucket, key, str(local_path))
                    csv_files.append(local_path)
                except Exception as e:  # pylint: disable=broad-exception-caught
                    print(f"  Error downloading {key}: {e}")

    print(f"\n✓ Downloaded {len(csv_files)} CSV files to {output_dir}")
    return csv_files


def load_and_combine_csvs(csv_files):
    """Load all CSV files and combine into a single DataFrame."""
    import re

    dfs = []
    for csv_file in csv_files:
        try:
            df = pd.read_csv(csv_file)
            df["source_file"] = csv_file.name

            if "metric" in df.columns and len(df) > 0:
                sample_metric = df["metric"].iloc[0]
                if "byte-unixbench" in sample_metric.lower():
                    match = re.search(r"byte-unixbench-[\d.]+", sample_metric, re.IGNORECASE)
                    if match:
                        df["unixbench_version"] = match.group(0)
                        df["metric"] = (
                            df["metric"]
                            .str.replace(r"byte-unixbench-[\d.]+\s*", "", regex=True, case=False)
                            .str.strip()
                        )
            dfs.append(df)
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"Warning: Failed to load {csv_file}: {e}")

    if not dfs:
        print("Error: No valid CSV files found")
        return None

    combined = pd.concat(dfs, ignore_index=True)
    print(f"✓ Combined {len(dfs)} CSV files into {len(combined)} rows")
    return combined


def main(args):
    """Download benchmark CSVs from S3 and combine into a single file.

    Args:
        args: Namespace with bucket, run_prefix, output_dir, output_csv, region, file_pattern.
    """
    if not args.file_pattern.endswith(".csv"):
        print(f"Error: File pattern '{args.file_pattern}' not supported (only *.csv)")
        return 1

    output_dir = args.output_dir or f"analysis/data/{args.run_prefix}"
    output_csv = args.output_csv or f"{output_dir}/combined_results.csv"

    csv_files = download_csvs_from_s3(args.bucket, args.run_prefix, output_dir, args.file_pattern, args.region)
    if not csv_files:
        print("No CSV files found")
        return 1

    df = load_and_combine_csvs(csv_files)
    if df is not None:
        output_path = Path(output_csv)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
        print(f"✓ Saved combined results to {output_csv}")

    return 0
