#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

set -euxo pipefail

echo "=== Run 2: Comparing kernel versions ==="

# Check if kernel version file exists
if [ ! -f kernel_version.txt ]; then
    echo "ERROR: kernel_version.txt not found"
    exit 1
fi

# Read stored kernel version
STORED_KERNEL=$(cat kernel_version.txt)
CURRENT_KERNEL=$(uname -r)

echo "Stored kernel version: $STORED_KERNEL"
echo "Current kernel version: $CURRENT_KERNEL"
echo "Uptime: $(uptime || echo "unknown")"

# Compare versions
if [ "$STORED_KERNEL" = "$CURRENT_KERNEL" ]; then
    echo "SUCCESS: Kernel versions match"
    exit 0
else
    echo "FAILURE: Kernel versions do not match"
    exit 1
fi
