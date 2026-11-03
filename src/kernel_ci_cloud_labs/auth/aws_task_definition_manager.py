"""AWS ECS Task Definition management"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from typing import Any, Dict
from botocore.exceptions import ClientError

from kernel_ci_cloud_labs.core.base_resource_manager import BaseResourceManager


def _contains(actual, wanted):
    """True when *actual* holds everything in *wanted*.

    describe_task_definition normalises what was registered: it adds defaults
    such as ``logConfiguration.secretOptions: []`` and empty container lists.
    Only the fields this manager sets are compared, recursively, so those
    additions do not register a new revision on every run.
    """
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(_contains(actual.get(key), value) for key, value in wanted.items())
    if isinstance(wanted, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(wanted)
            and all(_contains(item, value) for item, value in zip(actual, wanted))
        )
    return actual == wanted


class AWSTaskDefinitionManager(BaseResourceManager):
    """Manages ECS task definitions"""

    def ensure_exists(self, resource_name, resource_config=None, force_recreate=False):
        """Register a revision when the executable contract changes.

        Finding an ACTIVE family is insufficient: it may still reference the
        previous runner image. Existing revisions/tasks are never deregistered.
        """
        config = resource_config or self.config.get(resource_name, {})
        try:
            current = self.client.describe_task_definition(taskDefinition=resource_name)["taskDefinition"]
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ClientException":
                raise
            return self.create(resource_name, config), True
        if current.get("status") == "ACTIVE" and not force_recreate:
            if not config:
                return current["taskDefinitionArn"], False
            desired = self._definition(resource_name, config)
            fields = ("cpu", "memory", "networkMode", "executionRoleArn", "taskRoleArn")
            containers = current.get("containerDefinitions", [])
            expected = desired["containerDefinitions"]
            matches = all(current.get(key) == desired.get(key) for key in fields)
            matches = matches and len(containers) == len(expected)
            matches = matches and all(_contains(actual, wanted) for actual, wanted in zip(containers, expected))
            if matches:
                return current["taskDefinitionArn"], False
        return self.create(resource_name, config), True

    def check_exists(self, resource_name: str) -> bool:
        """Check if task definition exists"""
        try:
            response = self.client.describe_task_definition(taskDefinition=resource_name)
            return response["taskDefinition"].get("status") == "ACTIVE"
        except Exception:  # pylint: disable=broad-exception-caught
            # Broad exception is intentional - any error means task definition doesn't exist
            return False

    def create(self, resource_name: str, resource_config: Dict[str, Any]) -> str:
        """Create task definition with CloudWatch logs"""

        response = self.client.register_task_definition(**self._definition(resource_name, resource_config))
        return response["taskDefinition"]["taskDefinitionArn"]

    @staticmethod
    def _definition(resource_name, resource_config):

        # Build container definition
        container_def = {
            "name": resource_config.get("container_name", "app"),
            "image": resource_config.get("image", "alpine:latest"),
            "essential": True,
            "linuxParameters": {"initProcessEnabled": True},
        }

        # Add command if specified
        if resource_config.get("command"):
            container_def["command"] = resource_config["command"]

        # Add CloudWatch logs configuration
        log_group = f"/ecs/{resource_name}"
        container_def["logConfiguration"] = {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": log_group,
                "awslogs-region": resource_config.get("region", "us-west-2"),
                "awslogs-stream-prefix": "ecs",
                "awslogs-create-group": "true",
            },
        }

        # Register task definition
        return dict(
            family=resource_name,
            networkMode="awsvpc",
            requiresCompatibilities=["FARGATE"],
            cpu=resource_config.get("cpu", "256"),
            memory=resource_config.get("memory", "512"),
            executionRoleArn=resource_config.get("execution_role_arn"),
            taskRoleArn=resource_config.get("task_role_arn"),
            containerDefinitions=[container_def],
        )

    def get_identifier(self, resource_name: str) -> str:
        """Get task definition ARN"""
        response = self.client.describe_task_definition(taskDefinition=resource_name)
        return response["taskDefinition"]["taskDefinitionArn"]
