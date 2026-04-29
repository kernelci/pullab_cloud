#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

#
# Second run: verify kernel changed after reboot

set -euxo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

kernel_after="$(get_running_kernel)"
echo "Kernel after reboot: $kernel_after"

kernel_before="$(load_kernel_version "$KERNEL_FILE")"
echo "Kernel before installation: $kernel_before"

assert_kernel_changed "$kernel_before" "$kernel_after"

echo "=== Test Completed Successfully ==="
