#!/usr/bin/env python3

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

"""
Configure config.json with user-specific resource names and region.

This script updates examples/aws/config.json to use a custom prefix for all
AWS resources (S3 buckets, IAM roles, ECS clusters, etc.) and sets the region.

Usage:
    python3 setup/configure_project.py --prefix kernel-ci-myname- --region eu-west-2

    # Use defaults (kernel-ci-$USER- prefix, us-west-2 region)
    python3 setup/configure_project.py

    # Show what would be changed without modifying
    python3 setup/configure_project.py --dry-run
"""

import argparse
import json
import os
import sys
from pathlib import Path


def get_default_prefix():
    """Get default prefix using $USER environment variable."""
    user = os.environ.get("USER", "user")
    return f"kernel-ci-{user}-"


def update_config(config: dict, prefix: str, region: str, test_filter: str = None) -> dict:
    """Update config with new prefix and region."""

    # Derive resource names from prefix
    # Remove trailing dash for names that don't need it
    base = prefix.rstrip("-")

    # Update region
    config["region"] = region
    config["ecs"]["task_definition"]["region"] = region

    # Update S3 buckets
    config["storage"]["bucket"] = f"{base}-results"
    config["external_storage"]["bucket"] = f"{base}-storage"

    # Update IAM role name (used as key in roles dict)
    old_role_name = list(config["roles"].keys())[0]
    new_role_name = f"{base}-ecs-role"
    role_config = config["roles"].pop(old_role_name)
    config["roles"][new_role_name] = role_config

    # Update role reference in test_config
    config["test_config"]["role_name"] = new_role_name

    # Update PassRole policy to reference new role name
    pass_role_policy = role_config["inline_policies"]["AllowPassRole"]
    pass_role_policy["Statement"][0]["Resource"] = f"arn:aws:iam::*:role/{new_role_name}"

    # Update IAM instance profile policy to reference new role name
    if "AllowIAMInstanceProfile" in role_config["inline_policies"]:
        ip_policy = role_config["inline_policies"]["AllowIAMInstanceProfile"]
        ip_policy["Statement"][0]["Resource"] = f"arn:aws:iam::*:instance-profile/{new_role_name}"

    # Update S3 access policy with new bucket names
    s3_policy = role_config["inline_policies"]["AllowS3Access"]
    s3_policy["Statement"][0]["Resource"] = [
        f"arn:aws:s3:::{base}-results",
        f"arn:aws:s3:::{base}-results/*",
        f"arn:aws:s3:::{base}-results-*",
        f"arn:aws:s3:::{base}-results-*/*",
        f"arn:aws:s3:::{base}-storage",
        f"arn:aws:s3:::{base}-storage/*",
    ]

    # Update ECR repository
    config["ecr"]["repository"] = f"{base}-ecr"

    # Update ECS cluster and task definition
    config["ecs"]["cluster"] = f"{base}-cluster"
    config["ecs"]["task_definition"]["family"] = f"{base}-task"
    config["ecs"]["task_definition"]["container_name"] = f"{base}-app"

    # Update CloudWatch log groups — pick up retention from whatever exists
    old_log_groups = config["cloudwatch"]["log_groups"]
    ecs_retention = next((v for k, v in old_log_groups.items() if "/ecs/" in k), {"retention_days": 7})
    ec2_retention = next((v for k, v in old_log_groups.items() if "/ec2/" in k), {"retention_days": 3})
    config["cloudwatch"]["log_groups"] = {
        f"/ecs/{base}-task": ecs_retention,
        f"/ec2/{base}-vms": ec2_retention,
    }
    config["cloudwatch"]["metrics_namespace"] = f"{base}-metrics"

    # Filter VMs by test name if filter is set
    if test_filter:
        config["test_config"]["vms"] = [
            vm for vm in config["test_config"]["vms"] if any(test_filter in t for t in vm.get("test", []))
        ]
        if not config["test_config"]["vms"]:
            print(
                f"Warning: no tests matched filter '{test_filter}', keeping all VMs",
                file=sys.stderr,
            )

    return config


def print_resource_summary(config: dict, prefix: str, region: str):
    """Print summary of resources that will be created."""
    role_name = list(config["roles"].keys())[0]

    print(f"\n{'='*60}")
    print("RESOURCE SUMMARY")
    print(f"{'='*60}")
    print(f"Prefix: {prefix}")
    print(f"Region: {region}")
    print()
    print("Resources to be created:")
    print("  S3 Buckets:")
    print(f"    - {config['storage']['bucket']}-<ACCOUNT_ID>  (results)")
    print(f"    - {config['external_storage']['bucket']}  (kernel RPMs)")
    print(f"  IAM Role: {role_name}")
    print(f"  ECR Repository: {config['ecr']['repository']}")
    print(f"  ECS Cluster: {config['ecs']['cluster']}")
    print(f"  ECS Task Definition: {config['ecs']['task_definition']['family']}")
    print("  CloudWatch Log Groups:")
    for lg in config["cloudwatch"]["log_groups"]:
        print(f"    - {lg}")
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Configure config.json with user-specific resource names",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--prefix",
        default=get_default_prefix(),
        help=f"Prefix for all AWS resources (default: {get_default_prefix()})",
    )
    parser.add_argument("--region", default="us-west-2", help="AWS region for resources (default: us-west-2)")
    parser.add_argument(
        "--config",
        default="examples/aws/config.json",
        help="Path to config.json (default: examples/aws/config.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying the file",
    )
    parser.add_argument(
        "--force-recreate-roles",
        action="store_true",
        help="Set force_recreate_roles to true (needed on first run or after policy changes)",
    )
    parser.add_argument(
        "--test-filter",
        default=None,
        help="Only include VMs with tests matching this substring (e.g., 'unixbench')",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write configured config to this path instead of modifying the input file",
    )

    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    # Load config
    with open(config_path) as f:
        config = json.load(f)

    # Update config
    config = update_config(config, args.prefix, args.region, args.test_filter)

    if args.force_recreate_roles:
        config["force_recreate_roles"] = True

    # Print summary
    print_resource_summary(config, args.prefix, args.region)

    if args.dry_run:
        print("DRY RUN - no changes made")
        print("\nUpdated config.json would be:")
        print(json.dumps(config, indent=2))
        return

    # Write config
    output_path = Path(args.output) if args.output else config_path
    with open(output_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print(f"{'Wrote' if args.output else 'Updated'} {output_path}")
    print("\nNext steps:")
    bucket = config["external_storage"]["bucket"]
    print(
        f"  1. Upload kernel RPMs:  kernel-ci-cloud-runner aws setup upload-rpms"
        f" --bucket {bucket} --local-rpms <RPM_DIR>"
    )
    print("  2. Run integration test: pytest tests/integration/ -v -m integration")
    print("  3. Or run full pipeline: kernel-ci-cloud-runner aws run")


if __name__ == "__main__":
    main()
