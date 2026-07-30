#!/bin/bash

# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 Linaro Limited
#
# vm-test invoked by pull_labs_poller for PULL_LABS jobs of type "ltp".
# Downloads the LTP rootfs (ROOTFS_URL, a KernelCI Debian image with LTP
# installed in /opt/ltp), extracts it and runs the requested LTP command
# files inside a chroot. Like url-kernel-boot, the kernel under test is
# only booted when a BOOT_HOOK is configured; otherwise LTP runs against
# the currently booted kernel.

set -euxo pipefail

: "${ROOTFS_URL:?ROOTFS_URL is required (LTP rootfs, set by pull_labs_translate)}"

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE_DIR="${STAGE_DIR:-/tmp/pullab_ltp}"
CHROOT_DIR="${CHROOT_DIR:-$STAGE_DIR/rootfs}"
mkdir -p "$STAGE_DIR"

echo "=== ltp ==="
echo "Rootfs URL:  $ROOTFS_URL"
echo "Kernel URL:  ${KERNEL_URL:-<none>}"
echo "Arch:        ${ARCH:-$(uname -m)}"
echo "Node ID:     ${KERNELCI_NODE_ID:-unknown}"
echo "Test list:   ${PULL_LABS_TESTS_JSON:-${PULL_LABS_TESTS:-<empty>}}"

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

ROOTFS_TAR="$STAGE_DIR/rootfs.tar"
download "$ROOTFS_URL" "$ROOTFS_TAR"

# Hook for the deployment-specific boot step, same contract as
# url-kernel-boot: stage the kernel into /boot and kexec/reboot.
if [ -n "${BOOT_HOOK:-}" ] && [ -x "${BOOT_HOOK}" ] && [ -n "${KERNEL_URL:-}" ]; then
    KERNEL_BIN="$STAGE_DIR/kernel.bin"
    MODULES_TGZ="$STAGE_DIR/modules.tar.gz"
    download "$KERNEL_URL" "$KERNEL_BIN"
    download "${MODULES_URL:?MODULES_URL is required with BOOT_HOOK}" "$MODULES_TGZ"
    echo "Invoking BOOT_HOOK: $BOOT_HOOK"
    "$BOOT_HOOK" "$KERNEL_BIN" "$MODULES_TGZ" ""
else
    echo "No BOOT_HOOK configured — running LTP against currently booted kernel."
fi

sudo mkdir -p "$CHROOT_DIR"
sudo tar -xf "$ROOTFS_TAR" -C "$CHROOT_DIR" --numeric-owner

if ! sudo test -x "$CHROOT_DIR/opt/ltp/runltp"; then
    echo "ERROR: /opt/ltp/runltp not found in rootfs — not an LTP rootfs?" >&2
    exit 2
fi

cleanup() {
    for m in dev/pts dev proc sys; do
        sudo umount "$CHROOT_DIR/$m" 2>/dev/null || true
    done
}
trap cleanup EXIT

sudo mount -t proc proc "$CHROOT_DIR/proc"
sudo mount -t sysfs sys "$CHROOT_DIR/sys"
sudo mount --bind /dev "$CHROOT_DIR/dev"
sudo mount --bind /dev/pts "$CHROOT_DIR/dev/pts"
sudo cp -L /etc/resolv.conf "$CHROOT_DIR/etc/resolv.conf" || true

# Select LTP command files from the job's test list. Parameters that only
# the LAVA/LKFT runner understands (skipfile, workers, skip_install) are
# reported and ignored.
CMDFILES="$(python3 - <<'PYEOF'
import json
import os
import sys

tests = json.loads(os.environ.get("PULL_LABS_TESTS_JSON") or "[]")
files = []
for t in tests:
    if t.get("type") != "ltp":
        continue
    params = dict(
        kv.split("=", 1)
        for kv in (t.get("parameters") or "").split()
        if "=" in kv
    )
    for name in ("skipfile", "workers", "skip_install"):
        if name in params:
            print(f"WARNING: ignoring unsupported LTP parameter "
                  f"{name}={params[name]}", file=sys.stderr)
    for f in params.get("tst_cmdfiles", "smoketest").split(","):
        if f and f not in files:
            files.append(f)
print(" ".join(files) if files else "smoketest")
PYEOF
)"

RESULT_CSV="${SOURCE_DIR}/benchmark-ltp-$(uname -r).csv"
echo "metric,unit,value,more_is_better,kernel_version,instance_id,instance_type,arch" > "$RESULT_CSV"
CSV_SUFFIX="$(uname -r),${HOSTNAME:-unknown},${INSTANCE_TYPE:-unknown},${ARCH:-$(uname -m)}"

OVERALL_RC=0
for cmdfile in $CMDFILES; do
    echo "=== Running LTP cmdfile: $cmdfile ==="
    rc=0
    sudo chroot "$CHROOT_DIR" /opt/ltp/runltp -f "$cmdfile" -p -q \
        -l "/ltp-$cmdfile.log" -o "/ltp-$cmdfile-output.log" \
        -C "/ltp-$cmdfile-failed.log" || rc=$?

    for suffix in .log -output.log -failed.log; do
        if sudo test -f "$CHROOT_DIR/ltp-$cmdfile$suffix"; then
            sudo cp "$CHROOT_DIR/ltp-$cmdfile$suffix" \
                "$SOURCE_DIR/results_ltp-$cmdfile$suffix"
            sudo chown "$(id -u):$(id -g)" \
                "$SOURCE_DIR/results_ltp-$cmdfile$suffix"
        fi
    done

    passed=0; failed=0; skipped=0
    if [ -f "$SOURCE_DIR/results_ltp-$cmdfile.log" ]; then
        passed=$(grep -c ' PASS ' "$SOURCE_DIR/results_ltp-$cmdfile.log" || true)
        failed=$(grep -c ' FAIL ' "$SOURCE_DIR/results_ltp-$cmdfile.log" || true)
        skipped=$(grep -c ' CONF ' "$SOURCE_DIR/results_ltp-$cmdfile.log" || true)
    fi
    echo "LTP $cmdfile: rc=$rc passed=$passed failed=$failed skipped=$skipped"
    echo "ltp.$cmdfile,bool,$([ "$rc" -eq 0 ] && echo 1 || echo 0),true,$CSV_SUFFIX" >> "$RESULT_CSV"
    echo "ltp.$cmdfile.passed,count,$passed,true,$CSV_SUFFIX" >> "$RESULT_CSV"
    echo "ltp.$cmdfile.failed,count,$failed,false,$CSV_SUFFIX" >> "$RESULT_CSV"
    echo "ltp.$cmdfile.skipped,count,$skipped,false,$CSV_SUFFIX" >> "$RESULT_CSV"

    if [ "$rc" -ne 0 ]; then
        OVERALL_RC=1
    fi
done

if [ "$OVERALL_RC" -ne 0 ]; then
    echo "Test execution completed: FAILURE"
    exit 1
fi
echo "Test execution completed: SUCCESS"
