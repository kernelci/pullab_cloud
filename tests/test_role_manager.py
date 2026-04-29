"""Unit tests for AWS Role Manager"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from unittest.mock import Mock

import pytest

from kernel_ci_cloud_labs.auth.aws_role_manager import AWSRoleManager


class TestAWSRoleManager:
    """Test AWS IAM role management"""

    @pytest.fixture
    def mock_iam_client(self):
        """Create mock IAM client"""
        mock = Mock()
        mock.exceptions.NoSuchEntityException = type("NoSuchEntityException", (Exception,), {})
        return mock

    @pytest.fixture
    def roles_config(self):
        """Sample roles configuration"""
        return {
            "ecsTaskExecutionRole": {
                "description": "ECS task execution role",
                "trust_policy": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "policies": ["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"],
            }
        }

    def test_check_exists_true(self, mock_iam_client, roles_config):
        """Test when role exists"""
        mock_iam_client.get_role.return_value = {"Role": {"Arn": "arn:aws:iam::123:role/test"}}

        manager = AWSRoleManager(mock_iam_client, roles_config)
        exists = manager.check_exists("ecsTaskExecutionRole")

        assert exists is True

    def test_check_exists_false(self, mock_iam_client, roles_config):
        """Test when role doesn't exist"""
        mock_iam_client.get_role.side_effect = mock_iam_client.exceptions.NoSuchEntityException()

        manager = AWSRoleManager(mock_iam_client, roles_config)
        exists = manager.check_exists("ecsTaskExecutionRole")

        assert exists is False

    def test_create_role(self, mock_iam_client, roles_config):
        """Test creating a role"""
        mock_iam_client.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123:role/ecsTaskExecutionRole"}}

        manager = AWSRoleManager(mock_iam_client, roles_config)
        arn = manager.create("ecsTaskExecutionRole", roles_config["ecsTaskExecutionRole"])

        assert arn == "arn:aws:iam::123:role/ecsTaskExecutionRole"
        mock_iam_client.create_role.assert_called_once()
        mock_iam_client.attach_role_policy.assert_called_once()

    def test_get_identifier(self, mock_iam_client, roles_config):
        """Test getting role ARN"""
        expected_arn = "arn:aws:iam::123456789:role/ecsTaskExecutionRole"
        mock_iam_client.get_role.return_value = {"Role": {"Arn": expected_arn}}

        manager = AWSRoleManager(mock_iam_client, roles_config)
        arn = manager.get_identifier("ecsTaskExecutionRole")

        assert arn == expected_arn

    def test_ensure_exists_creates_missing(self, mock_iam_client, roles_config):
        """Test ensure_exists creates missing role"""
        mock_iam_client.get_role.side_effect = [
            mock_iam_client.exceptions.NoSuchEntityException(),
            {"Role": {"Arn": "arn:aws:iam::123:role/test"}},
        ]
        mock_iam_client.create_role.return_value = {"Role": {"Arn": "arn:aws:iam::123:role/test"}}

        manager = AWSRoleManager(mock_iam_client, roles_config)
        arn, created = manager.ensure_exists("ecsTaskExecutionRole")

        assert arn == "arn:aws:iam::123:role/test"
        assert created is True
        mock_iam_client.create_role.assert_called_once()
