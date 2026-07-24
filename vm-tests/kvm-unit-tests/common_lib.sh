# Authors: Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Common functions for the kvm-unit-tests test.
#
# This test exercises the KVM hypervisor *inside* an EC2 guest that has nested
# virtualization enabled (c8i/c7i families, see
# _supports_nested_virtualization() in launch_vm.py). The daily KernelCI cloud
# pipeline installs the target kernel, reboots into it, then runs the upstream
# kvm-unit-tests suite against that kernel's KVM -- a per-kernel regression
# signal for the virtualization stack.

# ---------------------------------------------------------------------------
# Tunables (overridable via environment for experimentation)
# ---------------------------------------------------------------------------
# Upstream source and pinned revision for reproducible runs.
KVMUT_REPO="${KVMUT_REPO:-https://gitlab.com/kvm-unit-tests/kvm-unit-tests.git}"
KVMUT_REPO_MIRROR="${KVMUT_REPO_MIRROR:-https://git.kernel.org/pub/scm/virt/kvm/kvm-unit-tests.git}"
# Pin to a tag/commit for reproducibility so upstream test-suite changes cannot
# break the pipeline. Validated on c8i 2026-07-10 (upstream v2026-04-17-72-g1da1819e).
# Set KVMUT_REF="" to track the default branch tip instead.
KVMUT_REF="${KVMUT_REF:-1da1819e49fc4938985edca67df669099b4c87a7}"
# Directory (persists across reboots in the client work dir).
KVMUT_DIR="${KVMUT_DIR:-${PWD}/kvm-unit-tests}"
# Observe-only mode: when "true", the test reports counts but always exits 0.
# Default is "false" because the default KVMUT_TESTS subset below is a curated,
# proven-stable set (6/6 PASS on c8i 2026-07-10) suitable as a real gate. Set
# "true" when running the FULL suite (KVMUT_TESTS="") to characterise results
# without failing the pipeline.
KVMUT_OBSERVE_ONLY="${KVMUT_OBSERVE_ONLY:-false}"
# Space-separated test names excluded from the pass/fail decision (known flaky
# or nested-virt-sensitive). Defaults to the full-suite failures observed on
# c8i 2026-07-10 so that a full-suite run still gates on the stable core:
#   timeouts (xapic, access, vmx_apicv_test, vmx_posted_intr_test,
#   vmx_pf_exception_test), MSR/PMU emulation gaps (msr, msr64, pmu),
#   nested-VMX instability (vmx SIGSEGV, la57).
KVMUT_IGNORE_FAILURES="${KVMUT_IGNORE_FAILURES-xapic access vmx_apicv_test vmx_posted_intr_test vmx_pf_exception_test msr msr64 pmu vmx la57}"
# Optional explicit test group to run (passed to run_tests.sh -g). Empty = all.
KVMUT_GROUP="${KVMUT_GROUP:-}"
# Explicit subset of test names to run (space-separated, passed positionally to
# run_tests.sh). Defaults to a small, fast, nested-virt-stable smoke set proven
# to pass on c8i (2026-07-10). Set KVMUT_TESTS="" to run the full suite, or
# KVMUT_TESTS="a b c" to pick your own.
KVMUT_TESTS="${KVMUT_TESTS-debug intel_iommu lam vmx_init_signal_test vmx_sipi_signal_test hyperv_clock}"

# Error trap handler to show the line where an error occurred
error_trap()
{
    local exit_code=$?
    local line_number=$1
    echo "$(date): ERROR: Script failed at line $line_number with exit code $exit_code"
    echo "$(date): ERROR: Command that failed: $(sed -n "${line_number}p" "$0")"
    exit $exit_code
}
trap 'error_trap $LINENO' ERR

# Install a single given package (yum first, dnf fallback)
install_package()
{
    local pkg="$1"
    local output
    echo "Installing package $pkg ..."
    if output=$(sudo yum install -y "$pkg" 2>&1) || output=$(sudo dnf install -y "$pkg" 2>&1); then
        return 0
    else
        echo "Failed to install package $pkg:"
        echo "$output"
        return 1
    fi
}

