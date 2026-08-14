#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Script to run kernel-ci-cloud-labs in a Python virtual environment
# This script:
# 1. Creates a virtual environment if it doesn't exist
# 2. Installs the module and dependencies
# 3. Runs unit tests to verify installation
# 4. Executes any command passed as arguments (e.g., the pipeline)

set -e

# Configuration
# PYTHON selects the interpreter used to create the virtual environment.
# Override it to build the venv with a specific version, e.g.
#   PYTHON=python3.12 tests/test-in-venv.sh
# It may be a name on PATH or an absolute path. All pip/pytest calls go through
# "<python> -m ..." (never the bare pip/python3 shims).
PYTHON="${PYTHON:-python3}"
VENV_DIR=".venv-testing"
MODULE_DIR="$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")"
STATUS_CACHE="$MODULE_DIR/$VENV_DIR/.git_status_cache"
# Interpreter inside the venv (created from $PYTHON). Used for pip/pytest so the
# correct environment is targeted regardless of which binary bootstrapped it.
VENV_PYTHON="$MODULE_DIR/$VENV_DIR/bin/python"

# Function to create and setup virtual environment
setup_virtual_environment()
{
    echo "Setting up virtual environment with '${PYTHON}'..."
    "${PYTHON}" -m venv "${VENV_DIR}"
    source "${VENV_DIR}/bin/activate"
    "${VENV_PYTHON}" -m pip install --upgrade pip
    "${VENV_PYTHON}" -m pip install -e ".[dev]"
}

# Function to activate virtual environment
activate_virtual_environment()
{
    echo "Activating virtual environment..." 1>&2
    source "${VENV_DIR}/bin/activate"
}

# Function to install the module
install_module()
{
    echo "Installing module..." 1>&2
    status=0
    output=$("${VENV_PYTHON}" -m pip install -e "${MODULE_DIR}" 2>&1) || status=$?
    if [ $status -ne 0 ]; then
        echo "Installation failed, with output:" 1>&2
        echo "$output" 1>&2
        return 1
    fi
}

# Function to run tests
run_tests()
{
    echo "Running unit tests..." 1>&2
    status=0
    output=$("${VENV_PYTHON}" -m pytest tests/ -v -m "not integration" 2>&1) || status=$?
    if [ $status -eq 0 ]; then
        echo "Unit tests passed" 1>&2
    else
        echo "Unit tests failed:" 1>&2
        echo "$output" 1>&2
        return 1
    fi
}

# Check if repo state matches cached state to skip reinstall
check_cache_matches_repo()
{
    [ ! -r "$STATUS_CACHE" ] && return 1

    local cache_tag="$(git -C "$MODULE_DIR" describe --tags --always)"
    local -i repo_is_dirty=0
    unclean_state=$(git -C "$MODULE_DIR" status --porcelain 2>/dev/null) || repo_is_dirty=1
    [ -n "$unclean_state" ] && repo_is_dirty=1
    [ "$repo_is_dirty" -ne 0 ] && return 1

    cached_content="$(cat "$STATUS_CACHE")"
    [ "$cached_content" != "$cache_tag" ] && return 1
    return 0
}

update_cache_tag()
{
    mkdir -p "$(dirname "$STATUS_CACHE")"
    git -C "$MODULE_DIR" describe --tags --always >"$STATUS_CACHE"
}

# Main execution
main()
{
    pushd "${MODULE_DIR}" >/dev/null

    if [ ! -d "${VENV_DIR}" ]; then
        setup_virtual_environment
    else
        activate_virtual_environment
    fi

    if ! check_cache_matches_repo; then
        install_module

        if [ -z "${SKIP_VENV_TESTING}" ]; then
            run_tests
        fi

        echo "Setup complete!" 1>&2
    else
        echo "Skipping installation, using previous environment state" 1>&2
    fi

    update_cache_tag
    popd >/dev/null

    # Execute any command provided as arguments
    if [ $# -gt 0 ]; then
        echo "Running: $*" 1>&2
        "$@" || {
            echo "Command failed with exit code $?" 1>&2
            exit 1
        }
    fi
}

main "$@"
