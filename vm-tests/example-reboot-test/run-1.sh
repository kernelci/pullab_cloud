#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

set -euxo pipefail

echo "=== Run 1: Recording kernel version ==="
KERNEL_VERSION=$(uname -r)
echo "Current kernel version: $KERNEL_VERSION"

# Store kernel version in file
echo "$KERNEL_VERSION" >kernel_version.txt
echo "Stored kernel version in kernel_version.txt"
echo "Uptime: $(uptime || echo "unknown")"
echo "=== Run 1 completed successfully ==="

exit 0 # Explicit successful termination
