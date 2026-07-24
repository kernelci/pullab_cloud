#!/bin/bash

# Authors: Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Run the upstream kvm-unit-tests suite against the guest KVM, exercising the
# kernel's virtualization stack on an EC2 instance with nested virtualization
# enabled. Second stage: runs after run-01 installed the target kernel and the
# client rebooted into it.

set -euxo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SOURCE_DIR}/common_lib.sh"

# 0. Confirm we booted the target kernel that run-01 installed. When run-01 did
#    not install one (no kernel RPMs provided), test the current kernel.
if [ -f "${KERNEL_FILE}" ]; then
    kernel_before="$(load_kernel_version "${KERNEL_FILE}")"
    kernel_now="$(get_running_kernel)"
    echo "Kernel before run-01 install: ${kernel_before}; now running: ${kernel_now}"
    assert_kernel_changed "${kernel_before}" "${kernel_now}"
else
    echo "No recorded pre-install kernel; testing the current kernel ($(get_running_kernel))."
fi

# 1. Hardware gate -- fail fast and clearly on wrong arch / no KVM, before
#    installing anything or building QEMU.
verify_hardware || exit 1

# 2. Dependencies (build toolchain + QEMU)
install_test_dependencies
install_qemu

# 3. Confirm nested virt actually reached this guest (regression check).
verify_nested_virt

# 4. Fetch + build the suite.
fetch_kvm_unit_tests
build_kvm_unit_tests

# 5. Run and capture output.
LOG="${PWD}/kvm-unit-tests.log"
set +e
run_kvm_unit_tests "$LOG"
RUN_RC=$?
set -e

# 6. Summarise into a results file + CSV for KCIDB. The `results_` prefix makes
#    test-vm-client.sh upload both from the test dir (it globs results_* and
#    benchmark-*.csv). The authoritative SUCCESS/FAILED verdict is written by
#    the client from this script's exit code.
summarize_kvm_unit_tests "$LOG" "${PWD}/results_kvm-unit-tests.csv"

{
    echo "kvm-unit-tests result"
    echo "kernel: $(uname -r)  arch: $(uname -m)"
    echo "PASS=${KVMUT_PASS} FAIL=${KVMUT_FAIL} SKIP=${KVMUT_SKIP}"
    echo "real_failures=${KVMUT_REAL_FAIL} (ignore list: ${KVMUT_IGNORE_FAILURES:-<none>})"
    echo "run_tests.sh exit code: ${RUN_RC}"
} >"${PWD}/results_kvm-unit-tests.txt"
cat "${PWD}/results_kvm-unit-tests.txt"

echo "Done executing kvm-unit-tests after $SECONDS seconds"

# 7. Decide pass/fail.
#    Observe-only mode reports but never fails the pipeline -- used while the
#    stable subset of tests under nested virt is still being characterised.
if [ "${KVMUT_OBSERVE_ONLY}" = "true" ]; then
    echo "OBSERVE-ONLY mode: not gating on failures (real_failures=${KVMUT_REAL_FAIL})."
    exit 0
fi

if [ "${KVMUT_REAL_FAIL}" -gt 0 ]; then
    echo "ERROR: ${KVMUT_REAL_FAIL} kvm-unit-tests failed (after ignore list)." >&2
    exit 1
fi

echo "kvm-unit-tests passed: no unexpected failures."
exit 0
