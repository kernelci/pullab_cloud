"""Upload local vm-tests to S3 for use in EventBridge-triggered pipeline runs."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import boto3


def upload_test_scripts(s3_client, bucket, test_dir="vm-tests"):
    """Upload test scripts to S3 external storage bucket.

    Uploads test-vm-client.sh and a zip per test directory to:
        s3://bucket/test-scripts/test-vm-client.sh
        s3://bucket/test-scripts/<test-name>/<test-name>_test_payload.zip

    Returns:
        Number of tests uploaded.
    """
    test_path = Path(test_dir)
    if not test_path.exists():
        print(f"Error: Test directory not found: {test_dir}", file=sys.stderr)
        return 0

    # Upload test-vm-client.sh
    client_script = test_path / "test-vm-client.sh"
    if client_script.exists():
        s3_client.upload_file(str(client_script), bucket, "test-scripts/test-vm-client.sh")
        print("  ✓ Uploaded test-vm-client.sh")
    else:
        print(f"  ✗ test-vm-client.sh not found in {test_dir}", file=sys.stderr)

    # Upload each test as a zip
    count = 0
    for entry in sorted(test_path.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        # Check it has at least one run script
        run_scripts = list(entry.glob("run*.sh"))
        if not run_scripts:
            continue

        zip_name = f"{entry.name}_test_payload.zip"
        s3_key = f"test-scripts/{entry.name}/{zip_name}"

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            zip_path = tmp.name

        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                for file_path in entry.rglob("*"):
                    if file_path.is_file():
                        zf.write(file_path, file_path.relative_to(entry))

            s3_client.upload_file(zip_path, bucket, s3_key)
            print(f"  ✓ Uploaded {entry.name} ({len(run_scripts)} scripts)")
            count += 1

            # Upload external_requirements.json separately so the pipeline
            # can read it without extracting the zip
            req_file = entry / "external_requirements.json"
            if req_file.exists():
                req_key = f"test-scripts/{entry.name}/external_requirements.json"
                s3_client.upload_file(str(req_file), bucket, req_key)
        finally:
            if os.path.exists(zip_path):
                os.unlink(zip_path)

    return count


def main():
    """CLI entry point for uploading test scripts."""
    parser = argparse.ArgumentParser(description="Upload vm-tests to S3 for EventBridge-triggered runs")
    parser.add_argument("--bucket", required=True, help="S3 external storage bucket")
    parser.add_argument("--test-dir", default="vm-tests", help="Local test directory (default: vm-tests)")
    parser.add_argument("--region", default="us-west-2", help="AWS region (default: us-west-2)")

    args = parser.parse_args()
    s3 = boto3.client("s3", region_name=args.region)

    print(f"Uploading tests from {args.test_dir} to s3://{args.bucket}/test-scripts/")
    count = upload_test_scripts(s3, args.bucket, args.test_dir)
    print(f"\nUploaded {count} test(s)")
    return 0 if count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
