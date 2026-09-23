#!/bin/bash

# Authors: Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# First stage: install the target kernel from the pipeline's shared kernel-rpms
# area and boot into it, so run-02 exercises kvm-unit-tests against the kernel
# under test rather than the AMI default. The kernel-management flow is reused
# from unixbench-kernel-regression. After this script the client reboots (a
# second run-*.sh follows), bringing up the installed kernel.

set -euxo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

# Record the kernel we booted from so run-02 can confirm the switch.
kernel_before="$(get_running_kernel)"
echo "Kernel before installation: $kernel_before"
save_kernel_version "$kernel_before" "$KERNEL_FILE"

# Install the target kernel (lowest version in the shared kernel-rpms area).
# If no kernel RPMs are provided (e.g. a standalone run without the pipeline's
# S3 layout), keep the current kernel and let run-02 test that instead.
if first_kernel="$(get_first_kernel_rpm_from_dir)"; then
    install_specified_kernel_rpm "$first_kernel"
    echo "Installed target kernel; the client reboots into it before run-02."
else
    echo "No kernel RPMs found in S3; keeping the current kernel for run-02."
fi

# Re-execution continues in run-02-kvm-unit-tests.sh after the reboot.
