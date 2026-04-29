"""Integration test for full pipeline execution."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import json
import sys
from pathlib import Path

import pytest

# Add src to path and import modules to register them
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@pytest.mark.integration
def test_full_pipeline_execution():  # pylint: disable=too-many-statements
    """Test complete pipeline: upload tests, spawn VM, retrieve results."""
    import importlib
    import logging
    import pkgutil

    from kernel_ci_cloud_labs.core.logging_config import (
        create_run_directory,
        setup_run_logging,
    )
    from kernel_ci_cloud_labs.core.pipeline import run_pipeline
    from kernel_ci_cloud_labs.core.registry import get_auth, get_provider, get_storage

    logger = logging.getLogger(__name__)

    # Import all packages to register providers, storage, and auth
    for pkg in [
        "kernel_ci_cloud_labs.providers",
        "kernel_ci_cloud_labs.storage",
        "kernel_ci_cloud_labs.auth",
    ]:
        package = importlib.import_module(pkg)
        for _, module_name, _ in pkgutil.iter_modules(package.__path__):
            importlib.import_module(f"{pkg}.{module_name}")

    # Use main config
    config_path = "examples/aws/config.json"

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    # Fail early if config still has uncustomized template defaults
    default_names = {"kernel-ci-results", "kernel-ci-test", "ecsTaskExecutionRole", "kernel-ci-exampleuser-cluster"}
    actual_names = {
        config["storage"]["bucket"],
        config["ecs"]["cluster"],
        list(config["roles"].keys())[0],
    }
    if actual_names & default_names:
        pytest.fail(
            "Config still has template defaults — run 'kernel-ci-cloud-runner aws setup configure "
            "--prefix kernel-ci-$USER- --region <REGION>' first to personalize resource names."
        )

    # Load credentials from credentials.json if exists
    credentials_path = "examples/aws/credentials.json"
    credentials = None
    if Path(credentials_path).exists():
        with open(credentials_path, encoding="utf-8") as f:
            credentials = json.load(f)
            logger.info("Credentials loaded!")

    # Override test_id to show always integration_test
    config["test_config"]["test_id"] = "integration_test"

    # Use basic-test on one x86_64 and one arm64 VM — fast, no kernel RPMs needed
    role_name = config["test_config"]["role_name"]
    config["test_config"]["vms"] = [
        {
            "ami_id": "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64",
            "instance_type": "t3.micro",
            "max_runtime": 300,
            "test": ["basic-test"],
            "role_name": role_name,
            "min_count": 1,
        },
        {
            "ami_id": "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-arm64",
            "instance_type": "t4g.micro",
            "max_runtime": 300,
            "test": ["basic-test"],
            "role_name": role_name,
            "min_count": 1,
        },
    ]

    try:
        # Create run directory and setup logging
        run_dir = create_run_directory(base_dir="tests/integration/logs")
        setup_run_logging(run_dir)

        # Initialize components using same flow as main.py
        auth_class = get_auth(config["auth_credentials"]["auth_provider"])
        storage_class = get_storage(config["storage"]["type"])
        provider_class = get_provider(config["provider"])

        # Create auth
        auth = auth_class(config, credentials)

        # Create storage - merge storage config with region and external_storage
        storage_config = {
            **config["storage"],
            "region": config.get("region"),
            "external_storage": config.get("external_storage", {}),
        }
        storage = storage_class(storage_config, auth)

        # Create provider
        provider = provider_class(auth, config, storage)

        # Step 1: Validate authentication
        logger.info("\n=== Step 1: Authentication ===")
        sts_client = auth.get_client("sts")
        identity = sts_client.get_caller_identity()
        assert "Arn" in identity, "Failed to get AWS identity"
        logger.info("✓ Authenticated as: %s", identity["Arn"])

        # Step 2: Validate AWS resources exist
        logger.info("\n=== Step 2: AWS Resources ===")
        ecs_client = auth.get_client("ecs")
        logs_client = auth.get_client("logs")
        iam_client = auth.get_client("iam")

        # Check IAM role
        role_name = list(config["roles"].keys())[0]
        role = iam_client.get_role(RoleName=role_name)
        assert role["Role"]["RoleName"] == role_name
        logger.info("✓ IAM role exists: %s", role_name)

        # Check ECS cluster
        cluster_name = config["ecs"]["cluster"]
        clusters = ecs_client.describe_clusters(clusters=[cluster_name])
        assert len(clusters["clusters"]) == 1
        assert clusters["clusters"][0]["clusterName"] == cluster_name
        logger.info("✓ ECS cluster exists: %s", cluster_name)

        # Check task definition
        task_family = config["ecs"]["task_definition"]["family"]
        task_def = ecs_client.describe_task_definition(taskDefinition=task_family)
        assert task_def["taskDefinition"]["family"] == task_family
        logger.info("✓ Task definition exists: %s", task_family)

        # Check CloudWatch log groups
        for log_group in config.get("cloudwatch", {}).get("log_groups", {}):
            log_groups = logs_client.describe_log_groups(logGroupNamePrefix=log_group)
            assert len(log_groups["logGroups"]) >= 1
            logger.info("✓ CloudWatch log group exists: %s", log_group)

        # Step 3: Run pipeline (handles test uploads internally)
        logger.info("\n=== Step 3: Run Pipeline ===")
        run_pipeline(provider, storage, run_dir=run_dir)

        # Step 4: Validate uploads happened
        logger.info("\n=== Step 4: Validate S3 Uploads ===")
        test_list = config["test_config"]["vms"][0]["test"]
        test_name = test_list[0] if isinstance(test_list, list) else test_list

        # Check that test payload was uploaded by pipeline
        s3 = auth.get_client("s3")
        bucket = storage.bucket  # Use actual bucket name from storage

        # Get run_prefix from storage
        run_prefix = storage.run_prefix
        assert run_prefix is not None, "run_prefix not set by pipeline"
        # Get run_prefix from summary
        logger.info("Run prefix: %s", run_prefix)

        payload_key = f"{run_prefix}/test_{test_name}/input/{test_name}_test_payload.zip"
        try:
            s3.head_object(Bucket=bucket, Key=payload_key)
            logger.info("✓ Test payload uploaded to S3: %s", bucket)
        except Exception as e:
            raise AssertionError(f"Test payload not found in S3 bucket {bucket}: {payload_key}") from e

        # Step 5: Validate logs exist and have content
        logger.info("\n=== Step 5: Validate Logs ===")
        pipeline_log = Path(run_dir) / "pipeline.log"
        container_log = Path(run_dir) / "container.log"
        summary_file = Path(run_dir) / "summary.json"

        assert pipeline_log.exists(), "Pipeline log not created"
        assert container_log.exists(), "Container log not created"
        assert summary_file.exists(), "Summary not created"

        # Check pipeline log
        pipeline_content = pipeline_log.read_text(encoding="utf-8")
        assert len(pipeline_content) > 0, "Pipeline log is empty"
        assert "Pipeline Finished" in pipeline_content, "Pipeline did not complete"

        # Check container log - verify VMs were launched
        container_content = container_log.read_text(encoding="utf-8")
        assert len(container_content) > 0, "Container log is empty"
        assert "All VMs completed" in container_content, "VMs did not complete - check container log"
        assert (
            "successful" in container_content and "failed" in container_content
        ), "VM success/failure summary not found"
        assert (
            "SUCCESS: All VMs launched and tested successfully" in container_content
        ), "Not all VMs succeeded - check container log"
        logger.info("✓ VMs completed successfully")

        # Check VM logs directory exists
        vms_dir = Path(run_dir) / "vms"
        assert vms_dir.exists(), "VM logs directory not created"
        vm_log_files = list(vms_dir.glob("*.log"))
        assert len(vm_log_files) > 0, "No VM log files found"
        logger.info("✓ Found %d VM log file(s)", len(vm_log_files))

        # Check VM logs contain test execution
        for vm_log in vm_log_files:
            vms_content = vm_log.read_text()
            assert len(vms_content) > 0, f"VM log {vm_log.name} is empty"
        logger.info("✓ VM logs contain test execution")

        # Step 6: Validate pipeline success
        logger.info("\n=== Step 6: Validate Pipeline Success ===")
        with open(summary_file, encoding="utf-8") as f:
            summary = json.load(f)

        # Calculate expected count from config
        expected_count = sum(vm.get("min_count", 1) for vm in config["test_config"]["vms"])

        assert summary["status"] in ["success", "partial_failure"], f"Pipeline failed: {summary}"
        assert (
            summary["vms"]["expected"] == expected_count
        ), f"Expected VM count mismatch: {summary['vms']['expected']} != {expected_count}"
        assert summary["vms"]["actual"] >= 1, "At least 1 VM should have spawned"
        logger.info(
            "✓ Pipeline completed: %d/%d VMs spawned",
            summary["vms"]["actual"],
            summary["vms"]["expected"],
        )

        # Step 7: Validate test results in S3
        logger.info("\n=== Step 7: Validate S3 Results ===")

        # List objects under run_prefix to verify results were uploaded
        try:
            response = s3.list_objects_v2(Bucket=bucket, Prefix=f"{run_prefix}/", MaxKeys=10)
            if "Contents" in response:
                logger.info(
                    "✓ Found %d objects in S3 under %s",
                    len(response["Contents"]),
                    run_prefix,
                )
                for obj in response["Contents"][:]:  # Show all objects
                    logger.info("  - %s", obj["Key"])
            else:
                logger.warning("No S3 objects found under %s", run_prefix)
        except Exception as e:
            logger.warning("Could not list S3 objects: %s", e)

    except Exception as e:
        logger.error("Integration test failed: %s", e)
        raise
