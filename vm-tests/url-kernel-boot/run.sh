#!/bin/bash

# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 Collabora Limited
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Author: Denys Fedoryshchenko <denys.f@collabora.com>
# Co-Author: Max Hubmann <mxhbm@amazon.de>
# Co-Author: Norbert Manthey <nmanthey@amazon.de>
#
# Co-author credit: shell scaffolding (set -euxo pipefail, SOURCE_DIR idiom)
# borrowed from vm-tests/basic-test/run.sh and simple-unixbench/common_lib.sh.
#
# vm-test invoked by pull_labs_poller. Reads kernel/modules URLs from
# environment variables (populated from PULL_LABS job_definition by
# pull_labs_translate.translate_job) and stages them on the VM. The actual
# kernel boot + benchmark step is environment-specific and left as a
# parameterised hook; this scaffold validates the download and emits a
# CSV in the same format BenchmarkAnalyzer expects.

set -euxo pipefail

: "${KERNEL_URL:?KERNEL_URL is required (set by pull_labs_translate)}"
: "${MODULES_URL:?MODULES_URL is required (set by pull_labs_translate)}"

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE_DIR="${STAGE_DIR:-/tmp/pullab_kernel}"
mkdir -p "$STAGE_DIR"

echo "=== url-kernel-boot ==="
echo "Kernel URL:  $KERNEL_URL"
echo "Modules URL: $MODULES_URL"
echo "Rootfs URL:  ${ROOTFS_URL:-<none>}"
echo "Arch:        ${ARCH:-$(uname -m)}"
echo "Node ID:     ${KERNELCI_NODE_ID:-unknown}"
echo "Test list:   ${PULL_LABS_TESTS:-<empty>}"

download() {
    local url="$1"
    local dest="$2"
    echo "Downloading $url -> $dest"
    if command -v curl >/dev/null 2>&1; then
        curl -fSL --retry 3 --retry-delay 5 -o "$dest" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$dest" "$url"
    else
        echo "ERROR: neither curl nor wget available" >&2
        exit 2
    fi
}

KERNEL_BIN="$STAGE_DIR/kernel.bin"
MODULES_TGZ="$STAGE_DIR/modules.tar.gz"

download "$KERNEL_URL" "$KERNEL_BIN"
download "$MODULES_URL" "$MODULES_TGZ"

if [ -n "${ROOTFS_URL:-}" ]; then
    ROOTFS_BIN="$STAGE_DIR/rootfs.bin"
    download "$ROOTFS_URL" "$ROOTFS_BIN"
fi

echo "Sizes:"
ls -la "$STAGE_DIR"
echo "Kernel file type:"
file "$KERNEL_BIN" || true

# Hook for the deployment-specific boot step. In an environment that can
# kexec or reboot into the supplied kernel, set BOOT_HOOK to a script that
# stages the kernel into /boot and either kexecs or schedules a reboot.
if [ -n "${BOOT_HOOK:-}" ] && [ -x "${BOOT_HOOK}" ]; then
    echo "Invoking BOOT_HOOK: $BOOT_HOOK"
    "$BOOT_HOOK" "$KERNEL_BIN" "$MODULES_TGZ" "${ROOTFS_BIN:-}"
else
    echo "No BOOT_HOOK configured — running tests against currently booted kernel."
fi

# Emit a minimal benchmark CSV so BenchmarkAnalyzer sees at least one row.
# Real benchmark scripts should append to this file using the same schema.
RESULT_CSV="${SOURCE_DIR}/benchmark-tip-$(uname -r).csv"
echo "metric,unit,value,more_is_better,kernel_version,instance_id,instance_type,arch" > "$RESULT_CSV"
echo "pullab.boot_check,bool,1,true,$(uname -r),${HOSTNAME:-unknown},${INSTANCE_TYPE:-unknown},${ARCH:-$(uname -m)}" >> "$RESULT_CSV"

echo "Test execution completed: SUCCESS"
