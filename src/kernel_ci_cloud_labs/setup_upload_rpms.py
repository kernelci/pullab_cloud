#!/usr/bin/env python3
"""Upload local kernel RPMs to S3 bucket for use in kernel testing pipelines."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

import argparse
import sys
import time
from pathlib import Path

import boto3


def bucket_exists(s3_client, bucket_name):
    """Check if S3 bucket exists and is accessible."""
    try:
        s3_client.head_bucket(Bucket=bucket_name)
        return True
    except s3_client.exceptions.NoSuchBucket:
        return False
    except s3_client.exceptions.ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "403":
            print(f"Error: Bucket exists but access denied: {bucket_name}")
        else:
            print(f"Error accessing bucket: {e}")
        return False
    except Exception as e:
        print(f"Error checking bucket: {e}")
        return False


def create_bucket(s3_client, bucket_name, region="us-west-2"):
    """Create S3 bucket in specified region."""
    try:
        if region == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        print("SUCCESS: Bucket created")
        return True
    except Exception as e:
        print(f"Error creating bucket: {e}")
        return False


def file_exists_in_s3(s3_client, bucket, key):
    """Check if file exists in S3."""
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except s3_client.exceptions.ClientError:
        return False


def verify_s3_upload(s3_client, bucket, key, local_file, retries=3):
    """Verify uploaded file size matches local file."""
    for attempt in range(retries):
        try:
            response = s3_client.head_object(Bucket=bucket, Key=key)
            s3_size = response["ContentLength"]
            local_size = local_file.stat().st_size
            return s3_size == local_size
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2**attempt)
                continue
            print(f"    Warning: Could not verify upload after {retries} attempts: {e}")
            return True
    return True


def clean_s3_bucket(bucket_name, exclude_kernels=False, region="us-west-2"):
    """Delete all files from S3 bucket, optionally excluding kernel RPMs."""
    s3 = boto3.client("s3", region_name=region)

    deleted = 0
    try:
        continuation_token = None
        while True:
            list_params = {"Bucket": bucket_name}
            if continuation_token:
                list_params["ContinuationToken"] = continuation_token

            response = s3.list_objects_v2(**list_params)

            if "Contents" in response:
                for obj in response["Contents"]:
                    key = obj["Key"]
                    if exclude_kernels and key.startswith("kernel-rpms/"):
                        print(f"  Skipped: {key} (kernel RPMs excluded)")
                        continue

                    s3.delete_object(Bucket=bucket_name, Key=key)
                    print(f"  Deleted: {key}")
                    deleted += 1

            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")

        if deleted > 0:
            print(f"  SUCCESS: Deleted {deleted} files")
        else:
            print("  No files to delete")
        return True
    except Exception as e:
        print(f"  Error cleaning bucket: {e}")
        return False


def collect_local_rpms(rpm_dirs):
    """Collect and classify RPM files from local directories."""
    files = {
        "source": [],
        "x86_64": [],
        "aarch64": [],
    }

    for rpm_dir in rpm_dirs:
        path = Path(rpm_dir)
        if not path.exists():
            print(f"Warning: Path does not exist: {rpm_dir}")
            continue

        rpm_files = list(path.glob("*.rpm")) if path.is_dir() else [path]

        for rpm_file in rpm_files:
            filename = rpm_file.name
            if filename.endswith(".src.rpm"):
                files["source"].append(rpm_file)
                print(f"  Source: {filename}")
            elif filename.endswith(".x86_64.rpm"):
                files["x86_64"].append(rpm_file)
                print(f"  x86_64: {filename}")
            elif filename.endswith(".aarch64.rpm"):
                files["aarch64"].append(rpm_file)
                print(f"  aarch64: {filename}")
            else:
                print(f"  Skipped (unknown arch): {filename}")

    total = sum(len(v) for v in files.values())
    print(
        f"  Found {total} RPMs: {len(files['source'])} source, "
        f"{len(files['x86_64'])} x86_64, {len(files['aarch64'])} aarch64"
    )
    return files


def upload_to_s3(files, bucket_name, region="us-west-2"):
    """Upload RPMs to S3 bucket with fixed structure."""
    s3 = boto3.client("s3", region_name=region)

    print(f"\nUploading to s3://{bucket_name}/...")

    arch_paths = {
        "source": "kernel-rpms/src",
        "x86_64": "kernel-rpms/binary/x86_64",
        "aarch64": "kernel-rpms/binary/aarch64",
    }

    for arch, rpm_list in files.items():
        if not rpm_list:
            continue

        arch_label = "source" if arch == "source" else f"{arch} binary"
        print(f"  Uploading {len(rpm_list)} {arch_label} RPMs...")

        for rpm_file in rpm_list:
            key = f"{arch_paths[arch]}/{rpm_file.name}"
            size_mb = rpm_file.stat().st_size / (1024 * 1024)
            start = time.time()
            try:
                s3.upload_file(str(rpm_file), bucket_name, key)
                elapsed = time.time() - start
                if verify_s3_upload(s3, bucket_name, key, rpm_file):
                    print(f"    SUCCESS: {key} ({size_mb:.1f} MB in {elapsed:.1f}s)")
                else:
                    print(f"    ERROR: {key} - verification failed")
                    return False
            except Exception as e:
                print(f"    ERROR: Failed to upload {key}: {e}")
                return False

    print("\nSUCCESS: Upload complete!")
    return True


def main():
    """Main function to upload local kernel RPMs to S3."""
    parser = argparse.ArgumentParser(description="Upload local kernel RPMs to S3 bucket")
    parser.add_argument(
        "--bucket",
        required=True,
        help="S3 bucket to upload kernel RPMs",
    )
    parser.add_argument(
        "--local-rpms",
        nargs="+",
        required=True,
        help="Local RPM files or directories containing RPMs",
    )
    parser.add_argument(
        "--region",
        default="us-west-2",
        help="AWS region (default: us-west-2)",
    )
    parser.add_argument(
        "--clean-bucket",
        action="store_true",
        help="Clean bucket before upload",
    )

    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=args.region)

    if bucket_exists(s3, args.bucket):
        print(f"Bucket s3://{args.bucket}/ already exists")
    else:
        print(f"Creating bucket s3://{args.bucket}/...")
        if not create_bucket(s3, args.bucket, args.region):
            print("Error: Failed to create S3 bucket")
            return 1

    if args.clean_bucket:
        print(f"\n{'=' * 60}")
        print("Cleaning S3 bucket")
        print(f"{'=' * 60}")
        clean_s3_bucket(args.bucket, region=args.region)

    print(f"\n{'=' * 60}")
    print("Collecting local RPMs")
    print(f"{'=' * 60}")
    files = collect_local_rpms(args.local_rpms)

    total = sum(len(v) for v in files.values())
    if total == 0:
        print("Error: No RPM files found")
        return 1

    overall_start = time.time()
    if not upload_to_s3(files, args.bucket, args.region):
        print("ERROR: Upload failed")
        return 1

    overall_elapsed = time.time() - overall_start
    print(f"\n{'=' * 60}")
    print(f"SUCCESS: Completed in {overall_elapsed:.1f}s")
    print(f"  Uploaded: {total} RPMs")
    print(f"  Bucket: s3://{args.bucket}/")
    print(f"{'=' * 60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
