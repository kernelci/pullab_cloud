#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# First run: build and install kernel from source RPM
set -euxo pipefail

# Set source directory and source common library for functions and constants
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

install_test_dependencies

#Save kernel version before
kernel_before="$(get_running_kernel)"
echo "Kernel before installation: $kernel_before"

save_kernel_version "$kernel_before" "$KERNEL_FILE"

# Install second kernel
install_and_build_kernel "$(get_first_source_kernel_rpm_from_dir)"

echo "✓ Kernel built and installed, will reboot"
