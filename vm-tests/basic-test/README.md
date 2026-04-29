# Basic Test

## Overview
Minimal test used primarily for integration testing to verify the pipeline's ability to spawn EC2 instances from the ECS Fargate container and execute basic commands.

## Test Flow

**Run:** `run.sh`
- Prints test name
- Displays current kernel version using `uname -r`
- Exits successfully

## Purpose
This test serves as a **smoke test** for the infrastructure orchestration layer. It validates:
- Container can successfully spawn EC2 VMs
- SSM connectivity works
- Test payload delivery and execution functions correctly
- VM logs are captured and uploaded to S3

Used extensively in integration tests (`pytest tests/integration/`) to verify pipeline behavior without the overhead of complex kernel operations.
