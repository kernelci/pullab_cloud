"""Unit tests for Network manager"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from unittest.mock import Mock

from kernel_ci_cloud_labs.auth.aws_network_manager import AWSNetworkManager


class TestAWSNetworkManager:
    """Test AWS network manager"""

    def test_get_default_subnets(self):
        """Test retrieving default subnets"""
        mock_client = Mock()
        mock_client.describe_subnets.return_value = {"Subnets": [{"SubnetId": "subnet-1"}, {"SubnetId": "subnet-2"}]}

        manager = AWSNetworkManager(mock_client, {})
        subnets = manager.get_default_subnets()

        assert len(subnets) == 2
        assert subnets[0] == "subnet-1"

    def test_get_default_security_group(self):
        """Test retrieving default security group"""
        mock_client = Mock()
        mock_client.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-123", "GroupName": "default"}]
        }

        manager = AWSNetworkManager(mock_client, {})
        sg_id = manager.get_default_security_group()

        assert sg_id == "sg-123"

    def test_get_network_config(self):
        """Test getting complete network configuration"""
        mock_client = Mock()
        mock_client.describe_subnets.side_effect = [
            # First call: get_default_subnets (filter by default-for-az)
            {
                "Subnets": [
                    {"SubnetId": "subnet-1", "VpcId": "vpc-abc"},
                    {"SubnetId": "subnet-2", "VpcId": "vpc-abc"},
                    {"SubnetId": "subnet-3", "VpcId": "vpc-abc"},
                ]
            },
            # Second call: describe_subnets by ID to get VPC
            {"Subnets": [{"SubnetId": "subnet-1", "VpcId": "vpc-abc"}]},
        ]
        mock_client.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-123", "GroupName": "default"}]
        }

        manager = AWSNetworkManager(mock_client, {})
        config = manager.get_network_config()

        assert "awsvpcConfiguration" in config
        assert len(config["awsvpcConfiguration"]["subnets"]) == 2  # Uses first 2
        assert config["awsvpcConfiguration"]["securityGroups"] == ["sg-123"]
        assert config["awsvpcConfiguration"]["assignPublicIp"] == "ENABLED"
        # Verify security group was scoped to the subnet's VPC
        mock_client.describe_security_groups.assert_called_once_with(
            Filters=[
                {"Name": "group-name", "Values": ["default"]},
                {"Name": "vpc-id", "Values": ["vpc-abc"]},
            ]
        )
