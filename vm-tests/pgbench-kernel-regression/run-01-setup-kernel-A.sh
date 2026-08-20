#!/bin/bash

# Authors: Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

#
# First run: install PostgreSQL dependencies and the first (lower-version)
# kernel to test. The client reboots into it before run-02.

set -euxo pipefail

# Set source directory and source common library for functions and constants
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

# Install PostgreSQL packages (from dependencies.txt, not test metadata).
install_test_dependencies

# Save a build-level identity of the kernel running before we install kernel A.
# get_running_kernel_id combines uname -r + uname -v + the booted vmlinuz hash,
# so it detects a real kernel switch even when two builds share the same NVR
# (e.g. compiler A/B kernels).
kernel_before="$(get_running_kernel_id)"
echo "Kernel before installation: $kernel_before"
save_kernel_version "$kernel_before" "$KERNEL_FILE"

# Install the kernel with the lower version as the kernel to be used next.
first_kernel=$(get_first_kernel_rpm_from_dir)
install_specified_kernel_rpm "$first_kernel"

# Stop here; re-execution happens after reboot and continues in run-02-*.sh
