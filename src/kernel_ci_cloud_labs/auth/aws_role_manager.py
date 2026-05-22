"""AWS IAM role management for Fargate tasks"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import json
from typing import Any, Dict

from kernel_ci_cloud_labs.core.base_resource_manager import BaseResourceManager


class AWSRoleManager(BaseResourceManager):
    """Manages IAM roles required for AWS Fargate operations"""

    def check_exists(self, resource_name: str) -> bool:
        """Check if IAM role exists"""
        try:
            self.client.get_role(RoleName=resource_name)
            return True
        except self.client.exceptions.NoSuchEntityException:
            return False

    @staticmethod
    def _is_ec2_role(resource_config: Dict[str, Any]) -> bool:
        """Return True if the role's trust policy allows EC2 to assume it."""
        trust_policy = resource_config.get("trust_policy", {})
        principals = trust_policy.get("Statement", [{}])[0].get("Principal", {})
        return isinstance(principals, dict) and "ec2.amazonaws.com" in str(principals)

    def _ensure_instance_profile(self, name: str) -> None:
        """Ensure the EC2 instance profile exists AND has the role bound.

        The two operations are independently idempotent. A previous partial
        setup (or manual cleanup) can leave the profile in place with no
        role attached — invisible to RunInstances, but the VM boots without
        credentials so the SSM agent can never register, and downstream
        SSM-based test execution fails with "SSM agent not ready".
        """
        try:
            self.client.create_instance_profile(InstanceProfileName=name)
        except self.client.exceptions.EntityAlreadyExistsException:
            pass
        try:
            profile = self.client.get_instance_profile(InstanceProfileName=name)
            bound = {r["RoleName"] for r in profile["InstanceProfile"]["Roles"]}
        except self.client.exceptions.NoSuchEntityException:
            bound = set()
        if name not in bound:
            try:
                self.client.add_role_to_instance_profile(
                    InstanceProfileName=name, RoleName=name,
                )
            except self.client.exceptions.LimitExceededException:
                # A different role is already bound to this profile;
                # leave it alone rather than silently rebinding.
                pass

    def delete_role(self, resource_name: str) -> None:
        """Delete IAM role and detach policies"""
        try:
            # Detach managed policies
            response = self.client.list_attached_role_policies(RoleName=resource_name)
            for policy in response.get("AttachedPolicies", []):
                self.client.detach_role_policy(RoleName=resource_name, PolicyArn=policy["PolicyArn"])

            # Delete inline policies
            inline_response = self.client.list_role_policies(RoleName=resource_name)
            for policy_name in inline_response.get("PolicyNames", []):
                self.client.delete_role_policy(RoleName=resource_name, PolicyName=policy_name)

            # Delete instance profile if exists
            try:
                self.client.remove_role_from_instance_profile(InstanceProfileName=resource_name, RoleName=resource_name)
                self.client.delete_instance_profile(InstanceProfileName=resource_name)
            except self.client.exceptions.NoSuchEntityException:
                pass

            # Delete role
            self.client.delete_role(RoleName=resource_name)
        except self.client.exceptions.NoSuchEntityException:
            pass

    def create(self, resource_name: str, resource_config: Dict[str, Any]) -> str:
        """Create IAM role with policies and instance profile if needed"""
        # Create role
        response = self.client.create_role(
            RoleName=resource_name,
            AssumeRolePolicyDocument=json.dumps(resource_config["trust_policy"]),
            Description=resource_config.get("description", ""),
        )

        # Attach managed policies
        for policy_arn in resource_config.get("policies", []):
            self.client.attach_role_policy(RoleName=resource_name, PolicyArn=policy_arn)

        # Add inline policies
        for policy_name, policy_doc in resource_config.get("inline_policies", {}).items():
            self.client.put_role_policy(
                RoleName=resource_name,
                PolicyName=policy_name,
                PolicyDocument=json.dumps(policy_doc),
            )

        # Create instance profile for EC2 roles
        if self._is_ec2_role(resource_config):
            self._ensure_instance_profile(resource_name)

        return response["Role"]["Arn"]

    def ensure_exists(
        self,
        resource_name: str,
        resource_config: Dict[str, Any] = None,
        force_recreate: bool = False,
    ):
        """Ensure role exists; also (re-)verify EC2 instance-profile binding.

        The base implementation short-circuits when the role already exists,
        which means a drifted instance profile (profile present but no role
        attached) is never repaired on re-run. We always run the binding
        check after the base call so re-runs heal that state.
        """
        result = super().ensure_exists(resource_name, resource_config, force_recreate)
        config = resource_config if resource_config is not None else self.config.get(resource_name, {})
        if self._is_ec2_role(config):
            self._ensure_instance_profile(resource_name)
        return result

    def get_identifier(self, resource_name: str) -> str:
        """Get role ARN"""
        response = self.client.get_role(RoleName=resource_name)
        return response["Role"]["Arn"]

    def ensure_all_roles(self) -> Dict[str, str]:
        """Ensure all configured roles exist"""
        role_arns = {}
        for role_name, role_config in self.config.items():
            role_arns[role_name] = self.ensure_exists(role_name, role_config)
        return role_arns
