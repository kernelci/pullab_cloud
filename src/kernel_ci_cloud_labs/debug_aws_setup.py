#!/usr/bin/env python3

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

"""Debug script to check AWS setup and permissions in container."""

import json
import sys

import boto3
from botocore.exceptions import ClientError


def check_aws_credentials():
    """Check if AWS credentials are configured."""
    print("\n=== Checking AWS Credentials ===")
    try:
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        print("✓ AWS credentials configured")
        print(f"  Account: {identity['Account']}")
        print(f"  User/Role: {identity['Arn']}")
        return True
    except Exception as e:
        print(f"✗ AWS credentials not configured: {e}")
        return False


def check_iam_role(role_name):
    """Check if IAM role exists and has correct policies."""
    print(f"\n=== Checking IAM Role: {role_name} ===")
    try:
        iam = boto3.client("iam")

        # Check role exists
        role = iam.get_role(RoleName=role_name)
        print(f"✓ Role exists: {role_name}")

        # Check trust policy
        trust_policy = role["Role"]["AssumeRolePolicyDocument"]
        principals = trust_policy.get("Statement", [{}])[0].get("Principal", {})
        services = principals.get("Service", [])
        if isinstance(services, str):
            services = [services]

        print(f"  Trust policy services: {', '.join(services)}")
        if "ec2.amazonaws.com" in services:
            print("  ✓ ec2.amazonaws.com in trust policy")
        else:
            print("  ✗ ec2.amazonaws.com NOT in trust policy")

        # Check attached policies
        policies = iam.list_attached_role_policies(RoleName=role_name)
        print("  Attached policies:")
        for policy in policies["AttachedPolicies"]:
            print(f"    - {policy['PolicyName']}")
            if "SSM" in policy["PolicyName"]:
                print("      ✓ SSM policy found")

        return True
    except ClientError as e:
        print(f"✗ Role check failed: {e}")
        return False


def check_instance_profile(profile_name):
    """Check if instance profile exists."""
    print(f"\n=== Checking Instance Profile: {profile_name} ===")
    try:
        iam = boto3.client("iam")
        profile = iam.get_instance_profile(InstanceProfileName=profile_name)
        print(f"✓ Instance profile exists: {profile_name}")

        roles = profile["InstanceProfile"].get("Roles", [])
        if roles:
            print("  Attached roles:")
            for role in roles:
                print(f"    - {role['RoleName']}")
        else:
            print("  ✗ No roles attached to instance profile")

        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            print(f"✗ Instance profile does NOT exist: {profile_name}")
        else:
            print(f"✗ Instance profile check failed: {e}")
        return False


def check_s3_bucket(bucket_name):
    """Check if S3 bucket is accessible."""
    print(f"\n=== Checking S3 Bucket: {bucket_name} ===")
    try:
        s3 = boto3.client("s3")
        s3.head_bucket(Bucket=bucket_name)
        print(f"✓ S3 bucket accessible: {bucket_name}")

        # Try to list objects
        response = s3.list_objects_v2(Bucket=bucket_name, MaxKeys=5)
        count = response.get("KeyCount", 0)
        print(f"  Objects in bucket: {count}+ (showing first 5)")

        return True
    except ClientError as e:
        print(f"✗ S3 bucket check failed: {e}")
        return False


def check_ec2_permissions(region=None):
    """Check if we can describe EC2 instances in the given region."""
    print("\n=== Checking EC2 Permissions ===")
    try:
        ec2 = boto3.client("ec2", region_name=region)
        _ = ec2.describe_instances(MaxResults=5)
        print("✓ Can describe EC2 instances")
        return True
    except ClientError as e:
        print(f"✗ EC2 permissions check failed: {e}")
        return False


def check_ssm_permissions(region=None):
    """Check if we can use SSM in the given region."""
    print("\n=== Checking SSM Permissions ===")
    try:
        ssm = boto3.client("ssm", region_name=region)
        response = ssm.describe_instance_information(MaxResults=5)
        print("✓ Can describe SSM instances")
        count = len(response.get("InstanceInformationList", []))
        print(f"  SSM-managed instances visible: {count}")
        return True
    except ClientError as e:
        print(f"✗ SSM permissions check failed: {e}")
        return False


def main():
    """Run all checks."""
    import os

    print("=" * 60)
    print("AWS Setup Debug Script")
    print("=" * 60)

    # Load config from environment variables
    try:
        test_config_json = os.getenv("TEST_CONFIG")
        if not test_config_json:
            print("\n✗ TEST_CONFIG environment variable not set")
            return 1

        test_config = json.loads(test_config_json)

        config = {
            "region": os.getenv("AWS_REGION", "us-west-2"),
            "storage": {"bucket": os.getenv("S3_BUCKET")},
            "test_config": test_config,
        }

        print("\n✓ Config loaded from environment variables")
        print(f"  Region: {config.get('region')}")
        print(f"  S3 Bucket: {config.get('storage', {}).get('bucket')}")
        print(f"  Role: {config.get('test_config', {}).get('role_name')}")
    except Exception as e:
        print(f"\n✗ Could not load config from environment: {e}")
        return 1

    # Run checks
    results = []
    results.append(check_aws_credentials())

    role_name = config.get("test_config", {}).get("role_name")
    if role_name:
        results.append(check_iam_role(role_name))
        results.append(check_instance_profile(role_name))

    bucket_name = config.get("storage", {}).get("bucket")
    if bucket_name:
        results.append(check_s3_bucket(bucket_name))

    results.append(check_ec2_permissions(config["region"]))
    results.append(check_ssm_permissions(config["region"]))

    # Summary
    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Summary: {passed}/{total} checks passed")
    print("=" * 60)

    return 0 if all(results) else 1


if __name__ == "__main__":
    main()
    sys.exit()
