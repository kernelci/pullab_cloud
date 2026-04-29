"""AWS S3 storage implementation."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from typing import Optional

import boto3

from kernel_ci_cloud_labs.core.base_storage import BaseStorage
from kernel_ci_cloud_labs.core.logging_config import get_logger
from kernel_ci_cloud_labs.core.registry import register_storage

logger = get_logger(__name__)


@register_storage("s3")
class S3Storage(BaseStorage):
    """Storage backend that saves results to AWS S3."""

    def __init__(self, config: Optional[dict] = None, auth: Optional["AWSAuth"] = None):  # type: ignore # noqa: F821
        if config is None:
            config = {}

        self.bucket = config.get("bucket")
        if not self.bucket:
            raise ValueError("S3 bucket name is required in storage config")
        self.results_prefix = config.get("results_prefix", "results")
        self.region = config.get("region", "us-west-2")
        self.external_storage = config.get("external_storage", {})

        # Run-specific prefix (set by pipeline)
        self.run_prefix = None

        # Use auth object if provided, otherwise create default client
        if auth:
            self.s3 = auth.get_client("s3")
        else:
            self.s3 = boto3.client("s3", region_name=self.region)

        # Create bucket if it doesn't exist
        self._ensure_bucket(auth)

    def set_run_prefix(self, run_prefix: str):
        """Set the run prefix for S3 paths: run_{test_id}_{datetime}/"""
        self.run_prefix = run_prefix
        logger.info("S3 run prefix set to: %s", run_prefix)

    def _ensure_bucket(self, auth: Optional["AWSAuth"] = None):  # type: ignore # noqa: F821
        """Create S3 bucket if it doesn't exist."""
        from botocore.exceptions import ClientError

        try:
            self.s3.head_bucket(Bucket=self.bucket)
            logger.info("S3 bucket exists: %s", self.bucket)
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ("403", "404"):
                # If bucket exists but not accessible (403), append account ID
                if error_code == "403":
                    logger.warning("Bucket %s exists but not accessible", self.bucket)
                    # Get account ID using auth object's STS client
                    if auth:
                        sts = auth.get_client("sts")
                    else:
                        sts = boto3.client("sts", region_name=self.region)
                    account_id = sts.get_caller_identity()["Account"]
                    self.bucket = f"{self.bucket}-{account_id}"
                    logger.info("Trying bucket with account ID: %s", self.bucket)
                    # Check if new bucket name works
                    try:
                        self.s3.head_bucket(Bucket=self.bucket)
                        logger.info("✓ Using existing bucket: %s", self.bucket)
                        return
                    except ClientError:
                        pass  # Will create below

                logger.info("Creating S3 bucket: %s", self.bucket)
                if self.region == "us-east-1":
                    self.s3.create_bucket(Bucket=self.bucket)
                else:
                    self.s3.create_bucket(
                        Bucket=self.bucket,
                        CreateBucketConfiguration={"LocationConstraint": self.region},
                    )
                logger.info("✓ Created S3 bucket: %s", self.bucket)
            else:
                logger.error("Error checking bucket %s: %s", self.bucket, e)
                raise

    def save_results(self, data):
        logger.info("Saving: %s", data)

    def save_file(self, key, content):
        """Save file content to S3."""

        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=content)
        except Exception as e:
            logger.error("Failed to upload %s: %s", key, e)

    def upload_string(self, content, key):
        """Upload string content to S3."""
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=content.encode("utf-8"))
        except Exception as e:
            logger.error("Failed to upload %s: %s", key, e)

    def upload_tests(self, test_name, test_id, test_dir="vm-tests", script_name="test-vm-client.sh"):
        """Upload test script — from local filesystem or external storage bucket."""
        import hashlib
        import os

        from botocore.exceptions import ClientError

        client_script = os.path.join(test_dir, script_name) if os.path.exists(test_dir) else None
        s3_key = f"{self.run_prefix}/test_{test_name}/input/{script_name}"

        if client_script and os.path.exists(client_script):
            # Local file available — upload directly
            with open(client_script, "rb") as f:
                local_content = f.read()
            local_hash = hashlib.md5(local_content).hexdigest()

            try:
                response = self.s3.head_object(Bucket=self.bucket, Key=s3_key)
                if response["ETag"].strip('"') == local_hash:
                    logger.info("✓ %s already up-to-date in S3", script_name)
                    return True
            except ClientError:
                pass

            self.save_file(s3_key, local_content)
            logger.info("✓ Uploaded %s from local", script_name)
            return True

        # No local file — copy from external storage bucket
        return self._copy_from_external_storage(f"test-scripts/{script_name}", s3_key, script_name)

    def upload_test_payload(self, test_name, test_dir="vm-tests"):
        """Create and upload a zip of the selected test for VM execution.

        Uses local vm-tests/ if available, otherwise copies pre-uploaded
        test payload from the external storage bucket.
        """
        import hashlib
        import os
        import tempfile
        import zipfile
        from pathlib import Path

        from botocore.exceptions import ClientError

        test_path = os.path.join(test_dir, test_name)
        s3_key = f"{self.run_prefix}/test_{test_name}/input/{test_name}_test_payload.zip"

        if os.path.exists(test_path):
            # Local test directory available — zip and upload, copy external requirements
            self.copy_external_requirements(test_name, test_path)

            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                zip_path = tmp.name

            try:
                logger.info("Creating test payload for '%s'", test_name)
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                    for file_path in Path(test_path).rglob("*"):
                        if file_path.is_file():
                            zf.write(file_path, file_path.relative_to(test_path))

                with open(zip_path, "rb") as f:
                    zip_content = f.read()
                local_hash = hashlib.md5(zip_content).hexdigest()

                try:
                    response = self.s3.head_object(Bucket=self.bucket, Key=s3_key)
                    if response["ETag"].strip('"') == local_hash:
                        logger.info("✓ Test payload already up-to-date in S3")
                        return True
                except ClientError:
                    pass

                self.save_file(s3_key, zip_content)
                logger.info("✓ Uploaded test payload to s3://%s/%s", self.bucket, s3_key)
                return True
            finally:
                if os.path.exists(zip_path):
                    os.remove(zip_path)

        # No local test directory — copy from external storage bucket
        source_key = f"test-scripts/{test_name}/{test_name}_test_payload.zip"
        if not self._copy_from_external_storage(source_key, s3_key, f"{test_name} payload"):
            return False

        # Also copy external requirements (kernel RPMs etc.) from external storage
        self._copy_external_requirements_from_s3(test_name)
        return True

    def copy_external_requirements(self, test_name, test_path):
        """Copy external requirements from external storage to shared location.

        Copies all architectures for kernel-rpms/binary - the VM will select the correct one
        based on its ARCH environment variable. Resources are uploaded once to a shared
        location to avoid duplication across tests.
        """
        import json
        import os

        requirements_file = os.path.join(test_path, "external_requirements.json")
        if not os.path.exists(requirements_file):
            return

        try:
            with open(requirements_file, "r", encoding="utf-8") as f:
                requirements = json.load(f)
        except Exception as e:
            logger.error("Failed to read external_requirements.json: %s", e)
            return

        if not self.external_storage:
            logger.warning("No external_storage configured, skipping external requirements")
            return

        source_bucket = self.external_storage.get("bucket", "")
        if not source_bucket:
            logger.warning("Invalid external_storage bucket configuration")
            return

        logger.info("Copying external requirements for test '%s'", test_name)

        for folder_name, enabled in requirements.items():
            if not enabled:
                continue

            source_prefix = f"{folder_name}/"
            dest_prefix = f"{self.run_prefix}/shared/{folder_name}/"

            # Check if already copied to shared location
            if self._check_s3_folder_exists(self.bucket, dest_prefix):
                logger.info("✓ %s already exists in shared location, skipping", folder_name)
                continue

            try:
                self._copy_s3_folder(source_bucket, source_prefix, self.bucket, dest_prefix)
                logger.info("✓ Copied %s to shared location", folder_name)
            except Exception as e:
                logger.error("Failed to copy %s: %s", folder_name, e)

    def _copy_s3_folder(self, source_bucket, source_prefix, dest_bucket, dest_prefix):
        """Copy all files from source S3 location to destination, preserving structure."""
        paginator = self.s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=source_bucket, Prefix=source_prefix)

        for page in pages:
            if "Contents" not in page:
                continue

            for obj in page["Contents"]:
                source_key = obj["Key"]
                if source_key.endswith("/"):
                    continue

                # Keep the full path structure from source
                # Just replace the source_prefix with dest_prefix
                dest_key = source_key.replace(source_prefix, dest_prefix, 1)

                copy_source = {"Bucket": source_bucket, "Key": source_key}
                self.s3.copy_object(CopySource=copy_source, Bucket=dest_bucket, Key=dest_key)
                logger.debug(
                    "Copied s3://%s/%s -> s3://%s/%s",
                    source_bucket,
                    source_key,
                    dest_bucket,
                    dest_key,
                )

    def _copy_from_external_storage(self, source_key, dest_key, label):
        """Copy a single object from the external storage bucket to the results bucket."""
        if not self.external_storage:
            logger.error("No external_storage configured, cannot copy %s", label)
            return False

        source_bucket = self.external_storage.get("bucket", "")
        if not source_bucket:
            logger.error("No external_storage bucket configured")
            return False

        try:
            self.s3.copy_object(
                CopySource={"Bucket": source_bucket, "Key": source_key},
                Bucket=self.bucket,
                Key=dest_key,
            )
            logger.info("✓ Copied %s from s3://%s/%s", label, source_bucket, source_key)
            return True
        except Exception as e:
            logger.error("Failed to copy %s from external storage: %s", label, e)
            return False

    def _copy_external_requirements_from_s3(self, test_name):
        """Copy external requirements using the requirements JSON from the external storage bucket."""
        import json

        if not self.external_storage:
            return

        source_bucket = self.external_storage.get("bucket", "")
        if not source_bucket:
            return

        # Read external_requirements.json from the test payload in external storage
        req_key = f"test-scripts/{test_name}/external_requirements.json"
        try:
            resp = self.s3.get_object(Bucket=source_bucket, Key=req_key)
            requirements = json.loads(resp["Body"].read().decode("utf-8"))
        except Exception:
            # No requirements file — test doesn't need external artifacts
            return

        for folder_name, enabled in requirements.items():
            if not enabled:
                continue

            dest_prefix = f"{self.run_prefix}/shared/{folder_name}/"
            if self._check_s3_folder_exists(self.bucket, dest_prefix):
                logger.info("✓ %s already exists in shared location, skipping", folder_name)
                continue

            try:
                self._copy_s3_folder(source_bucket, f"{folder_name}/", self.bucket, dest_prefix)
                logger.info("✓ Copied %s to shared location", folder_name)
            except Exception as e:
                logger.error("Failed to copy %s: %s", folder_name, e)

    def _check_s3_folder_exists(self, bucket, prefix):
        """Check if any objects exist with the given prefix."""
        try:
            response = self.s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
            return "Contents" in response and len(response["Contents"]) > 0
        except Exception:
            return False
