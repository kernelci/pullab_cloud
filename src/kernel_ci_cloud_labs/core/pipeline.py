"""Pipeline orchestration for running kernel CI tests."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import json
import time
from datetime import datetime, timezone
from pathlib import Path

from kernel_ci_cloud_labs.auth.aws_cloudwatch_manager import AWSCloudWatchManager
from kernel_ci_cloud_labs.core.logging_config import create_run_directory, get_logger

logger = get_logger(__name__)


def parse_vm_logs(vms_dir):
    """Parse VM log files and extract test statistics."""
    stats = {
        "total_vms": 0,
        "successful": 0,
        "failed": 0,
        "test_names": set(),
        "failed_tests": {},
    }

    if not vms_dir.exists():
        return stats

    for log_file in vms_dir.glob("*.log"):
        stats["total_vms"] += 1
        log_name = log_file.stem

        try:
            with open(log_file, "r", encoding="utf-8") as f:
                content = f.read()

            # Extract test name
            test_name = "unknown"
            for line in content.split("\n"):
                if "test_" in line and "/input/" in line:
                    parts = line.split("test_")
                    if len(parts) > 1:
                        test_name = parts[1].split("/")[0]
                        stats["test_names"].add(test_name)
                        break

            # Check success/failure
            if "Test execution completed: SUCCESS" in content:
                stats["successful"] += 1
            else:
                stats["failed"] += 1
                if test_name not in stats["failed_tests"]:
                    stats["failed_tests"][test_name] = []
                stats["failed_tests"][test_name].append(log_name)

        except Exception as e:
            logger.warning("Could not parse VM log %s: %s", log_file, e)

    return stats


def _log_failure_details(container_log, vm_stats, s3_context):
    """Log failure reasons, VM log excerpts, and S3 debug commands for failed VMs."""
    # Extract failure lines from container log
    if container_log.exists():
        try:
            content = container_log.read_text(encoding="utf-8")
            failure_lines = [
                line.strip() for line in content.splitlines() if "FAILED" in line or "✗ Test result" in line
            ]
            if failure_lines:
                logger.info("\n=== Failure Details ===")
                for line in failure_lines:
                    # Strip timestamp prefix like "[2026-03-23 13:37:28] [CONTAINER] "
                    msg = line.split("[CONTAINER] ", 1)[-1] if "[CONTAINER]" in line else line
                    logger.info("  %s", msg)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    if not s3_context or not vm_stats.get("failed_tests"):
        return

    bucket = s3_context.get("bucket", "")
    run_prefix = s3_context.get("run_prefix", "")
    region = s3_context.get("region", "")
    s3_client = s3_context.get("s3_client")
    region_flag = f" --region {region}" if region else ""

    # Download and show warnings/errors from one failed VM per test
    if s3_client:
        _log_vm_output_excerpts(s3_client, bucket, run_prefix, vm_stats["failed_tests"])

    # Print S3 debug commands
    logger.info("\n=== Debug Commands ===")
    for test_name, instance_ids in vm_stats["failed_tests"].items():
        for instance_id in instance_ids:
            logger.info(
                "  # %s on %s — download last run output:",
                test_name,
                instance_id,
            )
            output_prefix = f"s3://{bucket}/{run_prefix}/test_{test_name}/output/{instance_id}/"
            logger.info("  aws s3 ls %s%s", output_prefix, region_flag)
            logger.info("  aws s3 cp %sresult.txt -%s", output_prefix, region_flag)


def _log_vm_output_excerpts(s3_client, bucket, run_prefix, failed_tests):
    """Download the last run-N-output.log for one VM per failed test and show warnings/errors."""
    import re

    warn_re = re.compile(r"warning|WARNING|warn\b", re.IGNORECASE)
    error_re = re.compile(r"error|ERROR|FAILED", re.IGNORECASE)
    max_lines = 3

    for test_name, instance_ids in failed_tests.items():
        instance_id = instance_ids[0]  # one representative per test
        try:
            # Find the highest run-N-output.log (the one that failed)
            prefix = f"{run_prefix}/test_{test_name}/output/{instance_id}/"
            resp = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
            run_logs = sorted(
                [o["Key"] for o in resp.get("Contents", []) if "run-" in o["Key"] and "-output.log" in o["Key"]]
            )
            if not run_logs:
                continue
            last_log_key = run_logs[-1]
            obj = s3_client.get_object(Bucket=bucket, Key=last_log_key)
            content = obj["Body"].read().decode("utf-8", errors="replace")
            lines = content.splitlines()

            # Strip shell trace prefix (+ command) to match on actual output
            warnings = [line for line in lines if warn_re.search(line) and not line.startswith("+")][:max_lines]
            errors = [line for line in lines if error_re.search(line) and not line.startswith("+")][:max_lines]

            if warnings or errors:
                log_name = last_log_key.rsplit("/", 1)[-1]
                logger.info("\n=== VM Log Excerpt: %s (%s) ===", test_name, log_name)
                if warnings:
                    for w in warnings:
                        logger.info("  WARN:  %s", w.strip()[:200])
                if errors:
                    for e in errors:
                        logger.info("  ERROR: %s", e.strip()[:200])
                if len(instance_ids) > 1:
                    logger.info("  (%d more VMs failed for this test)", len(instance_ids) - 1)
        except Exception:  # pylint: disable=broad-exception-caught
            pass


def create_summary(run_dir, start_time, task_arn, expected_vm_count=None, s3_context=None):
    """Create summary.json with VM statistics.

    Args:
        run_dir: Path to the local run log directory.
        start_time: Pipeline start time as epoch seconds.
        task_arn: ARN of the ECS task that ran the tests.
        expected_vm_count: Number of VMs expected to spawn (None if unknown).
        s3_context: Optional dict with 'bucket', 'run_prefix', 'region', 's3_client' for debug output.
    """
    end_time = time.time()
    total_runtime = end_time - start_time
    minutes = int(total_runtime // 60)
    seconds = int(total_runtime % 60)

    # Parse VM logs
    vms_dir = Path(run_dir) / "vms"
    vm_stats = parse_vm_logs(vms_dir)

    # Determine status based on expected vs actual
    status = "success"
    if expected_vm_count is not None and vm_stats["total_vms"] != expected_vm_count:
        status = "partial_failure"
        logger.error(
            "VM count mismatch! Expected: %d, Actual: %d",
            expected_vm_count,
            vm_stats["total_vms"],
        )
    elif vm_stats["failed"] > 0:
        status = "partial_failure"

    summary = {
        "run_directory": str(run_dir),
        "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)),
        "end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time)),
        "total_runtime_seconds": int(total_runtime),
        "total_runtime_formatted": f"{minutes}m {seconds}s",
        "task_arn": task_arn,
        "status": status,
        "vms": {
            "expected": expected_vm_count,
            "actual": vm_stats["total_vms"],
            "successful": vm_stats["successful"],
            "failed": vm_stats["failed"],
            "missing": (expected_vm_count - vm_stats["total_vms"] if expected_vm_count is not None else 0),
            "test_names": sorted(list(vm_stats["test_names"])),
            "failed_by_test": vm_stats["failed_tests"],
        },
    }

    summary_file = Path(run_dir) / "summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    # Log summary
    logger.info("\n=== Pipeline Finished ===")
    logger.info("Total runtime: %d minutes %d seconds", minutes, seconds)
    if expected_vm_count is not None:
        logger.info(
            "VMs: %d/%d spawned, %d successful, %d failed, %d missing",
            vm_stats["total_vms"],
            expected_vm_count,
            vm_stats["successful"],
            vm_stats["failed"],
            expected_vm_count - vm_stats["total_vms"],
        )
    else:
        logger.info(
            "VMs: %d total, %d successful, %d failed",
            vm_stats["total_vms"],
            vm_stats["successful"],
            vm_stats["failed"],
        )
    if vm_stats["test_names"]:
        logger.info("Tests run: %s", ", ".join(sorted(vm_stats["test_names"])))
    logger.info("Summary: %s", summary_file)

    # Show debug info for failed tests
    if vm_stats["failed"] > 0:
        container_log = Path(run_dir) / "container.log"
        _log_failure_details(container_log, vm_stats, s3_context)

    return summary


def run_pipeline(
    provider, storage, run_dir=None
):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Run the standard pipeline sequence for any provider/storage."""
    start_time = time.time()

    # Use provided run directory or create new one
    if run_dir is None:
        run_dir = create_run_directory()

    container_log_file = Path(run_dir) / "container.log"

    logger.info("=== Starting Pipeline ===")
    logger.info("Run directory: %s", run_dir)
    logger.debug("Provider: %s, Storage: %s", type(provider).__name__, type(storage).__name__)

    try:
        # Get test info from config
        test_id = None
        test_names = set()  # Collect unique test names
        expected_vm_count = 0  # Calculate expected VM count
        if hasattr(provider, "config") and provider.config:
            test_config = provider.config.get("test_config", {})
            test_id = test_config.get("test_id")

            # Collect all unique test names from vms array and calculate expected count
            vms = test_config.get("vms", [])
            for vm in vms:
                test_value = vm.get("test")
                # Handle list of tests
                if isinstance(test_value, list):
                    for test_name in test_value:
                        test_names.add(test_name)
                    # Each test in list gets min_count VMs
                    expected_vm_count += vm.get("min_count", 1) * len(test_value)
                # Handle single test
                elif test_value:
                    test_names.add(test_value)
                    expected_vm_count += vm.get("min_count", 1)

        # Generate run prefix: run_{test_id}_{datetime}
        run_timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        run_prefix = f"run_{test_id}_{run_timestamp}"
        logger.info("Run prefix: %s", run_prefix)
        logger.info("Expected VMs to spawn: %d", expected_vm_count)

        # Set run_prefix in storage and provider config
        if hasattr(storage, "set_run_prefix"):
            storage.set_run_prefix(run_prefix)
        if hasattr(provider, "config") and provider.config:
            provider.config["run_prefix"] = run_prefix

        # Upload tests and payloads for all unique test names
        logger.info("\n=== Uploading Tests ===")
        if test_names and test_id:
            for test_name in test_names:
                logger.info("Uploading test script for '%s'", test_name)
                if storage.upload_tests(test_name, test_id):
                    logger.info("✓ Test script uploaded for '%s'", test_name)
                else:
                    logger.warning("✗ Test script upload failed for '%s'", test_name)

                logger.info("Uploading test payload for '%s' (test_id: %s)", test_name, test_id)
                if hasattr(storage, "upload_test_payload"):
                    storage.upload_test_payload(test_name)
                    logger.info("✓ Test payload uploaded for '%s'", test_name)
                else:
                    logger.warning("Storage backend does not support upload_test_payload")
        else:
            logger.warning("No tests configured - skipping uploads")

        # Authenticate and spawn
        if not provider.auth.is_authenticated:
            logger.debug("Starting authentication...")
            provider.authenticate()
            logger.debug("Authentication completed")

        # Wait for AWS resources to propagate if just created
        if hasattr(provider.auth, "resources_were_created") and provider.auth.resources_were_created():
            logger.info("\nWaiting for AWS resources to propagate...")
            provider.auth.wait_for_resources()
        else:
            logger.debug("Using existing resources, no propagation delay needed")

        logger.info("Spawning container...")
        task_arn = provider.spawn_container()
        logger.info("Container spawned: %s", task_arn.split("/")[-1] if task_arn else "None")

        if not task_arn:
            logger.error("Failed to spawn container - task_arn is None")
            raise RuntimeError("Container spawn failed")

        # Wait for container to be running
        logger.info("\nWaiting for container to start...")
        ecs_client = provider.auth.get_client("ecs")
        cluster = provider.config.get("ecs", {}).get("cluster", "default")

        waiter = ecs_client.get_waiter("tasks_running")
        waiter.wait(cluster=cluster, tasks=[task_arn])
        logger.info("✓ Container is running")

        # Show CloudWatch logs while task runs
        logger.info("\nStreaming CloudWatch logs...")
        logger.info("-" * 60)

        # Get CloudWatch manager and task info
        logger.debug("Getting CloudWatch logs client")
        logs_client = provider.auth.get_client("logs")
        if not logs_client:
            logger.error("Failed to get CloudWatch logs client")
            raise RuntimeError("CloudWatch client unavailable")

        # Calculate start time in milliseconds for filtering
        start_time_ms = int(start_time * 1000)

        # Resolve EC2 log group from cloudwatch config
        cw_log_groups = provider.config.get("cloudwatch", {}).get("log_groups", {})
        ec2_log_group = next((k for k in cw_log_groups if "/ec2/" in k), "/ec2/kernel-ci-vms")

        cw_manager = AWSCloudWatchManager(
            logs_client,
            {},
            run_prefix=run_prefix,
            start_time_ms=start_time_ms,
            ec2_log_group=ec2_log_group,
        )

        # Get task ID for log stream
        task_id = task_arn.split("/")[-1]
        log_group = f"/ecs/{provider.config['ecs']['task_definition']['family']}"
        log_stream = f"ecs/{provider.config['ecs']['task_definition']['container_name']}/{task_id}"
        logger.debug("Log stream path: %s/%s", log_group, log_stream)

        # Wait for task to complete (all VMs spawned, tested, and uploaded to S3)
        # This replaces the arbitrary 60-second timeout
        logger.info("Waiting for task to complete all VM operations...")
        try:
            final_status = provider.wait_for_task_completion()
            logger.info("✓ Task execution finished")
            logger.debug("Final status: %s", final_status)
        except Exception as e:
            logger.error("Task did not complete successfully: %s", e)
            raise

        logger.info("-" * 60)

        # Refresh CloudWatch client — credentials may have expired during the wait
        cw_manager.client = provider.auth.get_client("logs")

        # Retrieve all container logs after task completes
        try:
            logger.info("Retrieving container logs...")
            # Get all container logs using paginator (handles large volumes)
            log_events = cw_manager.get_all_logs(log_group, log_stream)

            logger.info("\n=== Container Logs ===")
            logger.info("Writing %d events to: %s", len(log_events), container_log_file)

            # Write container logs to separate file (not to console)
            with open(container_log_file, "w", encoding="utf-8") as f:
                for event in log_events:
                    message = event.get("message", "")
                    timestamp_ms = event.get("timestamp", 0)
                    # Convert milliseconds to readable format
                    timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp_ms / 1000))
                    # Write with formatted timestamp and source
                    f.write(f"[{timestamp_str}] [CONTAINER] {message}\n")

        except Exception as e:  # pylint: disable=broad-exception-caught
            # Broad exception is intentional - log retrieval is optional and shouldn't fail pipeline
            logger.warning("Could not retrieve container logs: %s", e)

        # Retrieve VM logs from CloudWatch - create separate file per instance
        logger.info("\n=== Retrieving VM Logs ===")
        try:
            # Create vms directory
            vms_dir = Path(run_dir) / "vms"
            vms_dir.mkdir(exist_ok=True)

            # Wait for VM logs to appear
            max_retries = 6
            retry_delay = 5

            for attempt in range(max_retries):
                # Get log streams from run-specific log group
                all_vm_log_events = cw_manager.get_logs_with_filter()

                if all_vm_log_events:
                    break

                if attempt < max_retries - 1:
                    logger.info(
                        "Waiting for VM logs... (attempt %d/%d)",
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(retry_delay)

            if not all_vm_log_events:
                logger.warning("No VM logs found in CloudWatch")
            else:
                # Group logs by instance ID
                instance_logs = {}
                for event in all_vm_log_events:
                    stream_name = event.get("logStreamName", "")
                    # SSM log stream format: {command_id}/{instance_id}/{stdout|stderr}
                    parts = stream_name.split("/")
                    instance_id = parts[1] if len(parts) >= 3 else parts[0] if parts else "unknown"

                    if instance_id not in instance_logs:
                        instance_logs[instance_id] = {"stdout": [], "stderr": []}

                    if "/stdout" in stream_name:
                        instance_logs[instance_id]["stdout"].append(event)
                    elif "/stderr" in stream_name:
                        instance_logs[instance_id]["stderr"].append(event)

                # Write separate log file for each instance
                for instance_id, logs in instance_logs.items():
                    log_file = vms_dir / f"{instance_id}.log"
                    with open(log_file, "w", encoding="utf-8") as f:
                        f.write("=" * 60 + "\n")
                        f.write(f"VM Instance: {instance_id}\n")
                        f.write("=" * 60 + "\n\n")

                        if logs["stdout"]:
                            f.write("STDOUT\n")
                            f.write("=" * 60 + "\n")
                            for event in logs["stdout"]:
                                message = event.get("message", "")
                                timestamp_ms = event.get("timestamp", 0)
                                timestamp_str = time.strftime(
                                    "%Y-%m-%d %H:%M:%S",
                                    time.localtime(timestamp_ms / 1000),
                                )
                                f.write(f"[{timestamp_str}] {message}\n")

                        if logs["stderr"]:
                            f.write("\n" + "=" * 60 + "\n")
                            f.write("STDERR\n")
                            f.write("=" * 60 + "\n")
                            for event in logs["stderr"]:
                                message = event.get("message", "")
                                timestamp_ms = event.get("timestamp", 0)
                                timestamp_str = time.strftime(
                                    "%Y-%m-%d %H:%M:%S",
                                    time.localtime(timestamp_ms / 1000),
                                )
                                f.write(f"[{timestamp_str}] {message}\n")

                logger.info(
                    "✓ Created log files for %d VMs in %s",
                    len(instance_logs),
                    vms_dir,
                )

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Could not retrieve VM logs: %s", e)

        logger.info("-" * 60)

        # Benchmark regression analysis
        try:
            from kernel_ci_cloud_labs.core.benchmark_analyzer import (
                BenchmarkAnalyzer,
                log_benchmark_summary,
            )

            test_names = list({t for vm in provider.config["test_config"]["vms"] for t in vm.get("test", [])})
            s3_client = provider.auth.get_client("s3")
            analyzer = BenchmarkAnalyzer(s3_client, storage.bucket, run_prefix)
            benchmark_summary = analyzer.analyze(test_names)
            log_benchmark_summary(benchmark_summary)

            # NOTIFICATION HOOK: Use benchmark_summary here to trigger
            # external notifications (SNS, KCIDB, etc.) when regressions
            # are detected. See BenchmarkAnalyzer for structured data.
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Benchmark analysis skipped: %s", e)

        # Save results
        logger.debug("Saving pipeline results")
        results = {"status": "success", "task_arn": task_arn}
        storage.save_results(results)
        logger.debug("Results saved successfully")

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Pipeline failed: %s", e)
        logger.debug("Pipeline exception type: %s", type(e).__name__)
        raise
    finally:
        # Always cleanup - stop task and terminate VMs
        logger.info("\nCleaning up...")
        logger.debug("Stopping task: %s", task_arn if "task_arn" in locals() else "None")
        try:
            if "task_arn" in locals() and task_arn:
                provider.terminate_container(task_arn)
            else:
                logger.warning("No task to stop")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Cleanup failed: %s", e)

        # Terminate VMs for this run
        try:
            run_prefix = provider.config.get("run_prefix") if hasattr(provider, "config") else None
            if run_prefix:
                ec2 = provider.auth.get_client("ec2")
                response = ec2.describe_instances(
                    Filters=[
                        {"Name": "tag:run_prefix", "Values": [run_prefix]},
                        {
                            "Name": "instance-state-name",
                            "Values": ["pending", "running"],
                        },
                    ]
                )
                instance_ids = []
                for reservation in response.get("Reservations", []):
                    for instance in reservation.get("Instances", []):
                        instance_ids.append(instance["InstanceId"])

                if instance_ids:
                    logger.info("Terminating %d VM(s): %s", len(instance_ids), instance_ids)
                    ec2.terminate_instances(InstanceIds=instance_ids)
                    logger.info("✓ Terminated %d VM(s)", len(instance_ids))
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to terminate VMs: %s", e)

    # Create summary file
    s3_context = None
    if "storage" in locals() and "run_prefix" in locals():
        s3_client = None
        try:
            s3_client = provider.auth.get_client("s3") if "provider" in locals() else None
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        s3_context = {
            "bucket": getattr(storage, "bucket", ""),
            "run_prefix": run_prefix,
            "region": provider.config.get("region", "") if "provider" in locals() else "",
            "s3_client": s3_client,
        }
    return create_summary(
        run_dir,
        start_time,
        task_arn if "task_arn" in locals() else None,
        expected_vm_count if "expected_vm_count" in locals() else None,
        s3_context=s3_context,
    )
