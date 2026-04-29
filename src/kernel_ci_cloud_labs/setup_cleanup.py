#!/usr/bin/env python3
"""Find and optionally clean up AWS resources created by Kernel CI Cloud Labs."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

import argparse

import boto3


def find_ec2_instances(ec2, prefix):
    """Find running/pending EC2 instances with matching Name tag."""
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [f"{prefix}*", "kernel-ci-test-*"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ]
    )
    instances = []
    for res in response.get("Reservations", []):
        for inst in res.get("Instances", []):
            name = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "")
            instances.append(
                {
                    "id": inst["InstanceId"],
                    "state": inst["State"]["Name"],
                    "type": inst["InstanceType"],
                    "name": name,
                    "launch": str(inst.get("LaunchTime", "")),
                }
            )
    return instances


def find_ecs_tasks(ecs, cluster):
    """Find running tasks in the cluster."""
    try:
        response = ecs.list_tasks(cluster=cluster, desiredStatus="RUNNING")
        return response.get("taskArns", [])
    except ecs.exceptions.ClusterNotFoundException:
        return []
    except Exception:
        return []


def find_ecs_cluster(ecs, cluster_name):
    """Check if ECS cluster exists."""
    try:
        response = ecs.describe_clusters(clusters=[cluster_name])
        for c in response.get("clusters", []):
            if c.get("status") == "ACTIVE":
                return c
    except Exception:
        pass
    return None


def find_iam_role(iam, role_name):
    """Check if IAM role exists."""
    try:
        iam.get_role(RoleName=role_name)
        return True
    except iam.exceptions.NoSuchEntityException:
        return False


def find_ecr_repo(ecr, repo_name):
    """Check if ECR repository exists."""
    try:
        ecr.describe_repositories(repositoryNames=[repo_name])
        return True
    except ecr.exceptions.RepositoryNotFoundException:
        return False


def find_log_groups(logs, prefix):
    """Find CloudWatch log groups matching prefix."""
    groups = []
    for pfx in [f"/ecs/{prefix}", f"/ec2/{prefix}"]:
        response = logs.describe_log_groups(logGroupNamePrefix=pfx)
        groups.extend(g["logGroupName"] for g in response.get("logGroups", []))
    return groups


def find_s3_buckets(s3, prefix):
    """Find S3 buckets matching prefix."""
    buckets = []
    response = s3.list_buckets()
    for b in response.get("Buckets", []):
        if b["Name"].startswith(prefix):
            buckets.append(b["Name"])
    return buckets


def find_task_definitions(ecs, family_prefix):
    """Find task definition families matching prefix."""
    families = []
    response = ecs.list_task_definition_families(familyPrefix=family_prefix, status="ACTIVE")
    families.extend(response.get("families", []))
    return families


def delete_iam_role(iam, role_name):
    """Delete IAM role with all attached policies and instance profile."""
    # Detach managed policies
    for p in iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", []):
        iam.detach_role_policy(RoleName=role_name, PolicyArn=p["PolicyArn"])
    # Delete inline policies
    for name in iam.list_role_policies(RoleName=role_name).get("PolicyNames", []):
        iam.delete_role_policy(RoleName=role_name, PolicyName=name)
    # Remove from instance profile and delete it
    try:
        iam.remove_role_from_instance_profile(InstanceProfileName=role_name, RoleName=role_name)
        iam.delete_instance_profile(InstanceProfileName=role_name)
    except iam.exceptions.NoSuchEntityException:
        pass
    iam.delete_role(RoleName=role_name)


def empty_and_delete_bucket(bucket_name):
    """Empty and delete an S3 bucket."""
    s3r = boto3.resource("s3")
    bucket = s3r.Bucket(bucket_name)
    bucket.objects.all().delete()
    bucket.delete()


def main():
    parser = argparse.ArgumentParser(description="Find and clean up Kernel CI Cloud Labs AWS resources")
    parser.add_argument("--prefix", required=True, help="Resource prefix (e.g. kernel-ci-myname-)")
    parser.add_argument("--region", default="us-west-2", help="AWS region")
    parser.add_argument("--delete", action="store_true", help="Actually delete resources (default: list only)")
    args = parser.parse_args()

    base = args.prefix.rstrip("-")
    region = args.region

    ec2 = boto3.client("ec2", region_name=region)
    ecs = boto3.client("ecs", region_name=region)
    iam = boto3.client("iam", region_name=region)
    ecr = boto3.client("ecr", region_name=region)
    logs = boto3.client("logs", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    found_anything = False

    # EC2 instances
    instances = find_ec2_instances(ec2, base)
    if instances:
        found_anything = True
        print(f"\nEC2 Instances ({len(instances)}):")
        for i in instances:
            print(f"  {i['id']}  {i['state']:10s}  {i['type']:15s}  {i['name']}  launched={i['launch']}")
        if args.delete:
            ids = [i["id"] for i in instances]
            ec2.terminate_instances(InstanceIds=ids)
            print(f"  → Terminated {len(ids)} instance(s)")

    # ECS tasks
    cluster_name = f"{base}-cluster"
    tasks = find_ecs_tasks(ecs, cluster_name)
    if tasks:
        found_anything = True
        print(f"\nECS Running Tasks ({len(tasks)}):")
        for t in tasks:
            print(f"  {t}")
        if args.delete:
            for t in tasks:
                ecs.stop_task(cluster=cluster_name, task=t)
            print(f"  → Stopped {len(tasks)} task(s)")

    # ECS cluster
    cluster = find_ecs_cluster(ecs, cluster_name)
    if cluster:
        found_anything = True
        print(f"\nECS Cluster: {cluster_name}")
        if args.delete:
            ecs.delete_cluster(cluster=cluster_name)
            print("  → Deleted")

    # Task definitions
    task_families = find_task_definitions(ecs, base)
    if task_families:
        found_anything = True
        print("\nECS Task Definitions:")
        for fam in task_families:
            print(f"  {fam}")
            if args.delete:
                # Deregister all revisions
                revs = ecs.list_task_definitions(familyPrefix=fam, status="ACTIVE")
                for arn in revs.get("taskDefinitionArns", []):
                    ecs.deregister_task_definition(taskDefinition=arn)
                print("  → Deregistered")

    # IAM role
    role_name = f"{base}-ecs-role"
    if find_iam_role(iam, role_name):
        found_anything = True
        print(f"\nIAM Role: {role_name}")
        if args.delete:
            delete_iam_role(iam, role_name)
            print("  → Deleted")
    # Also check the default name
    if find_iam_role(iam, "ecsTaskExecutionRole"):
        found_anything = True
        print("\nIAM Role: ecsTaskExecutionRole (default name)")
        if args.delete:
            delete_iam_role(iam, "ecsTaskExecutionRole")
            print("  → Deleted")

    # ECR repository
    ecr_name = f"{base}-ecr"
    if find_ecr_repo(ecr, ecr_name):
        found_anything = True
        print(f"\nECR Repository: {ecr_name}")
        if args.delete:
            ecr.delete_repository(repositoryName=ecr_name, force=True)
            print("  → Deleted")
    if find_ecr_repo(ecr, "kernel-ci-test"):
        found_anything = True
        print("\nECR Repository: kernel-ci-test (default name)")
        if args.delete:
            ecr.delete_repository(repositoryName="kernel-ci-test", force=True)
            print("  → Deleted")

    # CloudWatch log groups
    log_groups = find_log_groups(logs, base)
    if log_groups:
        found_anything = True
        print(f"\nCloudWatch Log Groups ({len(log_groups)}):")
        for lg in log_groups:
            print(f"  {lg}")
            if args.delete:
                logs.delete_log_group(logGroupName=lg)
                print("  → Deleted")

    # S3 buckets
    buckets = find_s3_buckets(s3, base)
    if buckets:
        found_anything = True
        print(f"\nS3 Buckets ({len(buckets)}):")
        for b in buckets:
            print(f"  {b}")
            if args.delete:
                empty_and_delete_bucket(b)
                print("  → Emptied and deleted")

    if not found_anything:
        print(f"\nNo resources found matching prefix '{base}'")
    elif not args.delete:
        print("\nRun with --delete to remove these resources")


if __name__ == "__main__":
    main()
