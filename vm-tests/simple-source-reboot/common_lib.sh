# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

KERNEL_BENCH_DIR="kernel-bench"

# Get results bucket and test paths from environment
RESULTS_BUCKET="${S3_BUCKET:-}"
ARCH=$(uname -m)
KERNEL_RPM_DIR="/tmp/kernel-rpms"
KERNEL_FILE="${SOURCE_DIR}/kernel_version_before.txt"

# Validate required environment variables
if [ -z "$RESULTS_BUCKET" ] || [ -z "$RUN_PREFIX" ] || [ -z "$TEST_NAME" ]; then
    echo "ERROR: Missing required environment variables (S3_BUCKET, RUN_PREFIX, TEST_NAME)" >&2
    exit 1
fi

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

get_running_kernel()
{
    uname -r
}

save_kernel_version()
{
    local version="$1"
    local out_file="$2"

    if [ -z "$version" ] || [ -z "$out_file" ]; then
        echo "ERROR: save_kernel_version requires version and file"
        return 1
    fi

    echo "$version" >"$out_file"
}

load_kernel_version()
{
    local in_file="$1"

    if [ ! -f "$in_file" ]; then
        echo "ERROR: Kernel version file not found: $in_file"
        return 1
    fi

    cat "$in_file"
}

assert_kernel_changed()
{
    local before="$1"
    local after="$2"

    if [ "$before" = "$after" ]; then
        echo "✗ FAILED: Kernel version did not change (still $after)"
        return 1
    fi

    echo "✓ SUCCESS: Kernel version changed from $before to $after"
}

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
            # Skip empty lines and comments
            [[ -z "$pkg" || "$pkg" =~ ^[[:space:]]*# ]] && continue

            # Remove leading/trailing whitespace
            pkg=$(echo "$pkg" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

            # Install package if not empty
            if [ -n "$pkg" ]; then
                install_package "$pkg" || return 1
            fi
        done <"$deps_file"
    else
        # Fallback to hardcoded dependencies
        install_package gcc make tar || return 1
    fi
}

# Install kernel source RPM (extracts source code to ~/rpmbuild/)
install_source_kernel_rpm()
{
    if [ -z "${1:-}" ]; then
        echo "ERROR: install_source_kernel_rpm requires kernel_src_rpm parameter" >&2
        return 1
    fi
    local kernel_src_rpm="$1"

    # Check it's a source RPM
    if [[ ! "$kernel_src_rpm" =~ \.src\.rpm$ ]]; then
        echo "ERROR: Not a source RPM: $kernel_src_rpm" >&2
        return 1
    fi

    echo "Installing kernel source from $kernel_src_rpm"

    # Install source RPM (extracts to ~/rpmbuild/SOURCES and ~/rpmbuild/SPECS)
    # Suppress mockbuild user warnings (harmless - files owned by root instead)
    if rpm -ivh "$kernel_src_rpm" 2>&1 | grep -v "user mockbuild\|group mock"; then
        echo "✓ Kernel source installed to ~/rpmbuild/"

        # Auto-install build dependencies from spec file
        echo "Installing build dependencies..."
        if sudo yum-builddep -y ~/rpmbuild/SPECS/kernel.spec 2>/dev/null ||
            sudo dnf builddep -y ~/rpmbuild/SPECS/kernel.spec 2>/dev/null; then
            echo "✓ Build dependencies installed"
        else
            echo "WARNING: Could not auto-install build dependencies"
        fi

        return 0
    else
        echo "ERROR: Failed to install kernel source RPM" >&2
        return 1
    fi
}

# Build binary kernel RPM from source
build_kernel_rpm_src()
{
    echo "Building binary kernel RPM from source..."

    if [ ! -f ~/rpmbuild/SPECS/kernel.spec ]; then
        echo "ERROR: kernel.spec not found in ~/rpmbuild/SPECS/" >&2
        return 1
    fi

    cd ~/rpmbuild/SPECS

    if rpmbuild -bb kernel.spec --with baseonly --without debug --without debuginfo; then
        echo "✓ Kernel built successfully"
        return 0
    else
        echo "ERROR: Kernel build failed" >&2
        return 1
    fi
}

# Dump boot configuration for debugging kernel install issues
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
    echo "--- grubby --info=ALL ---"
    sudo grubby --info=ALL 2>/dev/null || echo "(grubby --info=ALL failed)"
    echo "=== End Boot Debug Info ==="
}

# Install binary kernel RPM
install_kernel_rpm()
{
    if [ -z "${1:-}" ]; then
        echo "ERROR: install_kernel_rpm requires kernel_rpm parameter" >&2
        return 1
    fi
    local kernel_rpm="$1"

    # Check it's a binary RPM (not source)
    if [[ "$kernel_rpm" =~ \.src\.rpm$ ]]; then
        echo "ERROR: This is a source RPM, not a binary RPM: $kernel_rpm" >&2
        return 1
    fi

    # Check architecture compatibility
    local host_arch=$(uname -m)
    local rpm_arch=$(rpm -qp --queryformat '%{ARCH}' "$kernel_rpm" 2>/dev/null)

    if [ "$rpm_arch" != "$host_arch" ]; then
        echo "ERROR: Architecture mismatch - Host: $host_arch, RPM: $rpm_arch" >&2
        return 1
    fi

    echo "Installing binary kernel from $kernel_rpm (arch: $rpm_arch)"

    if sudo yum localinstall -y "$kernel_rpm" 2>/dev/null || sudo dnf install -y "$kernel_rpm" 2>/dev/null; then
        dump_boot_info

        # Set the newly installed kernel as default boot target.
        # Without this, GRUB boots the newest kernel which may not be the one we just installed.
        local installed_version
        installed_version=$(rpm -qp --queryformat '%{VERSION}' "$kernel_rpm" 2>/dev/null)

        # Find the grubby entry matching the installed kernel version.
        # Use grep || true to avoid ERR trap when no match is found.
        local grub_kernel
        grub_kernel=$(sudo grubby --info=ALL 2>/dev/null \
            | grep "^kernel=" \
            | grep "$installed_version" \
            | head -1 \
            | sed 's/^kernel=//' \
            | tr -d '"' \
            || true)

        if [ -z "$grub_kernel" ]; then
            # Upstream make binrpm-pkg kernels don't register with grubby.
            # Find the vmlinuz file and add a boot entry manually.
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
                    echo "✓ Added and set default: $vmlinuz"
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
        echo "✓ Kernel installed successfully"
        return 0
    else
        echo "ERROR: Failed to install kernel" >&2
        return 1
    fi
}

##### GET SRC KERNEL FROM S3 AND DOWNLOAD TO LOCAL MACHINE!
# List available kernels from S3
list_kernels_from_s3()
{
    S3_PATH="s3://${RESULTS_BUCKET}/${RUN_PREFIX}/shared/kernel-rpms/src/"
    aws s3 ls "${S3_PATH}" | grep "\.rpm$" | awk '{print $4}'
}

# Download specific kernel RPM from S3
download_kernel_rpm()
{
    if [ -z "${1:-}" ]; then
        echo "ERROR: download_kernel_rpm requires kernel_name parameter" >&2
        return 1
    fi
    local kernel_name="$1"

    S3_PATH="s3://${RESULTS_BUCKET}/${RUN_PREFIX}/shared/kernel-rpms/src/"

    mkdir -p "$KERNEL_RPM_DIR"
    local local_path="${KERNEL_RPM_DIR}/${kernel_name}"

    # Download if not already present
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

# Return kernel RPM with lowest version (downloads from S3)
get_first_source_kernel_rpm_from_dir()
{
    local kernels=$(list_kernels_from_s3 | sort -V)
    local first_kernel=$(echo "$kernels" | head -n 1)

    if [ -z "$first_kernel" ]; then
        return 1
    fi

    download_kernel_rpm "$first_kernel"
}

install_and_build_kernel()
{
    local kernel_src_rpm="$1"

    if [ -z "$kernel_src_rpm" ]; then
        echo "ERROR: No kernel source RPM provided"
        return 1
    fi

    if [ ! -f "$kernel_src_rpm" ]; then
        echo "ERROR: Kernel source RPM not found: $kernel_src_rpm"
        return 1
    fi

    local kernel_before
    kernel_before=$(uname -r)

    echo "Kernel before installation: $kernel_before"
    echo "$kernel_before" >"${SOURCE_DIR}/kernel_version_before.txt"

    echo "Step 1: Installing kernel source: $(basename "$kernel_src_rpm")"
    install_source_kernel_rpm "$kernel_src_rpm" || return 1

    echo "Step 2: Building kernel..."
    build_kernel_rpm_src || return 1

    echo "Step 3: Installing built kernel..."
    local built_kernel
    built_kernel=$(ls -t ~/rpmbuild/RPMS/$(uname -m)/kernel-[0-9]*.rpm | head -1)

    if [ -z "$built_kernel" ]; then
        echo "ERROR: No built kernel RPM found"
        return 1
    fi

    install_kernel_rpm "$built_kernel" || return 1

    echo "Kernel installed from source: $kernel_src_rpm"
}
