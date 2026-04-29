# Example Kernel Reboot Test

## Overview
Tests multiple kernel installations and reboots to verify the system can successfully switch between different kernel versions across multiple reboot cycles.

## Test Flow

**Run 1:** `run-01-install-first-kernel.sh`
- Records the default/current kernel version
- Downloads and installs first kernel RPM from S3
- Triggers reboot (exit 0)

**Run 2:** `run-02-install-second-kernel.sh`
- Verifies system booted into first kernel
- Records first kernel version
- Downloads and installs second (last) kernel RPM from S3
- Triggers reboot (exit 0)

**Run 3:** `run-03-verify-second-kernel.sh`
- Verifies system booted into second kernel
- Compares all three kernel versions (default → first → second)
- Confirms kernel changed at each reboot step
- Test passes if: `default ≠ first` AND `first ≠ second`

## Purpose
Validates multi-stage kernel installation workflow and ensures the system can reliably boot through multiple kernel version changes without issues.
