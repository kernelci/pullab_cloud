"""AWS ECS Cluster management"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from typing import Any, Dict

from kernel_ci_cloud_labs.core.base_resource_manager import BaseResourceManager


class AWSClusterManager(BaseResourceManager):
    """Manages ECS clusters"""

    def check_exists(self, resource_name: str) -> bool:
        """Check if ECS cluster exists"""
        try:
            response = self.client.describe_clusters(clusters=[resource_name])
            return len(response["clusters"]) > 0 and response["clusters"][0].get("status") == "ACTIVE"
        except Exception:  # pylint: disable=broad-exception-caught
            # Broad exception is intentional - any error means cluster doesn't exist
            return False

    def create(self, resource_name: str, resource_config: Dict[str, Any]) -> str:
        """Create ECS cluster"""
        response = self.client.create_cluster(clusterName=resource_name)
        return response["cluster"]["clusterArn"]

    def get_identifier(self, resource_name: str) -> str:
        """Get cluster ARN"""
        response = self.client.describe_clusters(clusters=[resource_name])
        return response["clusters"][0]["clusterArn"]
