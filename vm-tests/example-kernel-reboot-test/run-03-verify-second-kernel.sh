#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Run 3: Verify second kernel booted successfully

set -euxo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

echo "=== Run 3: Verify second kernel booted ==="

# Get current kernel
FINAL_KERNEL="$(get_running_kernel)"
echo "Final kernel: $FINAL_KERNEL"
echo "$FINAL_KERNEL" >second_kernel_booted.txt

# Read all tracked kernels
if [ -f default_kernel.txt ]; then
    DEFAULT=$(cat default_kernel.txt)
    echo "Default kernel was: $DEFAULT"
fi

if [ -f first_kernel_booted.txt ]; then
    FIRST=$(cat first_kernel_booted.txt)
    echo "First kernel was: $FIRST"
fi

echo "Second kernel is: $FINAL_KERNEL"

# Verify we went through kernel changes
if [ "$DEFAULT" != "$FIRST" ] && [ "$FIRST" != "$FINAL_KERNEL" ]; then
    echo "✓ SUCCESS: Kernel changed across reboots"
    echo "  Default -> First -> Second"
    echo "  $DEFAULT -> $FIRST -> $FINAL_KERNEL"
    exit 0
else
    echo "✗ FAILURE: Kernel did not change as expected"
    exit 1
fi
