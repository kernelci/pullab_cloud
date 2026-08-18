"""AWS Fargate provider implementation."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


# providers/aws_provider.py
import json
import os
import re
import time

from kernel_ci_cloud_labs.core.base_provider import BaseProvider
from kernel_ci_cloud_labs.core.logging_config import get_logger
from kernel_ci_cloud_labs.core.registry import register_provider

logger = get_logger(__name__)


# Kernel-side crash / stall patterns matched against each new CloudWatch
# event from the guest VM console. First hit triggers an early abort of
# wait_for_task_completion so the kernelci-api node is finished
# incomplete/Infrastructure rather than the loop spinning for an hour on a
# wedged guest.
_KERNEL_CRASH_PATTERNS = tuple(
    re.compile(p) for p in (
        # Fatal traps -- guest will normally exit, but if qemu wedges it won't.
        r"Kernel panic - not syncing",
        r"\bOops\s*:",
        r"\bBUG\s*:",
        r"general protection fault",
        r"unable to handle kernel paging request",
        r"double fault",
        r"Internal error\s*:",          # arm / arm64 die() banner
        # Stalls / hangs -- kernel is still scheduling but wedged.
        r"watchdog: BUG: soft lockup",
        r"soft lockup - CPU#",
        r"rcu_(?:sched|preempt|bh) detected stalls",
        r"INFO: task .* blocked for more than",
    )
)


def _scan_for_kernel_crash(events):
    """Return the first event whose message matches a crash pattern, else None."""
    for event in events:
        message = event.get("message") or ""
        for pat in _KERNEL_CRASH_PATTERNS:
            if pat.search(message):
                return event
    return None


@register_provider("aws")
class AWSProvider(BaseProvider):
    """AWS provider for running containers on Fargate."""

    def __init__(self, auth, config=None, storage=None):
        super().__init__(auth)
        self.config = config or {}
        self.storage = storage
        self.task_arn = None
        self.ecs = None

        # Get cluster and task definition from config
        ecs_config = self.config.get("ecs", {})
        self.cluster = ecs_config.get("cluster", "default")
        self.task_definition = ecs_config.get("task_definition", {}).get("family", "default-task")
        logger.debug(
            "Initialized AWSProvider - cluster: %s, task_def: %s",
            self.cluster,
            self.task_definition,
        )

    def authenticate(self):
        logger.info("Using provided auth object to authenticate.")
        logger.debug("Calling auth.authenticate()")
        self.auth.authenticate()
        logger.debug("Getting ECS client")
        self.ecs = self.auth.get_client("ecs")
        if not self.ecs:
            logger.error("Failed to get ECS client from auth")
            raise RuntimeError("ECS client unavailable")
        logger.debug("ECS client initialized successfully")

    def spawn_container(self):
        logger.info("Spawning Fargate container on cluster '%s'...", self.cluster)
        logger.debug("Task definition: %s", self.task_definition)

        if not self.ecs:
            logger.warning("ECS client not initialized, authenticating now")
            self.authenticate()

        # Get network configuration
        logger.debug("Getting network configuration")
        network_config = self.auth.get_network_config()
        logger.debug("Network config: %s", network_config)

        # Build environment overrides with runtime config
        env_vars = []

        # Pass run prefix
        if self.config.get("run_prefix"):
            env_vars.append({"name": "RUN_PREFIX", "value": self.config["run_prefix"]})
            logger.debug("Passing RUN_PREFIX to container: %s", self.config["run_prefix"])

        # Pass actual S3 bucket name from storage object
        if self.storage:
            env_vars.append({"name": "S3_BUCKET", "value": self.storage.bucket})
            logger.debug("Passing S3_BUCKET to container: %s", self.storage.bucket)

        # Pass region
        if self.config.get("region"):
            env_vars.append({"name": "AWS_REGION", "value": self.config["region"]})
            logger.debug("Passing AWS_REGION to container: %s", self.config["region"])

        # Pass EC2 VM log group name from cloudwatch config
        cw_config = self.config.get("cloudwatch", {}).get("log_groups", {})
        ec2_log_group = next((k for k in cw_config if "/ec2/" in k), None)
        if ec2_log_group:
            env_vars.append({"name": "EC2_LOG_GROUP", "value": ec2_log_group})
            logger.debug("Passing EC2_LOG_GROUP to container: %s", ec2_log_group)

        # Forward KCI_DEBUG to the container so launch_vm.log_debug() lines
        # show up in the ECS task log group. Set on the host running the
        # poller: KCI_DEBUG=1 ./prod-amd64.sh
        kci_debug = os.environ.get("KCI_DEBUG")
        if kci_debug:
            env_vars.append({"name": "KCI_DEBUG", "value": kci_debug})
            logger.debug("Passing KCI_DEBUG to container: %s", kci_debug)

        # Always pass test config via S3 in results bucket
        if self.config.get("test_config"):
            test_config_json = json.dumps(self.config["test_config"])
            run_prefix = self.config.get("run_prefix")
            if not run_prefix:
                raise ValueError("run_prefix is required in configuration")
            config_filename = "test_config.json"
            config_key = f"{run_prefix}/{config_filename}"
            self.storage.upload_string(test_config_json, config_key)
            # Pass only the filename, container will rebuild full path
            env_vars.append({"name": "TEST_CONFIG_FILENAME", "value": config_filename})
            logger.debug("Passing TEST_CONFIG_FILENAME: %s", config_filename)

        overrides = {}
        if env_vars:
            overrides = {
                "containerOverrides": [
                    {
                        "name": self.config.get("ecs", {})
                        .get("task_definition", {})
                        .get("container_name", "default-container"),
                        "environment": env_vars,
                    }
                ]
            }

        # Retry logic for transient errors (IAM propagation, etc.)
        max_retries = 5
        for attempt in range(max_retries):
            try:
                logger.debug("Calling ECS run_task API (attempt %d/%d)", attempt + 1, max_retries)
                response = self.ecs.run_task(
                    cluster=self.cluster,
                    launchType="FARGATE",
                    taskDefinition=self.task_definition,
                    count=1,
                    enableExecuteCommand=True,
                    networkConfiguration=network_config,
                    overrides=overrides,
                )
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2**attempt
                    logger.warning(
                        "Error calling run_task (%s), retrying in %ds...",
                        type(e).__name__,
                        wait_time,
                    )
                    time.sleep(wait_time)
                else:
                    logger.error("Failed to call run_task: %s", e)
                    raise

        if response["failures"]:
            logger.error("Task launch failures: %s", response["failures"])
            raise RuntimeError(f"Failed to launch task: {response['failures']}")

        if not response.get("tasks"):
            logger.error("No tasks returned in response")
            raise RuntimeError("No tasks created")

        self.task_arn = response["tasks"][0]["taskArn"]
        task_id = self.task_arn.split("/")[-1]
        logger.info("✓ Launched task: %s", task_id)
        logger.debug("Full task ARN: %s", self.task_arn)
        return self.task_arn

    def get_task_status(self):
        """Get current status of the task"""
        if not self.task_arn:
            logger.warning("get_task_status called but task_arn is None")
            return None

        logger.debug("Describing task: %s", self.task_arn)
        try:
            response = self.ecs.describe_tasks(cluster=self.cluster, tasks=[self.task_arn])
        except self.ecs.exceptions.ClientError as e:
            if "ExpiredTokenException" in str(e):
                logger.warning("Credentials expired, refreshing ECS client...")
                self.ecs = self.auth.get_client("ecs")
                try:
                    response = self.ecs.describe_tasks(cluster=self.cluster, tasks=[self.task_arn])
                except Exception as retry_e:
                    logger.error("Failed to describe task after refresh: %s", retry_e)
                    return None
            else:
                logger.error("Failed to describe task: %s", e)
                return None
        except Exception as e:
            logger.error("Failed to describe task: %s", e)
            return None

        if not response["tasks"]:
            logger.warning("Task not found in describe_tasks response")
            return None

        task = response["tasks"][0]
        status = {
            "status": task["lastStatus"],
            "desired_status": task["desiredStatus"],
            "containers": [
                {
                    "name": c["name"],
                    "status": c["lastStatus"],
                    "exit_code": c.get("exitCode"),
                }
                for c in task["containers"]
            ],
        }
        logger.debug("Task status: %s, desired: %s", status["status"], status["desired_status"])
        return status

    def wait_for_running(self, timeout=300):
        """Wait for task to reach RUNNING state using waiter"""
        logger.info("Waiting for task to start...")
        try:
            waiter = self.ecs.get_waiter("tasks_running")
            waiter.wait(
                cluster=self.cluster,
                tasks=[self.task_arn],
                WaiterConfig={"Delay": 5, "MaxAttempts": timeout // 5},
            )
            logger.info("✓ Task is RUNNING")
            return True
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("✗ Task failed to reach RUNNING state: %s", e)
            return False

    def _build_vm_log_manager(self, start_time_ms):
        """Build an AWSCloudWatchManager scoped to this run's VM console group.

        Returns None when no EC2 log group / run prefix is configured (e.g.
        unit tests or a deployment without the cloudwatch section); in that
        case wait_for_task_completion falls back to pure status polling
        without crash detection.
        """
        cw_log_groups = self.config.get("cloudwatch", {}).get("log_groups", {}) or {}
        ec2_log_group = next((k for k in cw_log_groups if "/ec2/" in k), None)
        run_prefix = self.config.get("run_prefix")
        if not ec2_log_group or not run_prefix:
            logger.debug(
                "VM crash detection disabled: ec2_log_group=%s run_prefix=%s",
                ec2_log_group, run_prefix,
            )
            return None
        try:
            logs_client = self.auth.get_client("logs")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Could not obtain CloudWatch logs client (%s) — "
                "VM crash detection disabled", e,
            )
            return None
        if not logs_client:
            logger.warning("No CloudWatch logs client — VM crash detection disabled")
            return None
        from kernel_ci_cloud_labs.auth.aws_cloudwatch_manager import (  # noqa: PLC0415
            AWSCloudWatchManager,
        )
        return AWSCloudWatchManager(
            logs_client,
            {},
            run_prefix=run_prefix,
            start_time_ms=start_time_ms,
            ec2_log_group=ec2_log_group,
        )

    def wait_for_task_completion(self):
        """Wait for the ECS task to reach STOPPED, with crash / stall detection.

        Polls task status until STOPPED. While waiting, tails the per-run VM
        console log group ({EC2_LOG_GROUP}/{run_prefix} -- written by SSM Run
        Command, see launch_vm.py) and aborts early on:

        * Kernel-side crash patterns in the guest console (panic, Oops, BUG:,
          soft lockup, RCU stall, hung task, GP fault, kernel paging fault).
          The ECS task is stopped and a RuntimeError is raised so the caller
          finishes the kernelci-api node incomplete/Infrastructure with the
          matched line surfaced in error_msg.
        * No new VM console output for PULLAB_TASK_HANG_THRESHOLD_SEC seconds
          (default 1200) -- silent stall, same treatment as a crash. The
          default accommodates CPU-heavy benchmarks (e.g. UnixBench) whose
          console goes quiet for many minutes during a run; lower it via the
          env var for faster hang detection on lighter workloads.
        * Overall PULLAB_TASK_WAIT_TIMEOUT_SEC seconds elapsed (default 3600)
          -- final safety net for whatever isn't covered above.

        Crash detection requires both cloudwatch.log_groups (with an /ec2/
        group) and run_prefix in the run config; otherwise the loop falls
        back to plain status polling.

        Returns:
            dict: Final task status including exit codes.
        """
        if not self.task_arn:
            logger.error("Cannot wait for completion - no task ARN available")
            raise RuntimeError("No task to wait for")

        logger.info("Waiting for task to complete...")
        logger.debug("Task ARN: %s", self.task_arn)

        poll_interval = float(os.getenv("PULLAB_TASK_POLL_INTERVAL_SEC") or 30)
        log_interval = float(os.getenv("PULLAB_TASK_PROGRESS_LOG_SEC") or 120)
        hang_threshold = float(os.getenv("PULLAB_TASK_HANG_THRESHOLD_SEC") or 1200)
        overall_timeout = float(os.getenv("PULLAB_TASK_WAIT_TIMEOUT_SEC") or 3600)

        start = time.time()
        start_ms = int(start * 1000)
        last_log_time = start
        last_event_seen_at = start
        # filter_log_events startTime is inclusive; -1 so the first poll
        # picks up events with timestamp == start_ms.
        last_event_ms = start_ms - 1

        cw_manager = self._build_vm_log_manager(start_ms)
        if cw_manager is None:
            logger.info("Wait loop: VM crash detection disabled (no log group / run_prefix)")

        while True:
            elapsed = time.time() - start
            if elapsed > overall_timeout:
                logger.error(
                    "Overall wait timeout (%ds) exceeded — stopping task",
                    int(overall_timeout),
                )
                self.terminate_container()
                raise RuntimeError(
                    f"task wait timeout exceeded after {int(elapsed)}s"
                )

            status = self.get_task_status()
            if not status:
                logger.warning("Could not retrieve task status, retrying...")
                time.sleep(poll_interval)
                continue

            task_status = status.get("status", "UNKNOWN")

            if task_status == "STOPPED":
                logger.info(
                    "✓ Task completed (elapsed: %dm %ds)",
                    int(elapsed) // 60, int(elapsed) % 60,
                )
                break

            # Tail the VM console group for crash patterns / progress.
            if cw_manager is not None:
                new_events = cw_manager.get_logs_with_filter(
                    start_time=last_event_ms + 1
                ) or []
                if new_events:
                    last_event_seen_at = time.time()
                    for ev in new_events:
                        ts = ev.get("timestamp", 0)
                        if ts > last_event_ms:
                            last_event_ms = ts
                    hit = _scan_for_kernel_crash(new_events)
                    if hit:
                        msg = (hit.get("message") or "").strip()[:300]
                        logger.error(
                            "Kernel crash/stall in VM console (stream=%s): %s",
                            hit.get("logStreamName", "?"), msg,
                        )
                        self.terminate_container()
                        raise RuntimeError(f"kernel crash detected in VM: {msg}")
                elif (time.time() - last_event_seen_at) > hang_threshold:
                    logger.error(
                        "No VM console output for %ds (hang threshold %ds) — stopping task",
                        int(time.time() - last_event_seen_at),
                        int(hang_threshold),
                    )
                    self.terminate_container()
                    raise RuntimeError(
                        f"no VM console output for {int(hang_threshold)}s"
                    )

            now = time.time()
            if now - last_log_time >= log_interval:
                last_log_time = now
                logger.info(
                    "Task still running... (status: %s, elapsed: %dm %ds)",
                    task_status, int(elapsed) // 60, int(elapsed) % 60,
                )

            time.sleep(poll_interval)

        # Get final status to check exit codes
        final_status = self.get_task_status()

        if final_status:
            logger.debug("Final task status: %s", final_status)
            # Check if any container failed
            for container in final_status.get("containers", []):
                exit_code = container.get("exit_code")
                container_name = container["name"]
                if exit_code is not None:
                    if exit_code == 0:
                        logger.info("Container %s exited successfully (code 0)", container_name)
                    else:
                        logger.warning(
                            "Container %s exited with code %d",
                            container_name,
                            exit_code,
                        )

        return final_status

    def get_task_logs(self):
        """Get CloudWatch logs for the task (if configured)"""
        logger.info("TODO: Implement CloudWatch logs retrieval")
        # Requires log configuration in task definition

    def run_test(self):
        """Run test in container and return status."""
        logger.info("Running test in container...")
        logger.info("Task ARN: %s", self.task_arn)

        # Get current status
        status = self.get_task_status()
        if status:
            logger.info("Task status: %s", status["status"])

        return {"status": "success", "task_arn": self.task_arn}

    def terminate_container(self, task_arn=None):
        """Terminate a specific task or the current task"""
        arn_to_stop = task_arn or self.task_arn

        if arn_to_stop:
            task_id = arn_to_stop.split("/")[-1]
            logger.debug("Stopping task: %s", task_id)
            try:
                self.ecs.stop_task(cluster=self.cluster, task=arn_to_stop)
                logger.info("✓ Stopped task: %s", task_id)
            except Exception as e:
                logger.error("Failed to stop task %s: %s", task_id, e)
                raise
        else:
            logger.warning("No container to terminate")

    def stop_all_tasks(self):
        """Stop all running tasks in the cluster"""
        logger.info("Stopping all tasks in cluster '%s'...", self.cluster)
        logger.debug("Listing running tasks")

        if not self.ecs:
            logger.warning("ECS client not initialized, authenticating")
            self.authenticate()

        # List all running tasks
        try:
            response = self.ecs.list_tasks(cluster=self.cluster, desiredStatus="RUNNING")
        except Exception as e:
            logger.error("Failed to list tasks: %s", e)
            raise

        task_arns = response.get("taskArns", [])
        logger.debug("Found %d running task(s)", len(task_arns))

        if not task_arns:
            logger.info("No running tasks found")
            return 0

        # Stop each task
        for task_arn in task_arns:
            self.terminate_container(task_arn)

        logger.info("✓ Stopped %d task(s)", len(task_arns))
        return len(task_arns)
