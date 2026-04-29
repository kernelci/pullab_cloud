"""AWS Fargate provider implementation."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


# providers/aws_provider.py
from kernel_ci_cloud_labs.core.base_provider import BaseProvider
from kernel_ci_cloud_labs.core.logging_config import get_logger
from kernel_ci_cloud_labs.core.registry import register_provider

logger = get_logger(__name__)


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
        import json
        import time

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

    def wait_for_task_completion(self):
        """
        Wait for the ECS task to complete (reach STOPPED state).

        This uses boto3's tasks_stopped waiter which polls the task status
        until it transitions to STOPPED, meaning:
        - All containers have finished executing
        - All VMs have been spawned, run tests, and uploaded results to S3
        - The task has gracefully shut down

        Returns:
            dict: Final task status including exit codes
        """
        if not self.task_arn:
            logger.error("Cannot wait for completion - no task ARN available")
            raise RuntimeError("No task to wait for")

        logger.info("Waiting for task to complete...")
        logger.debug("Task ARN: %s", self.task_arn)

        # Poll task status with periodic INFO logging so the user sees progress
        import time as _time

        poll_interval = 30  # seconds between status checks
        log_interval = 120  # seconds between INFO progress messages
        start = _time.time()
        last_log_time = start

        while True:
            status = self.get_task_status()
            if not status:
                logger.warning("Could not retrieve task status, retrying...")
                _time.sleep(poll_interval)
                continue

            task_status = status.get("status", "UNKNOWN")
            elapsed = int(_time.time() - start)

            if task_status == "STOPPED":
                logger.info("✓ Task completed (elapsed: %dm %ds)", elapsed // 60, elapsed % 60)
                break

            now = _time.time()
            if now - last_log_time >= log_interval:
                last_log_time = now
                logger.info(
                    "Task still running... (status: %s, elapsed: %dm %ds)",
                    task_status,
                    elapsed // 60,
                    elapsed % 60,
                )

            _time.sleep(poll_interval)

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
