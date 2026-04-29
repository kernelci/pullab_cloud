"""AWS VPC Network configuration management"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from typing import Any, Dict, List

from kernel_ci_cloud_labs.core.base_resource_manager import BaseResourceManager


class AWSNetworkManager(BaseResourceManager):
    """Manages VPC network configuration for Fargate"""

    def check_exists(self, resource_name: str) -> bool:
        """Network config always exists (uses default VPC)"""
        return True

    def create(self, resource_name: str, resource_config: Dict[str, Any]) -> str:
        """Not applicable - uses existing default VPC"""
        return "default-vpc"

    def get_identifier(self, resource_name: str) -> str:
        """Not applicable - returns network config dict"""
        return "default-vpc"

    def get_default_subnets(self) -> List[str]:
        """Get default VPC subnets"""
        response = self.client.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])
        return [subnet["SubnetId"] for subnet in response["Subnets"]]

    def get_default_security_group(self, vpc_id: str = None) -> str:
        """Get default security group, optionally scoped to a specific VPC."""
        filters = [{"Name": "group-name", "Values": ["default"]}]
        if vpc_id:
            filters.append({"Name": "vpc-id", "Values": [vpc_id]})
        response = self.client.describe_security_groups(Filters=filters)
        return response["SecurityGroups"][0]["GroupId"]

    def get_network_config(self) -> Dict[str, Any]:
        """Get complete network configuration for Fargate"""
        subnets = self.get_default_subnets()

        # Get VPC ID from first subnet to ensure security group is from the same VPC
        subnet_info = self.client.describe_subnets(SubnetIds=[subnets[0]])
        vpc_id = subnet_info["Subnets"][0]["VpcId"]
        sg = self.get_default_security_group(vpc_id)

        return {
            "awsvpcConfiguration": {
                "subnets": subnets[:2],  # Use first 2 subnets
                "securityGroups": [sg],
                "assignPublicIp": "ENABLED",
            }
        }
