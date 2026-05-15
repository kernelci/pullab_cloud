"""Launch EC2 VM instances with SSM-based multi-run test execution."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import json
import shlex
import sys
import time
import uuid

import boto3
from botocore.config import Config


def log_error(msg):
    """Print error to stderr (shows in container log)."""
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.stderr.flush()


def log_info(msg):
    """Prints info messages from container log (only in VM logs)."""
    sys.stdout.write(f"INFO: {msg}\n")
    sys.stdout.flush()


def log_not(msg):  # pylint: disable=unused-argument
    """Suppress info messages from container log (only in VM logs)."""
    # Info messages only appear in VM CloudWatch logs, not container log


class VMLauncher:
    """Launch and manage EC2 instances with multi-run test support."""

    def __init__(self, vm_config=None):
        """Initialize VM launcher with single VM config."""
        if vm_config is None:
            vm_config = {}

        self.region = vm_config.get("region", "us-west-2")
        ami_id = vm_config.get("ami_id", "ami-00c1d63aff2d420ad")

        # Resolve SSM parameter if ami_id starts with resolve:ssm:
        if ami_id.startswith("resolve:ssm:"):
            ssm_param = ami_id.replace("resolve:ssm:", "")
            self.ami_id = self._resolve_ssm_parameter(ssm_param)
            log_info(f"Resolved AMI from SSM: {self.ami_id}")
        else:
            self.ami_id = ami_id

        self.instance_type = vm_config.get("instance_type", "t3.micro")
        self.root_volume_size = vm_config.get("root_volume_size", 8)
        self.role_name = vm_config.get("role_name")
        if not self.role_name:
            raise ValueError("role_name is required in vm_config — VMs need an IAM instance profile")
        self.max_runtime = vm_config.get("max_runtime", 120)
        self.test = vm_config.get("test", "")
        self.s3_bucket = vm_config.get("s3_bucket")
        if not self.s3_bucket:
            raise ValueError("s3_bucket is required in vm_config")
        self.run_prefix = vm_config.get("run_prefix", "")
        self.test_params = vm_config.get("test_params", {})
        self.ec2_log_group = vm_config.get("ec2_log_group", "/ec2/kernel-ci-vms")

        # Use provided test_id or generate new one
        self.test_id = vm_config.get("test_id")
        if not self.test_id:
            self.test_id = f"{self.test}-{str(uuid.uuid4())[:8]}"

        self.instance_id = None

        # Configure boto3 with exponential backoff retry strategy
        retry_config = Config(
            retries={
                "max_attempts": 10,
                "mode": "adaptive",
            }
        )
        self.ec2 = boto3.client("ec2", region_name=self.region, config=retry_config)
        self.ssm = boto3.client("ssm", region_name=self.region, config=retry_config)
        self.s3 = boto3.client("s3", region_name=self.region, config=retry_config)

        log_not(f"Test ID: {self.test_id}")
        log_not(f"max_runtime set to: {self.max_runtime} seconds")

    def _resolve_ssm_parameter(self, parameter_name):
        """Resolve SSM parameter to get AMI ID."""
        ssm = boto3.client("ssm", region_name=self.region)
        try:
            response = ssm.get_parameter(Name=parameter_name)
            return response["Parameter"]["Value"]
        except Exception as e:
            log_error(f"Failed to resolve SSM parameter {parameter_name}: {e}")
            raise

    def prepare_test_artifacts(self):
        """Verify test payload zip exists in S3."""
        log_info(f"\n=== Verifying test artifacts for {self.test} ===")

        # Check if test payload zip exists (uploaded by pipeline)
        payload_key = f"{self.run_prefix}/test_{self.test}/input/{self.test}_test_payload.zip"
        log_info(f"Checking for test payload at s3://{self.s3_bucket}/{payload_key}")

        try:
            self.s3.head_object(Bucket=self.s3_bucket, Key=payload_key)
            log_info("✓ Test payload found")
            return True
        except Exception as e:
            log_error(f" Test payload not found: {e}")
            log_not("Make sure the pipeline uploaded the test payload before launching VM")
            return False

    def spawn_vm(self):
        """Spawn EC2 instance with SSM support."""
        log_info(f"\n=== Spawning EC2 instance ({self.instance_type}) ===")
        log_info(f"AMI: {self.ami_id}")
        log_not(f"Region: {self.region}")
        log_not(f"Role: {self.role_name or 'None'}")

        user_data = f"""#!/bin/bash
set -x
exec > >(tee /var/log/user-data.log|logger -t user-data -s 2>/dev/console) 2>&1

