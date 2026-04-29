"""EventBridge handler for automated pipeline triggering.

This module provides a Lambda-compatible handler that can be invoked by
Amazon EventBridge (scheduled rules or custom events) to run the kernel CI
pipeline automatically.

Expected EventBridge event payload:
    {
        "config_s3_uri": "s3://kernel-ci-myname-storage/configs/config.json",
        "region": "eu-west-2"
    }

For scheduled rules (e.g. daily regression runs), configure the EventBridge
rule with a constant JSON input containing the above fields.

Setup (AWS CLI):
    # 1. Create a Lambda function from this handler
    #    (package kernel_ci_cloud_labs and dependencies into a deployment zip)
    #
    # 2. Create a scheduled EventBridge rule (e.g. daily at 02:00 UTC):
    #    aws events put-rule \\
    #      --name kernel-ci-daily-regression \\
    #      --schedule-expression "cron(0 2 * * ? *)" \\
    #      --state ENABLED \\
    #      --region eu-west-2
    #
    # 3. Add the Lambda as target with the config payload:
    #    aws events put-targets \\
    #      --rule kernel-ci-daily-regression \\
    #      --targets '[{
    #        "Id": "kernel-ci-pipeline",
    #        "Arn": "arn:aws:lambda:<REGION>:<ACCOUNT>:function:<FUNCTION_NAME>",
    #        "Input": "{
    #          \\"config_s3_uri\\": \\"s3://kernel-ci-myname-storage/configs/config.json\\",
    #          \\"region\\": \\"eu-west-2\\"
    #        }"
    #      }]' \\
    #      --region eu-west-2
    #
    # 4. Grant EventBridge permission to invoke the Lambda:
    #    aws lambda add-permission \\
    #      --function-name <FUNCTION_NAME> \\
    #      --statement-id eventbridge-invoke \\
    #      --action lambda:InvokeFunction \\
    #      --principal events.amazonaws.com \\
    #      --source-arn arn:aws:events:<REGION>:<ACCOUNT>:rule/kernel-ci-daily-regression
    #
    # The config.json in S3 must use resource names matching your prefix
    # (as produced by `kernel-ci-cloud-runner aws setup configure`).
    # All AWS resources (ECS cluster, IAM roles, ECR image, etc.) must
    # already exist — the handler only triggers the pipeline, it does not
    # create infrastructure.
"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import json
import logging
import os
import tempfile
import uuid

import boto3

from kernel_ci_cloud_labs.core.logging_config import (
    create_run_directory,
    get_logger,
    setup_run_logging,
)

logger = get_logger(__name__)


def _download_config(s3_uri, region):
    """Download config JSON from S3 to a local temporary file.

    Returns:
        Path to the downloaded temporary config file.
    """
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI (must start with s3://): {s3_uri}")

    parts = s3_uri.replace("s3://", "").split("/", 1)
    if len(parts) != 2 or not parts[1]:
        raise ValueError(f"Invalid S3 URI (expected s3://bucket/key): {s3_uri}")

    bucket, key = parts
    s3 = boto3.client("s3", region_name=region)
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, prefix="eventbridge-config-")
    s3.download_file(bucket, key, tmp.name)
    logger.info("Downloaded config from %s to %s", s3_uri, tmp.name)
    return tmp.name


def _prepare_kernel_rpms(config, region):  # pylint: disable=unused-argument
    """Prepare kernel RPMs for the pipeline run.

    This is a placeholder for automatic kernel RPM retrieval. The current
    implementation requires RPMs to be pre-uploaded to the external storage
    bucket (see `kernel-ci-cloud-runner aws setup upload-rpms`).

    A future implementation should:
      1. Determine the tip kernel version (e.g. latest build from KernelCI
         or a koji/brew build system, or a URL passed in the EventBridge
         event payload).
      2. Determine the base kernel version(s) to compare against (e.g. the
         previous stable release, or the currently running production kernel).
      3. Download the RPMs for both versions and both architectures
         (x86_64, aarch64) to a temporary directory.
      4. Upload them to the external storage bucket using the same structure
         that `setup_upload_rpms.py` produces:
           s3://<external-bucket>/kernel-rpms/binary/x86_64/*.rpm
           s3://<external-bucket>/kernel-rpms/binary/aarch64/*.rpm
      5. Update config["test_config"]["vms"][*]["test_params"] with the
         kernel versions so that vm-tests can pick them up.

    Sources to consider:
      - KernelCI API: https://api.kernelci.org/
      - Koji (Fedora/AL2023): query latest builds via koji CLI or XML-RPC
      - Direct HTTP download from a build artifact server

    Args:
        config: Pipeline configuration dict (mutable — update in place).
        region: AWS region for S3 operations.
    """
    logger.info("Kernel RPM preparation: using pre-uploaded RPMs from external storage bucket")
    # TODO(kernel-rpms): implement automatic kernel RPM retrieval  # noqa: TD003


def _make_config_run_local(config):
    """Create a run-local copy of the config to avoid conflicts with parallel events.

    Appends a unique suffix to the test_id so that each EventBridge invocation
    writes to its own S3 prefix and does not collide with concurrent runs.
    """
    run_id = uuid.uuid4().hex[:8]
    test_config = config.get("test_config", {})
    base_test_id = test_config.get("test_id", "eventbridge")
    test_config["test_id"] = f"{base_test_id}-{run_id}"
    logger.info("Run-local test_id: %s", test_config["test_id"])
    return config


def handle_eventbridge(event, context=None):
    """Lambda / EventBridge entry point for automated pipeline runs.

    Workflow:
      1. Download pipeline config from S3 (URI from event payload).
      2. Prepare kernel RPMs (placeholder — currently expects pre-uploaded).
      3. Make config run-local (unique test_id to avoid parallel conflicts).
      4. Run the normal pipeline.

    Args:
        event: EventBridge event dict. Required keys:
            - config_s3_uri (str): S3 URI to the pipeline config JSON.
            - region (str, optional): AWS region. Defaults to AWS_DEFAULT_REGION
              or us-west-2.
        context: Lambda context (unused, present for Lambda compatibility).

    Returns:
        dict with status and run details.
    """
    # Setup logging for this invocation
    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    run_dir = create_run_directory()
    setup_run_logging(run_dir, level=log_level)

    invocation_id = uuid.uuid4().hex[:12]
    logger.info("=== EventBridge Handler Invoked ===")
    logger.info("Invocation ID: %s", invocation_id)
    logger.info("Event: %s", json.dumps(event, default=str))
    if context:
        logger.info("Lambda request ID: %s", getattr(context, "aws_request_id", "N/A"))

    # Extract parameters from event
    config_s3_uri = event.get("config_s3_uri")
    if not config_s3_uri:
        logger.error("Missing required field 'config_s3_uri' in event payload")
        return {"status": "error", "message": "Missing config_s3_uri in event"}

    region = event.get("region", os.getenv("AWS_DEFAULT_REGION", "us-west-2"))
    logger.info("Config S3 URI: %s", config_s3_uri)
    logger.info("Region: %s", region)

    try:
        # Step 1: Download config from S3
        config_path = _download_config(config_s3_uri, region)

        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)

        # Step 2: Prepare kernel RPMs (placeholder for future automation)
        _prepare_kernel_rpms(config, region)

        # Step 3: Make config run-local for parallel safety
        config = _make_config_run_local(config)

        # Write updated config back to temp file (pipeline reads from file)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        # Step 4: Run the pipeline
        logger.info("Starting pipeline from EventBridge trigger")

        from kernel_ci_cloud_labs.core.pipeline import run_pipeline
        from kernel_ci_cloud_labs.core.registry import (
            AUTH_REGISTRY,
            PROVIDER_REGISTRY,
            STORAGE_REGISTRY,
        )
        from kernel_ci_cloud_labs.main import import_all_packages, load_credentials

        for pkg in [
            "kernel_ci_cloud_labs.providers",
            "kernel_ci_cloud_labs.storage",
            "kernel_ci_cloud_labs.auth",
        ]:
            import_all_packages(pkg)

        credentials = load_credentials(config_path)
        auth = AUTH_REGISTRY[config["auth_credentials"]["auth_provider"]](config, credentials)
        storage_config = {
            **config["storage"],
            "region": config.get("region"),
            "external_storage": config.get("external_storage", {}),
        }
        storage = STORAGE_REGISTRY[config["storage"]["type"]](storage_config, auth)
        provider = PROVIDER_REGISTRY[config["provider"]](auth, config, storage)

        run_pipeline(provider, storage, run_dir=run_dir)

        logger.info("=== EventBridge Handler Completed Successfully ===")
        return {
            "status": "success",
            "invocation_id": invocation_id,
            "test_id": config["test_config"]["test_id"],
            "run_dir": str(run_dir),
        }

    except Exception as e:
        logger.error("EventBridge handler failed: %s", e, exc_info=True)
        return {"status": "error", "invocation_id": invocation_id, "message": str(e)}

    finally:
        # Clean up temp config file
        if "config_path" in locals():
            try:
                os.unlink(config_path)
            except OSError:
                pass


# Lambda handler alias — use this as the Lambda handler entry point:
#   kernel_ci_cloud_labs.eventbridge_handler.lambda_handler
lambda_handler = handle_eventbridge
