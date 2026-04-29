#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

set -euxo pipefail

echo "Running test: basic-test"
KERNEL_VERSION=$(uname -r)
echo "Kernel version: $KERNEL_VERSION"

echo "Succesful run - exiting"

exit 0