echo "VM starting at $(date)"
echo "Test ID: {self.test_id}"
INSTANCE_ID=$(ec2-metadata --instance-id | cut -d " " -f 2)

# Safety shutdown: terminate VM if no test completes within max_runtime + 10min buffer.
# This catches the case where the orchestrator dies before sending the SSM command.
nohup bash -c 'sleep {self.max_runtime + 600}; \
echo "UserData safety timeout reached, shutting down"; shutdown -h now' &>/dev/null &

# Wait for SSM agent
while ! systemctl is-active --quiet amazon-ssm-agent; do
    echo "Waiting for SSM agent..."
    sleep 2
done
echo "✓ SSM agent is ready"
"""

        params = {
            "ImageId": self.ami_id,
            "MinCount": 1,
            "MaxCount": 1,
            "InstanceType": self.instance_type,
            "UserData": user_data,
            "InstanceInitiatedShutdownBehavior": "terminate",
            "BlockDeviceMappings": [
                {
                    "DeviceName": "/dev/xvda",
                    "Ebs": {
                        "VolumeSize": self.root_volume_size,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                }
            ],
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {
                            "Key": "Name",
                            "Value": f"{self.ec2_log_group.split('/')[-1]}-{self.test_id}",
                        },
                        {"Key": "TestID", "Value": self.test_id},
                        {"Key": "run_prefix", "Value": self.run_prefix},
                    ],
                }
            ],
        }

        params["IamInstanceProfile"] = {"Name": self.role_name}

        log_not("Calling run_instances...")
        response = self.ec2.run_instances(**params)

        if not response.get("Instances"):
            log_error("Failed to launch instance")
            return None

        self.instance_id = response["Instances"][0]["InstanceId"]
        log_info(f"✓ Spawned VM: {self.instance_id}")

        # Wait for instance to be running
        log_not("Waiting for instance to be running...")
        self.ec2.get_waiter("instance_running").wait(InstanceIds=[self.instance_id])
        log_not(f"✓ VM {self.instance_id} is running")

        # Wait for SSM agent
        log_not("Waiting for SSM agent to be ready...")
        max_wait = 300
        start = time.time()
        while time.time() - start < max_wait:
            try:
                response = self.ssm.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [self.instance_id]}]
                )
                if response["InstanceInformationList"]:
                    log_not("✓ SSM agent is ready")
                    return self.instance_id
            except Exception:
                pass
            time.sleep(10)

        log_error(" SSM agent not ready after 5 minutes")
        return None

    def execute_test_via_ssm(self):  # pylint: disable=too-many-statements
        """Execute test via SSM using test-vm-client.sh."""
        log_not("\n=== Executing test via SSM ===")

        # Calculate timeout (base + buffer for reboots)
        total_timeout = min(self.max_runtime + 3600, 43200)  # max 12 hours
        log_not(f"SSM command timeout: {total_timeout}s")
        log_not(f"Wait timeout: {self.max_runtime}s")

        # Build test parameters exports for SSM command
        test_params_cmd = ""
        if self.test_params:
            for key, value in self.test_params.items():
                test_params_cmd += f"export {key.upper()}={shlex.quote(str(value))}\n"

        command = f"""#!/bin/bash
set -x

# Export test parameters
{test_params_cmd}

# Export S3 bucket and test identifiers for use by test scripts
export S3_BUCKET="{self.s3_bucket}"
export RUN_PREFIX="{self.run_prefix}"
export TEST_NAME="{self.test}"

