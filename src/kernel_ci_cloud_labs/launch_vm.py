"""Launch EC2 VM instances with SSM-based multi-run test execution."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import base64
import json
import shlex
import sys
import time
import uuid

import boto3
from botocore.config import Config

from kernel_ci_cloud_labs.core.log_scrub import scrub_text


def log_error(msg):
    """Print error to stderr (shows in container log)."""
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.stderr.flush()


def log_info(msg):
    """Prints info messages from container log (only in VM logs)."""
    sys.stdout.write(f"INFO: {msg}\n")
    sys.stdout.flush()


def log_not(msg):
    """Print info-level diagnostic to stdout (lands in ECS container log).

    Previously a no-op, which made the console-capture / cleanup path
    impossible to debug — failures like "No console output available yet"
    or "Failed to upload console output" were swallowed. Routed to stdout
    so it shows up in CloudWatch under the container log group without
    being styled as an error.
    """
    sys.stdout.write(f"INFO: {msg}\n")
    sys.stdout.flush()


# Kernel-side fatal/near-fatal markers we scan captured console buffers for.
# Hit on any of these gets logged loudly and stamped into the S3 object
# metadata so downstream consumers (KCIDB submitter, triage tooling) can
# flag the run without re-parsing the log. Patterns are case-sensitive
# substrings — kernel prints these verbatim.
PANIC_PATTERNS = (
    "Kernel panic - not syncing",
    "Oops: ",
    "BUG: ",
    "Call Trace:",
    "general protection fault",
    "Unable to handle kernel",
    "double fault",
    "watchdog: BUG: soft lockup",
    "rcu_sched detected stalls",
    "page fault in interrupt",
)


def _scan_for_panic(text):
    """Return the first PANIC_PATTERNS substring found in text, or None."""
    if not text:
        return None
    for pattern in PANIC_PATTERNS:
        if pattern in text:
            return pattern
    return None


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
        # Console-output capture is idempotent: once we've uploaded a non-empty
        # buffer we don't re-upload. The buffer in EC2 only grows, so any
        # second call would either be identical or strictly larger — we
        # accept "strictly larger" by allowing capture again until we get a
        # non-empty response.
        self._console_captured = False

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

                    # Grab console buffer before the VM is terminated by
                    # cleanup() — the tail (panic / OOM / kernel trace) is
                    # the most useful artifact when SSM did not return Success.
                    self.capture_console_output(reason="ssm-failure")
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
        # Same rationale as the SSM-failure branch above: capture the console
        # buffer now, while the VM is still alive, so a panic on the watchdog
        # path is not lost when cleanup() races the EC2 GetConsoleOutput lag.
        self.capture_console_output(reason="ssm-failure")
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

    def capture_console_output(self, reason="cleanup"):
        """Fetch EC2 serial console output (kernel boot log) and upload to S3.

        Re-entrancy rules:
          * "cleanup" is the best-effort pre-terminate pass. It's skipped if
            a previous call already uploaded — there's nothing new to fetch
            while the VM is still running and the buffer only grows.
          * "ssm-failure" and "post-terminate" always run. The first catches
            a panic visible mid-run; the second catches the flushed final
            buffer that EC2 only finalizes after shutdown — typically the
            only place an early-boot panic actually shows up. Both can
            overwrite an earlier (smaller) capture.

        Also scans the scrubbed output for PANIC_PATTERNS and stamps the
        result into the S3 object metadata so triage tooling can flag a run
        without re-reading the log.

        Args:
            reason: Free-text label for the call site (logged for diagnostics).
                Currently used values: "cleanup", "ssm-failure", "post-terminate".
        """
        if not self.instance_id:
            return
        if self._console_captured and reason == "cleanup":
            # cleanup() always runs in the finally block, even if we already
            # grabbed the buffer on SSM failure. Skip the redundant fetch.
            log_not("  Console output already captured this run, skipping")
            return

        log_not(f"\n=== Capturing console output ({reason}) ===")

        # EC2 mirrors the serial console asynchronously. State==terminated
        # does not mean the buffer is flushed: short-lived VMs commonly
        # return an empty Output for 1–3 min after termination. Poll on
        # the post-terminate pass; one-shot is fine for cleanup/ssm-failure
        # because post-terminate covers the lag.
        output_b64 = ""
        if reason == "post-terminate":
            poll_budget = 240  # 4 min; EC2 mirror typically settles in 1–3 min
            poll_interval = 15
            start = time.time()
            attempt = 0
            while time.time() - start < poll_budget:
                attempt += 1
                try:
                    resp = self.ec2.get_console_output(
                        InstanceId=self.instance_id, Latest=True
                    )
                    output_b64 = resp.get("Output", "")
                except Exception as e:
                    log_not(f"  get_console_output error (attempt {attempt}): {e}")
                    output_b64 = ""
                if output_b64:
                    log_not(
                        f"  Console buffer available on attempt {attempt} "
                        f"(after {int(time.time() - start)}s)"
                    )
                    break
                log_not(
                    f"  Console buffer empty (attempt {attempt}); "
                    f"retrying in {poll_interval}s"
                )
                time.sleep(poll_interval)
            if not output_b64:
                log_not(
                    f"  No console output after {attempt} attempts "
                    f"({int(time.time() - start)}s) — EC2 mirror never flushed"
                )
                return
        else:
            try:
                resp = self.ec2.get_console_output(
                    InstanceId=self.instance_id, Latest=True
                )
            except Exception as e:
                log_not(f"  Failed to fetch console output: {e}")
                return
            output_b64 = resp.get("Output", "")
            if not output_b64:
                log_not("  No console output available yet")
                return

        # boto3 returns the buffer base64-encoded; decode for human-readable upload.
        try:
            output = base64.b64decode(output_b64).decode("utf-8", errors="replace")
        except Exception:
            output = output_b64

        # Scrub before upload. The bucket is public-read (KCIDB dashboard users
        # follow the URL we publish), so any secret that lands here would be
        # world-visible. Counters are logged; original strings are not.
        scrubbed, redaction_counts = scrub_text(output)
        if redaction_counts:
            summary = ", ".join(f"{k}={v}" for k, v in sorted(redaction_counts.items()))
            log_not(f"  Console scrub redacted: {summary}")
        output = scrubbed

        # Panic scan runs on the scrubbed buffer so the pattern we log can't
        # re-leak a token the scrubber just redacted.
        panic_match = _scan_for_panic(output)
        if panic_match:
            log_error(
                f"⚠ Kernel panic indicator in console buffer "
                f"(instance={self.instance_id}, marker={panic_match!r}, reason={reason})"
            )

        s3_key = f"{self.run_prefix}/test_{self.test}/output/{self.instance_id}/console-output.log"
        try:
            self.s3.put_object(
                Bucket=self.s3_bucket,
                Key=s3_key,
                Body=output.encode("utf-8"),
                ContentType="text/plain; charset=utf-8",
                Metadata={
                    "capture-reason": reason,
                    # Records that the buffer passed through the scrubber, so an
                    # operator inspecting the object knows it's not raw.
                    "scrubbed": "v1",
                    "panic-detected": "true" if panic_match else "false",
                },
            )
            log_not(f"✓ Console output uploaded ({len(output)} bytes) to s3://{self.s3_bucket}/{s3_key}")
            self._console_captured = True
        except Exception as e:
            log_not(f"  Failed to upload console output: {e}")

    def _wait_for_terminated(self, timeout=90):
        """Poll describe_instances until the VM reaches a terminal state.

        EC2 only finalizes the serial-console buffer at shutdown; a
        get_console_output call against a still-running short-lived VM
        often returns empty because the async mirror hasn't caught up. By
        waiting for ``terminated``/``stopped`` we can re-fetch and get the
        buffer that includes early-boot output and any panic on shutdown.

        Bounded by ``timeout`` so a stuck instance can't pin the pipeline.
        Returns True if a terminal state was observed, False on timeout or
        API error (caller proceeds either way).
        """
        if not self.instance_id:
            return False
        log_not(f"Waiting up to {timeout}s for instance {self.instance_id} to reach terminated/stopped state...")
        start = time.time()
        while time.time() - start < timeout:
            try:
                resp = self.ec2.describe_instances(InstanceIds=[self.instance_id])
                reservations = resp.get("Reservations", [])
                if not reservations or not reservations[0].get("Instances"):
                    # Instance metadata aged out — treat as terminal.
                    return True
                state = reservations[0]["Instances"][0]["State"]["Name"]
                if state in ("terminated", "stopped"):
                    log_not(f"✓ Instance state: {state} (after {int(time.time() - start)}s)")
                    return True
            except Exception as e:
                log_not(f"  describe_instances error: {e} (continuing without wait)")
                return False
            time.sleep(5)
        log_not(f"⚠ Timed out waiting for instance to terminate after {timeout}s")
        return False

    def cleanup(self):
        """Hybrid console capture around instance termination.

        Two captures bracket the terminate call:

          1. Pre-terminate (``reason="cleanup"``) — best-effort while the VM
             is still alive. Often empty for short-lived VMs (EC2's mirror
             lags by minutes), but on a longer run this is the only way to
             grab the buffer if termination later hangs.
          2. Post-terminate (``reason="post-terminate"``) — after shutdown
             EC2 finalizes and preserves the buffer for ~1h. This is the
             pass that reliably catches the boot log, kernel panics, and
             any shutdown-time oops.

        The capture path scans for panic markers and stamps metadata on
        whichever upload wins (post-terminate overwrites cleanup).
        """
        # Best-effort live capture. Skipped silently inside the helper if a
        # prior ssm-failure capture already uploaded.
        self.capture_console_output(reason="cleanup")

        if not self.instance_id:
            return

        log_not(f"\n=== Terminating instance {self.instance_id} ===")
        try:
            self.ec2.terminate_instances(InstanceIds=[self.instance_id])
            log_not("✓ Termination requested")
        except Exception as e:
            log_not(f"Error terminating instance: {e}")
            return

        # Wait for the VM to actually wind down, then grab the flushed
        # buffer. This is where an early-boot panic that never made it into
        # the live mirror finally becomes visible.
        self._wait_for_terminated(timeout=90)
        self.capture_console_output(reason="post-terminate")


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
