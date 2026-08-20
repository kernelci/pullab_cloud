#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

#
# Third run: run benchmark for second kernel

set -euxo pipefail

# Set source directory and source common library for functions and constants
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

#Assert if kernel version has changed
kernel_after="$(get_running_kernel_id)"
echo "Current running kernel: $kernel_after"

kernel_before="$(load_kernel_version "$KERNEL_FILE")"
echo "Kernel before installation: $kernel_before"

assert_kernel_changed "$kernel_before" "$kernel_after"
save_kernel_version "$kernel_after" "$KERNEL_FILE"

# Run Unixbench for current setup
RESULTS_DIR="${PWD}/${KERNEL_BENCH_DIR}/last_kernel"
mkdir -p "$RESULTS_DIR"
run_unixbench "$RESULTS_DIR"
summarize_unixbench_log "$RESULTS_DIR"/unixbench.log "benchmark-tip-$(uname -r).csv"

# Stop here, this is the last script, no more execution