# Download and execute client script
S3_PATH="s3://{self.s3_bucket}/{self.run_prefix}/test_{self.test}/input/test-vm-client.sh"
aws s3 cp "$S3_PATH" /tmp/test-vm-client.sh
chmod +x /tmp/test-vm-client.sh
/tmp/test-vm-client.sh {self.s3_bucket} {self.run_prefix} {self.test} {self.max_runtime}
"""

        response = self.ssm.send_command(
            InstanceIds=[self.instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [command],
                "executionTimeout": [str(total_timeout)],
            },
            TimeoutSeconds=total_timeout,
            CloudWatchOutputConfig={
                "CloudWatchLogGroupName": f"{self.ec2_log_group}/{self.run_prefix}",
                "CloudWatchOutputEnabled": True,
            },
        )

        command_id = response["Command"]["CommandId"]
        log_not(f"Command ID: {command_id}")

        # Wait for completion with timeout
        log_not("Waiting for test to complete...")
        start_time = time.time()

        # Open log file for VM output
        vm_log_path = "/tmp/vm_output.log"
        vm_log = open(vm_log_path, "w", encoding="utf-8")  # pylint: disable=consider-using-with

        while time.time() - start_time < total_timeout:
            time.sleep(5)
            try:
                result = self.ssm.get_command_invocation(CommandId=command_id, InstanceId=self.instance_id)
                status = result["Status"]
                elapsed = int(time.time() - start_time)
                log_not(f"  Status: {status} (elapsed: {elapsed}s)")

                # Get and log output
                stdout = result.get("StandardOutputContent", "")
                stderr = result.get("StandardErrorContent", "")

                if stdout:
                    vm_log.write(stdout)
                    vm_log.flush()
                if stderr:
                    vm_log.write(f"\n=== STDERR ===\n{stderr}\n")
                    vm_log.flush()

                if status in ["Success", "Failed", "TimedOut", "Cancelled"]:
                    vm_log.close()
                    log_not(f"SSM RunCommand final status: {status}")

                    if status == "Success":
                        log_not("✓ SSM command completed successfully")
                        return True
                    if status == "Failed":
                        log_not("⚠ SSM command status: Failed (VM may have shut down before SSM could report)")
                        log_not("  Checking S3 for actual test results...")
                    elif status == "TimedOut":
                        log_error("✗ SSM command timed out")
                    elif status == "Cancelled":
                        log_error("✗ SSM command was cancelled")

                    return False

            except self.ssm.exceptions.InvocationDoesNotExist:
                log_error("  Command not yet available, waiting...")
                continue
            except Exception as e:
                log_error(f"  Error checking status: {e}")
                continue

        # Timeout reached
        vm_log.close()
        log_error(f"✗ Test timeout after {self.max_runtime}s")
        try:
            self.ssm.cancel_command(CommandId=command_id, InstanceIds=[self.instance_id])
            log_not("  Cancelled SSM command")
        except Exception:
            pass
        return False

    def check_test_result(self):
        """Download and verify result.txt from S3. Returns True if SUCCESS, False otherwise."""
        log_not("\n=== Checking test results ===")

        # Check for result.txt in output directory
        s3_key = f"{self.run_prefix}/test_{self.test}/output/{self.instance_id}/result.txt"
        try:
            obj = self.s3.get_object(Bucket=self.s3_bucket, Key=s3_key)
            content = obj["Body"].read().decode("utf-8").strip()
            log_not(f"  result.txt: {content}")

            if "SUCCESS" in content:
                log_not("✓ Test result: SUCCESS")
                return True

            log_error(f"✗ Test result: {content}")
            return False

        except self.s3.exceptions.NoSuchKey:
            log_error("✗ result.txt not found in S3")
            return False
        except Exception as e:
            log_error(f"✗ Failed to read result.txt: {e}")
            return False

    def cleanup(self):
        """Terminate instance."""
        if self.instance_id:
            log_not(f"\n=== Terminating instance {self.instance_id} ===")
            try:
                self.ec2.terminate_instances(InstanceIds=[self.instance_id])
                log_not("✓ Instance terminated")
            except Exception as e:
                log_not(f"Error terminating instance: {e}")


def launch_vms_from_config():
    """Launch and test multiple VMs from environment variables (no config file needed)."""
    import os
    import threading

    # Read config from environment variables
    run_prefix = os.getenv("RUN_PREFIX")
    s3_bucket = os.getenv("S3_BUCKET")
    region = os.getenv("AWS_REGION", "us-west-2")
    config_filename = os.getenv("TEST_CONFIG_FILENAME")

    # Validate required environment variables
    missing_vars = []
    if not run_prefix:
        missing_vars.append("RUN_PREFIX")
    if not s3_bucket:
        missing_vars.append("S3_BUCKET")
    if not config_filename:
        missing_vars.append("TEST_CONFIG_FILENAME")

    if missing_vars:
        log_error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return None

    # Rebuild S3 path from components
    config_key = f"{run_prefix}/{config_filename}"
    log_info(f"Loading TEST_CONFIG from S3: s3://{s3_bucket}/{config_key}")

    try:
        s3 = boto3.client("s3", region_name=region)
        response = s3.get_object(Bucket=s3_bucket, Key=config_key)
        test_config_json = response["Body"].read().decode("utf-8")
    except Exception as e:
        log_error(f"Failed to load TEST_CONFIG from S3: {e}")
        return None

    try:
        test_config = json.loads(test_config_json)
    except json.JSONDecodeError as e:
        log_error(f"Failed to parse TEST_CONFIG: {e}")
        return None

    if not test_config:
        log_error("No test configuration found")
        return None

    # Get the vms array from test_config
    vms_config = test_config.get("vms", [])
    if not vms_config:
        log_not("No VMs configured in test_config.vms")
        return None

    if run_prefix:
        log_info(f"Using RUN_PREFIX from environment: {run_prefix}")
    if s3_bucket:
        log_info(f"Using S3_BUCKET from environment: {s3_bucket}")

    if not s3_bucket:
        raise ValueError("S3_BUCKET environment variable is required")

    ec2_log_group = os.getenv("EC2_LOG_GROUP", "/ec2/kernel-ci-vms")

    shared_config = {
        "s3_bucket": s3_bucket,
        "region": region,
        "role_name": test_config.get("role_name"),
        "test_id": test_config.get("test_id"),
        "run_prefix": run_prefix,
        "ec2_log_group": ec2_log_group,
    }

    # Expand VM configs to handle multiple tests
    expanded_vms = []
    for vm_config in vms_config:
        test_value = vm_config.get("test")

        # Handle list of tests
        if isinstance(test_value, list):
            tests = test_value
        # Handle single test
        else:
            tests = [test_value]

        # Create a VM config for each test
        for test_name in tests:
            vm_copy = vm_config.copy()
            vm_copy["test"] = test_name
            expanded_vms.append(vm_copy)

    # Function to launch and test a single VM instance
    def launch_and_test_vm(vm_config, instance_num, results_list):
        """Launch one VM instance, execute test, and verify results from S3."""
        # Merge shared config with VM-specific config
        full_vm_config = {**shared_config, **vm_config}
        vm_name = f"{vm_config.get('instance_type', 'vm')}-{vm_config.get('test', 'test')}-{instance_num}"

        log_not(f"\n=== Launching VM: {vm_name} ===")
        launcher = VMLauncher(full_vm_config)

        try:
            if not launcher.prepare_test_artifacts():
                log_error(f"FAILED: {vm_name} - Could not prepare test artifacts")
                results_list.append({"vm_name": vm_name, "success": False})
                return

            vm_instance_id = launcher.spawn_vm()
            if not vm_instance_id:
                log_error(f"FAILED: {vm_name} - Could not spawn VM")
                results_list.append({"vm_name": vm_name, "success": False})
                return

            log_info(f"✓ {vm_name} spawned: {vm_instance_id}")

            # Execute test via SSM
            ssm_success = launcher.execute_test_via_ssm()

            # Always check S3 for actual test result (source of truth)
            # SSM may report "Failed" if VM shuts down before reporting final status
            test_passed = launcher.check_test_result()

            if test_passed:
                if not ssm_success:
                    log_info(f"✓ {vm_name} completed successfully (SSM reported failure but S3 confirms success)")
                else:
                    log_info(f"✓ {vm_name} completed successfully")
                results_list.append({"vm_name": vm_name, "success": True, "instance_id": vm_instance_id})
            else:
                log_error(f"FAILED: {vm_name} - Test did not complete successfully")
                results_list.append({"vm_name": vm_name, "success": False})

        except Exception as e:
            log_error(f"FAILED: {vm_name} - {e}")
            results_list.append({"vm_name": vm_name, "success": False})
        finally:
            launcher.cleanup()

    # Launch all VMs in parallel using threads
    threads = []
    results = []

    for vm_config in expanded_vms:
        min_count = vm_config.get("min_count", 1)
        log_not(f"\n=== Queueing {min_count}x {vm_config.get('instance_type')} for test: {vm_config.get('test')} ===")

        # Create a thread for each instance
        for i in range(min_count):
            thread = threading.Thread(target=launch_and_test_vm, args=(vm_config, i + 1, results))
            threads.append(thread)
            thread.start()  # Start immediately

    # Wait for all threads to complete
    log_not(f"\n=== Waiting for {len(threads)} VMs to complete ===")
    for thread in threads:
        thread.join()

    # Count successes and failures
    total = len(results)
    successful = sum(1 for r in results if r["success"])
    failed = total - successful

    log_info(f"\n=== All VMs completed: {successful}/{total} successful, {failed} failed ===")

    # Return success only if all VMs succeeded
    return successful == total and total > 0


if __name__ == "__main__":
    try:
        success = launch_vms_from_config()
        if success:
            log_info("SUCCESS: All VMs launched and tested successfully")
            sys.exit(0)
        elif success is False:
            log_error("FAILED: Some or all VMs failed")
            sys.exit(1)
        else:
            log_error("FAILED: No VMs were launched")
            sys.exit(1)
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        log_not(f"FAILED: {e}")
        sys.exit(1)
