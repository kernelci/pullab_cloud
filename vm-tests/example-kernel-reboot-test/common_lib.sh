# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Common functions for the kernel reboot test.
#
# All kernel-management logic (environment validation, kernel RPM
# download/selection, install_kernel_rpm, reboot helpers) lives in the shared
# vm-tests/lib/kernel_helpers.sh, included here via the kernel_helpers.sh
# symlink in this directory. SOURCE_DIR is set by the run script before this
# file is sourced.
source "${SOURCE_DIR}/kernel_helpers.sh"
