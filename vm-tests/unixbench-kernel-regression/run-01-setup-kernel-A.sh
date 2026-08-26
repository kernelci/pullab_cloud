#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

#
# First run: setup benchmark and install first kernel to test

set -euxo pipefail

# Set source directory and source common library for functions and constants
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

# Prepare Unix Bench
install_test_dependencies
prepare_unixbench

# Save kernel version before
kernel_before="$(get_running_kernel_id)"
echo "Kernel before installation: $kernel_before"
save_kernel_version "$kernel_before" "$KERNEL_FILE"

# Install kernel with lower version as kernel to be used next
first_kernel=$(get_first_kernel_rpm_from_dir)
install_specified_kernel_rpm "$first_kernel"

# Stop here, re-execution will happen after reboot and continue in run-02-*.sh
