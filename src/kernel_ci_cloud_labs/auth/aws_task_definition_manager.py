"""AWS ECS Task Definition management"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from typing import Any, Dict

from kernel_ci_cloud_labs.core.base_resource_manager import BaseResourceManager


class AWSTaskDefinitionManager(BaseResourceManager):
    """Manages ECS task definitions"""

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
        # The awslogs stream name is "<prefix>/<container-name>/<task-id>".
        # A per-run prefix (e.g. the run/test id) makes concurrent runs — which
        # all log into the same /ecs/<family> group — easy to tell apart in
        # CloudWatch, instead of every task sharing the generic "ecs" prefix.
        # The <task-id> suffix already guarantees stream uniqueness; the prefix
        # is purely for human/tool separability. Defaults to "ecs".
        stream_prefix = resource_config.get("log_stream_prefix", "ecs")
        container_def["logConfiguration"] = {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": log_group,
                "awslogs-region": resource_config.get("region", "us-west-2"),
                "awslogs-stream-prefix": stream_prefix,
                "awslogs-create-group": "true",
            },
        }

        # Register task definition
        response = self.client.register_task_definition(
            family=resource_name,
            networkMode="awsvpc",
            requiresCompatibilities=["FARGATE"],
            cpu=resource_config.get("cpu", "256"),
            memory=resource_config.get("memory", "512"),
            executionRoleArn=resource_config.get("execution_role_arn"),
            taskRoleArn=resource_config.get("task_role_arn"),
            containerDefinitions=[container_def],
        )

        return response["taskDefinition"]["taskDefinitionArn"]

    def get_identifier(self, resource_name: str) -> str:
        """Get task definition ARN"""
        response = self.client.describe_task_definition(taskDefinition=resource_name)
        return response["taskDefinition"]["taskDefinitionArn"]
