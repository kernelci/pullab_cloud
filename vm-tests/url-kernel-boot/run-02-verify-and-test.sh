#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sudo -E python3 "$SOURCE_DIR/url_kernel.py" test --state "$SOURCE_DIR/kernel-state.json"
