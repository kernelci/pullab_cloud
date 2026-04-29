# Example Multi-Run Reboot Test

Demonstrates state persistence across reboots.

## Test Flow

1. **run-1.sh**: Records kernel version and uptime to `kernel_version.txt`
2. *(automatic reboot)*
3. **run-2.sh**: Reads stored version, compares with current — passes if they match

## Purpose

Smoke test for the multi-stage reboot mechanism. Validates that the working directory persists across reboots and that SSM re-executes the client script correctly.
