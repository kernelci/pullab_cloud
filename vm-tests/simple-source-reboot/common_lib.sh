# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Common library for the simple source-build kernel reboot test.
#
# Binary-kernel install/reboot logic (environment validation, kernel RPM
# download/selection, install_kernel_rpm, reboot helpers) lives in the shared
# vm-tests/lib/kernel_helpers.sh, included via the kernel_helpers.sh symlink in
# this directory. Only the source-RPM build helpers stay here. SOURCE_DIR is
# set by the run script before this file is sourced.

KERNEL_BENCH_DIR="kernel-bench"

source "${SOURCE_DIR}/kernel_helpers.sh"

# This test consumes source RPMs (*.src.rpm) staged under shared/kernel-rpms/src/,
# not the per-arch binary dir the shared helpers default to. Point the shared
# list/download helpers at the src subpath.
KERNEL_RPM_SUBPATH="src"

# ---------------------------------------------------------------------------
# Source-RPM build helpers (specific to this test)
# ---------------------------------------------------------------------------

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
