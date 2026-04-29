#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Run 2: Verify first kernel booted and install second kernel

set -euxo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

echo "=== Run 2: Verify first kernel and install second kernel ==="

# Get current kernel
CURRENT_KERNEL="$(get_running_kernel)"
echo "Current kernel: $CURRENT_KERNEL"
echo "$CURRENT_KERNEL" >first_kernel_booted.txt

# Install second (last) kernel
last_kernel=$(get_last_kernel_rpm_from_dir)
install_specified_kernel_rpm "$last_kernel"

echo "✓ Run 2 completed - second kernel installed, will reboot"
exit 0