# Install all packages listed in dependencies.txt
install_test_dependencies()
{
    local deps_file="${SOURCE_DIR}/dependencies.txt"
    if [ -f "$deps_file" ]; then
        while IFS= read -r pkg || [ -n "$pkg" ]; do
            [[ -z "$pkg" || "$pkg" =~ ^[[:space:]]*# ]] && continue
            pkg=$(echo "$pkg" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            [ -n "$pkg" ] && { install_package "$pkg" || return 1; }
        done <"$deps_file"
    fi
}

# ---------------------------------------------------------------------------
# Kernel management (reused from unixbench-kernel-regression): install the
# target kernel from the pipeline's shared kernel-rpms area and boot into it,
# so the suite runs against the kernel under test rather than the AMI default.
# ---------------------------------------------------------------------------
# Results bucket + kernel paths, populated from the pipeline environment.
RESULTS_BUCKET="${S3_BUCKET:-}"
ARCH="$(uname -m)"
KERNEL_RPM_DIR="/tmp/kernel-rpms"
KERNEL_FILE="${SOURCE_DIR}/kernel_version_before.txt"

get_running_kernel()
{
    uname -r
}

save_kernel_version()
{
    local version="$1"
    local out_file="$2"
    if [ -z "$version" ] || [ -z "$out_file" ]; then
        echo "ERROR: save_kernel_version requires version and file" >&2
        return 1
    fi
    echo "$version" >"$out_file"
}

load_kernel_version()
{
    local in_file="$1"
    if [ ! -f "$in_file" ]; then
        echo "ERROR: Kernel version file not found: $in_file" >&2
        return 1
    fi
    cat "$in_file"
}

assert_kernel_changed()
{
    local before="$1"
    local after="$2"
    if [ "$before" = "$after" ]; then
        echo "ERROR: kernel version did not change (still $after)" >&2
        return 1
    fi
    echo "Kernel version changed from $before to $after"
}

# List available kernel RPMs from the shared S3 area.
list_kernels_from_s3()
{
    S3_PATH="s3://${RESULTS_BUCKET}/${RUN_PREFIX:-}/shared/kernel-rpms/binary/${ARCH}/"
    aws s3 ls "${S3_PATH}" | grep "\.rpm$" | awk '{print $4}'
}

# Download a specific kernel RPM from S3.
download_kernel_rpm()
{
    if [ -z "${1:-}" ]; then
        echo "ERROR: download_kernel_rpm requires kernel_name parameter" >&2
        return 1
    fi
    local kernel_name="$1"
    S3_PATH="s3://${RESULTS_BUCKET}/${RUN_PREFIX:-}/shared/kernel-rpms/binary/${ARCH}/"
    mkdir -p "$KERNEL_RPM_DIR"
    local local_path="${KERNEL_RPM_DIR}/${kernel_name}"
    if [ -f "$local_path" ]; then
        echo "$local_path"
        return 0
    fi
    if aws s3 cp "${S3_PATH}${kernel_name}" "$local_path" --no-progress >&2; then
        echo "$local_path"
        return 0
    else
        echo "ERROR: Failed to download kernel" >&2
        return 1
    fi
}

# Return the lowest-versioned kernel RPM (downloads it from S3).
get_first_kernel_rpm_from_dir()
{
    local kernels=$(list_kernels_from_s3 | sort -V)
    local first_kernel=$(echo "$kernels" | head -n 1)
    if [ -z "$first_kernel" ]; then
        return 1
    fi
    download_kernel_rpm "$first_kernel"
}

# Dump boot configuration for debugging kernel install issues.
dump_boot_info()
{
    echo "=== Boot Debug Info ==="
    echo "--- OS ---"
    head -2 /etc/os-release 2>/dev/null || true
    echo "--- Running kernel ---"
    uname -r
    echo "--- Installed kernel packages ---"
    rpm -qa 'kernel*' | sort
    echo "--- vmlinuz files in /boot ---"
    ls -la /boot/vmlinuz-* 2>/dev/null || echo "(none)"
    echo "--- BLS entries ---"
    ls -la /boot/loader/entries/ 2>/dev/null || echo "(no BLS directory)"
    echo "--- grubby default ---"
    sudo grubby --default-kernel 2>/dev/null || echo "(grubby --default-kernel failed)"
    echo "=== End Boot Debug Info ==="
}

# Install a kernel RPM and make it the default boot target.
install_kernel_rpm()
{
    if [ -z "${1:-}" ]; then
        echo "ERROR: install_kernel_rpm requires kernel_rpm parameter" >&2
        return 1
    fi
    local kernel_rpm="$1"

    local host_arch=$(uname -m)
    local rpm_arch=$(rpm -qp --queryformat '%{ARCH}' "$kernel_rpm" 2>/dev/null)
    if [ "$rpm_arch" != "$host_arch" ]; then
        echo "ERROR: Architecture mismatch - Host: $host_arch, RPM: $rpm_arch" >&2
        return 1
    fi

    echo "kernel before installation: $(uname -r)"
    echo "Installing kernel from $kernel_rpm (arch: $rpm_arch)"

    if sudo yum localinstall -y "$kernel_rpm" 2>/dev/null || sudo dnf install -y "$kernel_rpm" 2>/dev/null; then
        dump_boot_info

        # Set the newly installed kernel as default boot target. Without this,
        # GRUB may boot a different kernel than the one just installed.
        local installed_version
        installed_version=$(rpm -qp --queryformat '%{VERSION}' "$kernel_rpm" 2>/dev/null)

        local grub_kernel
        grub_kernel=$(sudo grubby --info=ALL 2>/dev/null \
            | grep "^kernel=" \
            | grep "$installed_version" \
            | head -1 \
            | sed 's/^kernel=//' \
            | tr -d '"' \
            || true)

        if [ -z "$grub_kernel" ]; then
            # Upstream make binrpm-pkg kernels don't register with grubby; find
            # the vmlinuz file and add a boot entry manually.
            local vmlinuz
            vmlinuz=$(ls /boot/vmlinuz-*"$installed_version"* 2>/dev/null | head -1)
            if [ -n "$vmlinuz" ]; then
                echo "Adding grubby entry for $vmlinuz"
                local initrd="/boot/initramfs-${installed_version}.img"
                if [ ! -f "$initrd" ]; then
                    echo "Generating initramfs at $initrd"
                    sudo dracut --force "$initrd" "$installed_version" 2>/dev/null \
                        || sudo mkinitrd "$initrd" "$installed_version" 2>/dev/null \
                        || true
                fi
                if [ -f "$initrd" ]; then
                    sudo grubby --add-kernel="$vmlinuz" \
                        --initrd="$initrd" \
                        --title="Linux $installed_version" \
                        --copy-default \
                        --make-default
                    echo "Added and set default: $vmlinuz"
                else
                    echo "WARNING: No initramfs for $installed_version, trying set-default anyway"
                    sudo grubby --set-default="$vmlinuz" || true
                fi
                grub_kernel="$vmlinuz"
            else
                echo "WARNING: No vmlinuz found for version $installed_version"
            fi
        else
            echo "Setting default boot kernel to $grub_kernel"
            sudo grubby --set-default="$grub_kernel"
        fi

        if [ -n "$grub_kernel" ]; then
            echo "Verifying default kernel:"
            sudo grubby --default-kernel
        fi
        return 0
    else
        echo "ERROR: Failed to install new kernel" >&2
        return 1
    fi
}

# Install a given kernel RPM (path passed as argument).
install_specified_kernel_rpm()
{
    local kernel_rpm="$1"
    if [ -z "$kernel_rpm" ]; then
        echo "ERROR: install_specified_kernel_rpm requires a kernel RPM path" >&2
        return 1
    fi
    echo "Installing kernel RPM: $(basename "$kernel_rpm")"
    install_kernel_rpm "$kernel_rpm"
}

# AL2023 core repos ship no qemu system emulator (only qemu-img), and SPAL also
# does not ship it. So build a minimal x86_64-softmmu QEMU from source via the
# vendored build-qemu.sh (--enable-kvm, installs to /opt/qemu).
# Sets and exports QEMU for run_tests.sh.
install_qemu()
{
    QEMU_BIN=""
    if command -v qemu-system-x86_64 >/dev/null 2>&1; then
        QEMU_BIN="$(command -v qemu-system-x86_64)"
        echo "QEMU already present: $QEMU_BIN"
    else
        echo "No distro qemu-system-x86_64 on AL2023; building QEMU from source ..."
        sudo -E QEMU_VERSION="${QEMU_VERSION:-9.2.0}" bash "${SOURCE_DIR}/build-qemu.sh"
        [ -x /opt/qemu/bin/qemu-system-x86_64 ] && QEMU_BIN=/opt/qemu/bin/qemu-system-x86_64
    fi
    if [ -z "$QEMU_BIN" ] || [ ! -x "$QEMU_BIN" ]; then
        echo "ERROR: no usable qemu-system-x86_64 after build" >&2
        return 1
    fi
    export QEMU="$QEMU_BIN"
    export PATH="$(dirname "$QEMU_BIN"):$PATH"
    echo "Using QEMU: $QEMU ($("$QEMU" --version | head -1))"
    return 0
}

# Hardware gate: this test profile targets x86_64 with Intel VT-x (VMX) and a
# usable /dev/kvm -- the only configuration AWS EC2 nested virtualization
# supports (Intel C8i/M8i/R8i/C7i/... families). Fail fast and clearly on
# anything else, before the expensive QEMU build.
verify_hardware()
{
    echo "=== Hardware gate ==="
    local arch; arch="$(uname -m)"
    if [ "$arch" != "x86_64" ]; then
        echo "ERROR: unsupported architecture '$arch'." >&2
        echo "      This kvm-unit-tests profile supports x86_64 (Intel) only. EC2 nested" >&2
        echo "      virtualization is Intel-VT-x only (C8i/M8i/R8i/C7i/... families)." >&2
        echo "      aarch64/Graviton and AMD KVM require a bare-metal instance plus an arch-" >&2
        echo "      generalized qemu build (aarch64-softmmu / SVM test set), not yet implemented." >&2
        return 1
    fi
    if [ ! -e /dev/kvm ]; then
        echo "ERROR: /dev/kvm is missing -- hardware virtualization is not available." >&2
        echo "      Launch a nested-virt-capable Intel family (C8i/C7i/...) with" >&2
        echo "      CpuOptions={NestedVirtualization:enabled}, or use a bare-metal host." >&2
        return 1
    fi
    if grep -qw vmx /proc/cpuinfo; then
        echo "OK: x86_64 + Intel VT-x (vmx) + /dev/kvm present ($(ls -l /dev/kvm))."
        return 0
    elif grep -qw svm /proc/cpuinfo; then
        echo "ERROR: AMD SVM detected, but this profile targets Intel VMX." >&2
        echo "      The default subset exercises Intel vmx_* tests. Running on AMD needs the" >&2
        echo "      SVM test set on a bare-metal AMD host (EC2 nested virt is Intel-only)." >&2
        return 1
    else
        echo "ERROR: no CPU virtualization flag (vmx/svm) exposed to the guest CPU." >&2
        return 1
    fi
}

# Confirm nested virtualization actually reached the guest. This doubles as a
# regression check on the EC2 nested-virt enablement itself.
verify_nested_virt()
{
    echo "=== Verifying nested virtualization is available ==="
    if [ ! -e /dev/kvm ]; then
        echo "ERROR: /dev/kvm is missing -- nested virtualization is NOT enabled on this instance." >&2
        echo "       Ensure the VM uses a supported family (c8i/c7i/...) so launch_vm.py sets" >&2
        echo "       CpuOptions={NestedVirtualization: enabled}." >&2
        return 1
    fi
    echo "/dev/kvm present:"
    ls -l /dev/kvm
    grep -m1 -oE 'vmx|svm' /proc/cpuinfo | head -1 || {
        echo "ERROR: no vmx/svm virtualization flag exposed to the guest CPU." >&2
        return 1
    }
    echo "Virtualization CPU flag exposed to guest: OK"
    return 0
}

# Fetch kvm-unit-tests (primary repo, then kernel.org mirror as fallback).
fetch_kvm_unit_tests()
{
    if [ -d "${KVMUT_DIR}/.git" ]; then
        echo "kvm-unit-tests already checked out at ${KVMUT_DIR}"
        return 0
    fi
    echo "=== Cloning kvm-unit-tests ==="
    if ! git clone "${KVMUT_REPO}" "${KVMUT_DIR}"; then
        echo "Primary clone failed, trying mirror ${KVMUT_REPO_MIRROR}"
        git clone "${KVMUT_REPO_MIRROR}" "${KVMUT_DIR}"
    fi
    if [ -n "${KVMUT_REF}" ]; then
        echo "Checking out pinned ref ${KVMUT_REF}"
        git -C "${KVMUT_DIR}" checkout --detach "${KVMUT_REF}"
    fi
    ( cd "${KVMUT_DIR}" && git submodule update --init >/dev/null 2>&1 || true )
    echo "kvm-unit-tests revision: $(git -C "${KVMUT_DIR}" rev-parse --short HEAD)"
}

# Configure + build the test binaries.
build_kvm_unit_tests()
{
    echo "=== Building kvm-unit-tests ==="
    ( cd "${KVMUT_DIR}" && ./configure && make -j"$(nproc)" )
}

# Run the suite, tee-ing full output to a log. Uses standalone tests so a single
# invocation runs the whole set. Returns run_tests.sh's own exit code.
run_kvm_unit_tests()
{
    local logfile="$1"
    echo "=== Running kvm-unit-tests (accel=kvm) ==="
    local args=(-v)
    [ -n "${KVMUT_GROUP}" ] && args+=(-g "${KVMUT_GROUP}")
    # Optional explicit subset of test names (positional args to run_tests.sh).
    local tests=()
    [ -n "${KVMUT_TESTS}" ] && read -r -a tests <<< "${KVMUT_TESTS}"
    [ ${#tests[@]} -gt 0 ] && echo "Running subset: ${tests[*]}"
    # ACCEL=kvm forces hardware acceleration; the run fails loudly if KVM is
    # unavailable rather than silently falling back to slow TCG emulation.
    ( cd "${KVMUT_DIR}" && ACCEL=kvm ./run_tests.sh "${args[@]}" "${tests[@]}" ) 2>&1 | tee "$logfile"
    return "${PIPESTATUS[0]}"
}

# Parse run_tests.sh output into PASS/FAIL/SKIP counts and a per-test CSV.
# Writes:
#   result.txt        human summary
#   kvm-unit-tests.csv machine-readable per-test rows for KCIDB ingestion
# Sets globals: KVMUT_PASS KVMUT_FAIL KVMUT_SKIP KVMUT_REAL_FAIL
summarize_kvm_unit_tests()
{
    local logfile="$1"
    local csv="$2"
    local kver arch clean
    kver="$(uname -r)"
    arch="$(uname -m)"

    # run_tests.sh emits ANSI colour codes (e.g. \e[32mPASS\e[0m), so strip them
    # to a clean copy before matching line-anchored PASS/FAIL/SKIP.
    clean="$(mktemp)"
    sed -E 's/\x1b\[[0-9;]*m//g' "$logfile" >"$clean"

    KVMUT_PASS=$(grep -cE '^PASS ' "$clean" || true)
    KVMUT_FAIL=$(grep -cE '^FAIL ' "$clean" || true)
    KVMUT_SKIP=$(grep -cE '^SKIP ' "$clean" || true)

    # Per-test CSV, KCIDB-friendly: test_name,status,kernel_version,arch
    echo "test_name,status,kernel_version,arch" >"$csv"
    # Guard against pipefail when there are zero matches.
    grep -E '^(PASS|FAIL|SKIP) ' "$clean" | while read -r status name _rest; do
        echo "${name},${status},${kver},${arch}" >>"$csv"
    done || true

    # Count "real" failures excluding the ignore list.
    KVMUT_REAL_FAIL=0
    local name
    while read -r _status name _rest; do
        [ -z "$name" ] && continue
        local ignored=0
        for ig in ${KVMUT_IGNORE_FAILURES}; do
            [ "$name" = "$ig" ] && ignored=1 && break
        done
        [ "$ignored" -eq 0 ] && KVMUT_REAL_FAIL=$((KVMUT_REAL_FAIL + 1))
    done < <(grep -E '^FAIL ' "$clean" || true)

    rm -f "$clean"
    echo "kvm-unit-tests summary: PASS=${KVMUT_PASS} FAIL=${KVMUT_FAIL} SKIP=${KVMUT_SKIP} (real failures after ignore list: ${KVMUT_REAL_FAIL})"
}
