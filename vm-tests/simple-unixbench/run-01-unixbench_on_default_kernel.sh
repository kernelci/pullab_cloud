#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# First run: setup benchmark and install first kernel to test
set -euxo pipefail

# Set source directory and source common library for functions and constants
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

# Prepare Unix Bench
install_test_dependencies
prepare_unixbench

# Run Unixbench for current setup
RESULTS_DIR="${PWD}/${KERNEL_BENCH_DIR}/first_kernel"
mkdir -p "$RESULTS_DIR"
current_kernel="$(get_running_kernel)"

run_unixbench "$RESULTS_DIR"
summarize_unixbench_log "$RESULTS_DIR"/unixbench.log "benchmark-$current_kernel.csv"

echo "Done executing simple unixbench test after $SECONDS seconds"
