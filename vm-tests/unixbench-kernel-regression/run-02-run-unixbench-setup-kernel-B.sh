#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

#
# Second run: run benchmark for first kernel, and install second kernel to test

set -euxo pipefail

# Set source directory and source common library for functions and constants
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

#Assert if kernel version has changed
kernel_after="$(get_running_kernel)"
echo "Current running kernel: $kernel_after"

kernel_before="$(load_kernel_version "$KERNEL_FILE")"
echo "Kernel before installation: $kernel_before"

assert_kernel_changed "$kernel_before" "$kernel_after"
save_kernel_version "$kernel_after" "$KERNEL_FILE"

# Run Unixbench for current setup
RESULTS_DIR="${PWD}/${KERNEL_BENCH_DIR}/first_kernel"
mkdir -p "$RESULTS_DIR"
run_unixbench "$RESULTS_DIR"
summarize_unixbench_log "$RESULTS_DIR"/unixbench.log "benchmark-base-$(basename $kernel_after).csv"

# Install kernel with higher version as kernel to be used next
last_kernel=$(get_last_kernel_rpm_from_dir "$KERNEL_RPM_DIR")
install_specified_kernel_rpm "$last_kernel"

# Stop here, re-execution will happen after reboot and continue in run-03-*.sh
