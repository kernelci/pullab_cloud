#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Run 1: Track default kernel and install first kernel from S3

set -euxo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

echo "=== Run 1: Track default kernel and install first kernel ==="

# Track default kernel
DEFAULT_KERNEL="$(get_running_kernel)"
echo "Default kernel: $DEFAULT_KERNEL"
echo "$DEFAULT_KERNEL" >default_kernel.txt

# Install first kernel (downloads on-demand from S3)
first_kernel=$(get_first_kernel_rpm_from_dir)
install_specified_kernel_rpm "$first_kernel"

echo "✓ Run 1 completed - kernel installed, will reboot"
exit 0
