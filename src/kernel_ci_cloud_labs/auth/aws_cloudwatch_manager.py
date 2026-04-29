"""AWS CloudWatch Logs management"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import time
from typing import Any, Dict, List

from kernel_ci_cloud_labs.core.base_resource_manager import BaseResourceManager
from kernel_ci_cloud_labs.core.logging_config import get_logger

logger = get_logger(__name__)


class AWSCloudWatchManager(BaseResourceManager):
    """Manages CloudWatch log groups and log streams"""

    def __init__(self, client, config, run_prefix=None, start_time_ms=None, ec2_log_group=None):
        """Initialize CloudWatch manager with optional run context"""
        super().__init__(client, config)
        self.run_prefix = run_prefix
        self.start_time_ms = start_time_ms or int(time.time() * 1000)
        self.ec2_log_group = ec2_log_group or "/ec2/kernel-ci-vms"

    def check_exists(self, resource_name: str) -> bool:
        """Check if log group exists"""
        try:
            logger.debug("Checking if log group exists: %s", resource_name)
            response = self.client.describe_log_groups(logGroupNamePrefix=resource_name)
            exists = len(response.get("logGroups", [])) > 0
            logger.debug("Log group %s exists: %s", resource_name, exists)
            return exists
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Broad exception is intentional - any error means log group doesn't exist
            logger.warning("Error checking log group %s: %s", resource_name, e)
            return False

    def create(self, resource_name: str, resource_config: Dict[str, Any]) -> str:
        """Create log group with retention"""
        logger.debug("Creating log group: %s", resource_name)
        self.client.create_log_group(logGroupName=resource_name)

        # Set retention if specified
        retention_days = resource_config.get("retention_days", 7)
        logger.debug("Setting retention policy: %d days", retention_days)
        self.client.put_retention_policy(logGroupName=resource_name, retentionInDays=retention_days)

        return resource_name

    def get_identifier(self, resource_name: str) -> str:
        """Get log group name (identifier)"""
        return resource_name

    def get_all_logs(self, log_group: str, log_stream_prefix: str = None) -> List[Dict]:
        """
        Retrieve ALL logs from CloudWatch using paginator (handles large log volumes).

        Args:
            log_group: CloudWatch log group name
            log_stream_prefix: Optional log stream prefix to filter

        Returns:
            List of all log events
        """
        try:
            paginator = self.client.get_paginator("filter_log_events")

            kwargs = {"logGroupName": log_group}
            if log_stream_prefix:
                kwargs["logStreamNamePrefix"] = log_stream_prefix

            page_iterator = paginator.paginate(**kwargs)

            all_events = []
            for page in page_iterator:
                all_events.extend(page.get("events", []))

            logger.info("Retrieved %d log events from %s", len(all_events), log_group)
            return all_events

        except self.client.exceptions.ResourceNotFoundException:
            logger.warning("Log group not found: %s", log_group)
            return []
        except Exception as e:
            logger.error("Error retrieving logs: %s", e)
            return []

    def get_logs_with_filter(
        self, log_group_suffix: str = None, start_time: int = None, log_stream_prefix: str = None
    ) -> List[Dict]:
        """
        Retrieve logs with time filter to avoid fetching entire history.

        Args:
            log_group_suffix: Optional suffix to append to run-specific log group
            start_time: Start time in milliseconds since epoch (uses self.start_time_ms if not provided)
            log_stream_prefix: Optional log stream prefix to filter

        Returns:
            List of log events
        """
        try:
            # Build log group name from run_prefix and optional suffix
            if log_group_suffix:
                log_group = f"{self.ec2_log_group}/{self.run_prefix}/{log_group_suffix}"
            elif self.run_prefix:
                log_group = f"{self.ec2_log_group}/{self.run_prefix}"
            else:
                log_group = self.ec2_log_group

            kwargs = {"logGroupName": log_group}
            if log_stream_prefix:
                kwargs["logStreamNamePrefix"] = log_stream_prefix
            if start_time:
                kwargs["startTime"] = start_time
            elif self.start_time_ms:
                kwargs["startTime"] = self.start_time_ms

            paginator = self.client.get_paginator("filter_log_events")
            page_iterator = paginator.paginate(**kwargs)

            all_events = []
            for page in page_iterator:
                all_events.extend(page.get("events", []))

            logger.info("Retrieved %d log events from %s", len(all_events), log_group)
            return all_events

        except self.client.exceptions.ResourceNotFoundException:
            logger.warning("Log group not found: %s", log_group)
            return []
        except Exception as e:
            logger.error("Error retrieving logs: %s", e)
            return []

    def get_logs(self, log_group: str, log_stream: str, limit: int = None) -> List[Dict]:
        """Retrieve logs from CloudWatch"""
        try:
            kwargs = {
                "logGroupName": log_group,
                "logStreamName": log_stream,
                "startFromHead": True,
            }
            if limit:
                kwargs["limit"] = limit

            response = self.client.get_log_events(**kwargs)
            return response["events"]
        except self.client.exceptions.ResourceNotFoundException:
            return []

    def tail_logs(self, log_group: str, log_stream: str, duration: int = 60):
        """Stream logs in real-time for specified duration"""
        logger.info("Tailing logs from %s/%s", log_group, log_stream)
        logger.debug("Tail duration: %d seconds", duration)
        start_time = time.time()
        next_token = None
        stream_found = False
        start_from_head = True
        event_count = 0

        while time.time() - start_time < duration:
            try:
                kwargs = {
                    "logGroupName": log_group,
                    "logStreamName": log_stream,
                    "startFromHead": start_from_head,
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                    logger.debug("Using nextToken for pagination")

                response = self.client.get_log_events(**kwargs)

                if not stream_found:
                    stream_found = True
                    logger.info("✓ Log stream found, streaming logs...")
                    logger.debug("Log stream has events: %d", len(response.get("events", [])))

                for event in response["events"]:
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["timestamp"] / 1000))
                    logger.info("[%s] %s", timestamp, event["message"])
                    event_count += 1

                next_token = response.get("nextForwardToken")
                if not response["events"]:
                    logger.debug("No new events, waiting...")
                time.sleep(1)

            except self.client.exceptions.ResourceNotFoundException as e:
                if not stream_found:
                    elapsed = int(time.time() - start_time)
                    logger.info("Waiting for log stream... (%ds)", elapsed)
                    if elapsed > 30:
                        logger.warning("Log stream not found after %ds - may not exist", elapsed)
                else:
                    logger.warning("Log stream disappeared: %s", e)
                    break
                time.sleep(5)
            except KeyboardInterrupt:
                logger.info("\nStopped tailing logs")
                break
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("Error tailing logs: %s", e)
                logger.debug("Exception type: %s", type(e).__name__)
                break

        logger.debug("Finished tailing logs. Total events: %d", event_count)

    def tail_vm_logs(self, instance_ids: list, duration: int = 60):
        """Tail logs from multiple EC2 VMs sequentially"""
        log_group = self.ec2_log_group
        for instance_id in instance_ids:
            logger.info("=== Tailing VM logs: %s ===", instance_id)
            self.tail_logs(log_group, instance_id, duration)
            logger.info("=== Finished VM: %s ===\n", instance_id)

    def list_log_streams(self, log_group: str, limit: int = 10) -> List[str]:
        """List recent log streams in a log group"""
        try:
            response = self.client.describe_log_streams(
                logGroupName=log_group, orderBy="LastEventTime", descending=True, limit=limit
            )
            return [stream["logStreamName"] for stream in response["logStreams"]]
        except self.client.exceptions.ResourceNotFoundException:
            return []
