#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# These tools operate on the supplied kernel and modules, not the AMI kernel.
sudo dnf install -y dracut grubby kmod python3 python3-pyyaml util-linux
sudo -E python3 "$SOURCE_DIR/url_kernel.py" install --state "$SOURCE_DIR/kernel-state.json"
# The VM client returns SSM's reboot code 194 between these two run scripts.
