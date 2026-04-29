"""AWS ECR repository management"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from typing import Any, Dict

from kernel_ci_cloud_labs.core.base_resource_manager import BaseResourceManager


class AWSECRManager(BaseResourceManager):
    """Manages ECR repositories for container images"""

    def check_exists(self, resource_name: str) -> bool:
        """Check if ECR repository exists"""
        try:
            self.client.describe_repositories(repositoryNames=[resource_name])
            return True
        except self.client.exceptions.RepositoryNotFoundException:
            return False

    def create(self, resource_name: str, resource_config: Dict[str, Any]) -> str:
        """Create ECR repository"""
        response = self.client.create_repository(
            repositoryName=resource_name,
            imageScanningConfiguration={"scanOnPush": resource_config.get("scan_on_push", False)},
        )
        return response["repository"]["repositoryUri"]

    def get_identifier(self, resource_name: str) -> str:
        """Get repository URI"""
        response = self.client.describe_repositories(repositoryNames=[resource_name])
        return response["repositories"][0]["repositoryUri"]
