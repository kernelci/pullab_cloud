# Authors: Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Shared kernel-management helpers for vm-tests that install and boot a kernel
# RPM from the pipeline's shared kernel-rpms area.
#
# Sourced by a test's common_lib.sh (which sets SOURCE_DIR first). It packages
# into each test payload via a symlink `kernel_helpers.sh -> ../lib/kernel_helpers.sh`
# in the test directory; the zip step stores the symlink target's content as a
# real file, so on the VM this is a normal file in the flat test dir.
#
# Fix once, benefit everywhere: the underscore/dash RPM-version handling and the
# FIPS-disable-before-reboot logic live here, so all kernel tests share them.

# ---------------------------------------------------------------------------
# Results bucket and kernel paths from the pipeline environment.
RESULTS_BUCKET="${S3_BUCKET:-}"
ARCH=$(uname -m)
KERNEL_RPM_DIR="/tmp/kernel-rpms"
KERNEL_FILE="${SOURCE_DIR}/kernel_version_before.txt"

# Validate required environment variables
if [ -z "$RESULTS_BUCKET" ] || [ -z "$RUN_PREFIX" ] || [ -z "$TEST_NAME" ]; then
    echo "ERROR: Missing required environment variables (S3_BUCKET, RUN_PREFIX, TEST_NAME)" >&2
    exit 1
fi

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
    S3_PATH="s3://${RESULTS_BUCKET}/${RUN_PREFIX}/shared/kernel-rpms/binary/${ARCH}/"
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
    S3_PATH="s3://${RESULTS_BUCKET}/${RUN_PREFIX}/shared/kernel-rpms/binary/${ARCH}/"
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

# Error trap handler to show line where error occurred
error_trap()
{
    local exit_code=$?
    local line_number=$1
    echo "$(date): ERROR: Script failed at line $line_number with exit code $exit_code"
    echo "$(date): ERROR: Command that failed: $(sed -n "${line_number}p" "$0")"
    exit $exit_code
}
trap 'error_trap $LINENO' ERR

# Install a single given package
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

# Install all dependencies for this test
install_test_dependencies()
{
    local deps_file="${SOURCE_DIR}/dependencies.txt"
    if [ -f "$deps_file" ]; then
        while IFS= read -r pkg || [ -n "$pkg" ]; do
            [[ -z "$pkg" || "$pkg" =~ ^[[:space:]]*# ]] && continue
            pkg=$(echo "$pkg" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
            [ -n "$pkg" ] && { install_package "$pkg" || return 1; }
        done <"$deps_file"
    else
        echo "ERROR: dependencies.txt not found" >&2
        return 1
    fi
}

# List available kernels from S3, dump boot info, install a kernel RPM and make
# it the default boot target.
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
    echo "--- grubby default ---"
    sudo grubby --default-kernel 2>/dev/null || echo "(grubby --default-kernel failed)"
    echo "=== End Boot Debug Info ==="
}

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

    # Install the kernel RPM. On an AMI whose default kernel is a different
    # series (e.g. a 6.18 AMI when installing a 6.1 kernel), the distro
    # kernel<N>-tools package declares "conflicts with kernel-uname-r < <N>",
    # so a plain install is refused. Fall back to --allowerasing, which
    # removes the conflicting tools package and installs the requested kernel
    # (both vmlinuz files remain in /boot, so the target kernel can be booted).
    if sudo dnf install -y "$kernel_rpm" 2>/dev/null \
        || sudo yum localinstall -y "$kernel_rpm" 2>/dev/null \
        || sudo dnf install -y --allowerasing "$kernel_rpm" 2>/dev/null; then
        dump_boot_info
        local installed_version
        installed_version=$(rpm -qp --queryformat '%{VERSION}' "$kernel_rpm" 2>/dev/null)
        # RPM VERSION may use underscores (e.g. 6.18.41_nogup) while the kernel
        # LOCALVERSION uses dashes (vmlinuz-6.18.41-nogup). Try both variants.
        local installed_version_alt="${installed_version//_/-}"

        local grub_kernel
        grub_kernel=$(sudo grubby --info=ALL 2>/dev/null \
            | grep "^kernel=" \
            | grep -E "$installed_version|$installed_version_alt" \
            | head -1 \
            | sed 's/^kernel=//' \
            | tr -d '"' \
            || true)

        if [ -z "$grub_kernel" ]; then
            local vmlinuz
            vmlinuz=$(ls /boot/vmlinuz-*"$installed_version"* 2>/dev/null | head -1)
            if [ -z "$vmlinuz" ] && [ "$installed_version_alt" != "$installed_version" ]; then
                vmlinuz=$(ls /boot/vmlinuz-*"$installed_version_alt"* 2>/dev/null | head -1)
            fi
            if [ -n "$vmlinuz" ]; then
                echo "Adding grubby entry for $vmlinuz"
                # Derive the kernel version from the vmlinuz filename
                local kver="${vmlinuz#/boot/vmlinuz-}"
                local initrd="/boot/initramfs-${kver}.img"
                if [ ! -f "$initrd" ]; then
                    echo "Generating initramfs at $initrd for kernel $kver"
                    sudo dracut --force "$initrd" "$kver" 2>/dev/null \
                        || sudo mkinitrd "$initrd" "$kver" 2>/dev/null \
                        || true
                fi
                if [ -f "$initrd" ]; then
                    sudo grubby --add-kernel="$vmlinuz" \
                        --initrd="$initrd" \
                        --title="Linux $kver" \
                        --args="fips=0" \
                        --copy-default \
                        --make-default
                    echo "Added and set default: $vmlinuz"
                else
                    echo "WARNING: No initramfs for $kver, trying set-default anyway"
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

        # Disable FIPS mode system-wide before rebooting into a custom kernel.
        # Some AL2023 enable FIPS; unsigned modules (from make binrpm-pkg)
        # fail signature verification and cause a kernel panic.
        if command -v fips-mode-setup &>/dev/null; then
            echo "Disabling FIPS mode for custom kernel boot"
            sudo fips-mode-setup --disable 2>/dev/null || true
        fi

        return 0
    else
        echo "ERROR: Failed to install new kernel" >&2
        return 1
    fi
}

get_first_kernel_rpm_from_dir()
{
    local kernels=$(list_kernels_from_s3 | sort -V)
    local first_kernel=$(echo "$kernels" | head -n 1)
    [ -z "$first_kernel" ] && return 1
    download_kernel_rpm "$first_kernel"
}

get_last_kernel_rpm_from_dir()
{
    local kernels=$(list_kernels_from_s3 | sort -V)
    local last_kernel=$(echo "$kernels" | tail -n 1)
    [ -z "$last_kernel" ] && return 1
    download_kernel_rpm "$last_kernel"
}

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
