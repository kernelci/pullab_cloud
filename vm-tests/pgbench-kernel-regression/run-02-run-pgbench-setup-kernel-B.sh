#!/bin/bash

# Authors: Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

#
# Second run: run pgbench on the first (base) kernel, then install the second
# (higher-version) kernel to test.

set -euxo pipefail

# Set source directory and source common library for functions and constants
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

# Confirm the kernel actually changed after the reboot from run-01. Use the
# build-level identity so a same-NVR-but-different-build kernel still counts.
kernel_after="$(get_running_kernel_id)"
echo "Current running kernel: $(uname -r)  (id: $kernel_after)"
kernel_before="$(load_kernel_version "$KERNEL_FILE")"
echo "Kernel before installation: $kernel_before"
assert_kernel_changed "$kernel_before" "$kernel_after"
save_kernel_version "$kernel_after" "$KERNEL_FILE"

# Make sure PostgreSQL is stopped even if the benchmark fails.
trap stop_postgresql EXIT

# Run pgbench for the base kernel and record the benchmark CSV.
RESULTS_DIR="${PWD}/results"
run_pgbench_suite "$RESULTS_DIR"
summarize_pgbench_output \
    "$RESULTS_DIR/pgbench_readonly.txt" \
    "$RESULTS_DIR/pgbench_readwrite.txt" \
    "benchmark-base-$(uname -r).csv"

# Install the kernel with the higher version as the kernel to be used next.
last_kernel=$(get_last_kernel_rpm_from_dir)
install_specified_kernel_rpm "$last_kernel"

# Stop here; re-execution happens after reboot and continues in run-03-*.sh
