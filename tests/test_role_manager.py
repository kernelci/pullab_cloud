"""Unit tests for AWS Role Manager"""
# pylint: disable=protected-access

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

    # ------------------------------------------------------------------
    # Instance-profile binding: regression tests for the "profile exists
    # but role is not attached" foot-gun that caused VMs to boot without
    # IAM credentials and never register with SSM.
    # ------------------------------------------------------------------

    @pytest.fixture
    def mock_iam_with_profile_exceptions(self):
        """Mock IAM client with all the exception types we use."""
        mock = Mock()
        mock.exceptions.NoSuchEntityException = type("NoSuchEntityException", (Exception,), {})
        mock.exceptions.EntityAlreadyExistsException = type("EntityAlreadyExistsException", (Exception,), {})
        mock.exceptions.LimitExceededException = type("LimitExceededException", (Exception,), {})
        return mock

    @pytest.fixture
    def ec2_role_config(self):
        """A role with ec2.amazonaws.com in its trust policy."""
        return {
            "vm-role": {
                "description": "EC2 instance profile role",
                "trust_policy": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": ["ec2.amazonaws.com", "ecs-tasks.amazonaws.com"]},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                },
                "policies": ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"],
            }
        }

    def test_is_ec2_role_detects_ec2_principal(self, ec2_role_config):
        """Roles whose trust policy names ec2.amazonaws.com are EC2 roles."""
        assert AWSRoleManager._is_ec2_role(ec2_role_config["vm-role"]) is True

    def test_is_ec2_role_skips_non_ec2(self, roles_config):
        """ECS-only trust policies should not trigger instance-profile setup."""
        assert AWSRoleManager._is_ec2_role(roles_config["ecsTaskExecutionRole"]) is False

    def test_ensure_instance_profile_creates_and_binds(self, mock_iam_with_profile_exceptions):
        """Fresh profile: create it, then bind the role."""
        client = mock_iam_with_profile_exceptions
        client.get_instance_profile.return_value = {"InstanceProfile": {"Roles": []}}

        manager = AWSRoleManager(client, {})
        manager._ensure_instance_profile("vm-role")

        client.create_instance_profile.assert_called_once_with(InstanceProfileName="vm-role")
        client.add_role_to_instance_profile.assert_called_once_with(
            InstanceProfileName="vm-role", RoleName="vm-role",
        )

    def test_ensure_instance_profile_repairs_empty_profile(self, mock_iam_with_profile_exceptions):
        """Regression: profile already exists but has no role — must bind.

        This is the exact failure mode we hit in production: a previous partial
        setup left an empty instance profile in place. RunInstances accepts it,
        but the VM gets no IAM credentials and SSM never registers it.
        """
        client = mock_iam_with_profile_exceptions
        client.create_instance_profile.side_effect = client.exceptions.EntityAlreadyExistsException()
        client.get_instance_profile.return_value = {"InstanceProfile": {"Roles": []}}

        manager = AWSRoleManager(client, {})
        manager._ensure_instance_profile("vm-role")

        # Even though create raised, the bind must still happen.
        client.add_role_to_instance_profile.assert_called_once_with(
            InstanceProfileName="vm-role", RoleName="vm-role",
        )

    def test_ensure_instance_profile_idempotent_when_already_bound(self, mock_iam_with_profile_exceptions):
        """If our role is already bound, don't re-bind."""
        client = mock_iam_with_profile_exceptions
        client.create_instance_profile.side_effect = client.exceptions.EntityAlreadyExistsException()
        client.get_instance_profile.return_value = {
            "InstanceProfile": {"Roles": [{"RoleName": "vm-role"}]}
        }

        manager = AWSRoleManager(client, {})
        manager._ensure_instance_profile("vm-role")

        client.add_role_to_instance_profile.assert_not_called()

    def test_ensure_instance_profile_preserves_foreign_binding(self, mock_iam_with_profile_exceptions):
        """A different role is bound — leave it, don't silently rebind."""
        client = mock_iam_with_profile_exceptions
        client.create_instance_profile.side_effect = client.exceptions.EntityAlreadyExistsException()
        client.get_instance_profile.return_value = {
            "InstanceProfile": {"Roles": [{"RoleName": "someone-else"}]}
        }
        # IAM enforces max 1 role per profile, so a real add would raise.
        client.add_role_to_instance_profile.side_effect = client.exceptions.LimitExceededException()

        manager = AWSRoleManager(client, {})
        manager._ensure_instance_profile("vm-role")  # must not raise

    def test_ensure_exists_repairs_binding_when_role_already_present(
        self, mock_iam_with_profile_exceptions, ec2_role_config,
    ):
        """The original bug: re-runs against an existing role never repaired
        a drifted profile because the base ensure_exists short-circuits.
        After the fix, ensure_exists must still run the binding check."""
        client = mock_iam_with_profile_exceptions
        # Role exists, so base ensure_exists short-circuits without create().
        client.get_role.return_value = {"Role": {"Arn": "arn:aws:iam::123:role/vm-role"}}
        # Profile exists but is empty (drifted state).
        client.create_instance_profile.side_effect = client.exceptions.EntityAlreadyExistsException()
        client.get_instance_profile.return_value = {"InstanceProfile": {"Roles": []}}

        manager = AWSRoleManager(client, ec2_role_config)
        manager.ensure_exists("vm-role", ec2_role_config["vm-role"])

        # create_role must NOT be called — role already exists.
        client.create_role.assert_not_called()
        # But the binding repair MUST run.
        client.add_role_to_instance_profile.assert_called_once_with(
            InstanceProfileName="vm-role", RoleName="vm-role",
        )
