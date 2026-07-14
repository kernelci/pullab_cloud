#!/bin/bash

# Authors: Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Build QEMU from source on AL2023 (no QEMU in default repos).
# Builds a minimal x86_64-softmmu target — enough for nested KVM testing.
set -e
exec 2>&1

QEMU_VERSION="${QEMU_VERSION:-9.2.0}"
# Expected sha256 of qemu-${QEMU_VERSION}.tar.xz. download.qemu.org publishes
# only GPG signatures, so the digest is pinned here. This value matches the
# default 9.2.0 tarball; override QEMU_SHA256 when bumping QEMU_VERSION, or set
# it to empty to skip the integrity check.
QEMU_SHA256="${QEMU_SHA256:-f859f0bc65e1f533d040bbe8c92bcfecee5af2c921a6687c652fb44d089bd894}"
QEMU_DIR="/opt/qemu"
NPROC=$(nproc)

# Pre-flight: disk space check
AVAIL_MB=$(df --output=avail -m /tmp | tail -1 | tr -d ' ')
if [ "$AVAIL_MB" -lt 3072 ]; then
    echo "ERROR: Need ≥3GB free in /tmp for QEMU build (have ${AVAIL_MB}MB)"
    exit 1
fi

if [ -x "$QEMU_DIR/bin/qemu-system-x86_64" ]; then
    echo "QEMU already built: $($QEMU_DIR/bin/qemu-system-x86_64 --version | head -1)"
    exit 0
fi

echo "=== Installing build dependencies ==="
dnf install -y gcc gcc-c++ make ninja-build python3 python3-pip \
    glib2-devel pixman-devel zlib-devel libfdt-devel \
    flex bison diffutils findutils tar gzip xz wget bzip2
# meson needs a TOML parser on Python <3.11 (AL2023 ships 3.9); tomllib is only
# in the 3.11+ stdlib. Prefer the distro package python3-tomli; fall back to pip
# only if it is not available.
dnf install -y python3-tomli 2>/dev/null || pip3 install tomli 2>/dev/null || true

# libslirp-devel may not be available — build from source if needed
if ! dnf install -y libslirp-devel 2>/dev/null; then
    echo "libslirp-devel not available, building from source..."
    dnf install -y meson git 2>/dev/null || pip3 install meson
    cd /tmp
    wget -q "https://gitlab.freedesktop.org/slirp/libslirp/-/archive/v4.7.0/libslirp-v4.7.0.tar.gz"
    tar xf libslirp-v4.7.0.tar.gz
    cd libslirp-v4.7.0
    meson setup build --prefix=/usr --default-library=both
    ninja -C build install
    ldconfig
    cd /tmp
    rm -rf libslirp-v4.7.0*
fi

echo "=== Downloading QEMU $QEMU_VERSION ==="
cd /tmp
wget -q "https://download.qemu.org/qemu-${QEMU_VERSION}.tar.xz"
if [ -n "$QEMU_SHA256" ]; then
    echo "Verifying sha256 of qemu-${QEMU_VERSION}.tar.xz ..."
    echo "${QEMU_SHA256}  qemu-${QEMU_VERSION}.tar.xz" | sha256sum -c - || {
        echo "ERROR: qemu-${QEMU_VERSION}.tar.xz sha256 mismatch (expected ${QEMU_SHA256})"
        exit 1
    }
else
    echo "WARNING: QEMU_SHA256 is empty -- skipping archive integrity check"
fi
tar xf "qemu-${QEMU_VERSION}.tar.xz"
cd "qemu-${QEMU_VERSION}"

echo "=== Configuring (x86_64-softmmu only) ==="
./configure \
    --prefix="$QEMU_DIR" \
    --target-list=x86_64-softmmu \
    --enable-kvm \
    --enable-slirp \
    --disable-docs \
    --disable-user \
    --disable-gtk \
    --disable-sdl \
    --disable-opengl \
    --disable-virglrenderer \
    --disable-xen \
    --disable-spice \
    --disable-vnc \
    --disable-curses

echo "=== Building (${NPROC} jobs) ==="
make -j"$NPROC"
make install

echo "=== Cleanup ==="
cd /
rm -rf /tmp/qemu-${QEMU_VERSION}*

echo "QEMU installed: $($QEMU_DIR/bin/qemu-system-x86_64 --version | head -1)"
