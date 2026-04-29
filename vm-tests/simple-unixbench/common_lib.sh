# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Lib with functions required in multiple test steps

UNIXBENCH_VERSION=byte-unixbench-6.0.0
UNIXBENCH_TAR_FILE="$UNIXBENCH_VERSION.tar.gz"
KERNEL_BENCH_DIR="kernel-bench"

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

#Return current kernel
get_running_kernel()
{
    uname -r
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

# Extract unixbench
prepare_unixbench()
{
    if [ ! -r "$UNIXBENCH_TAR_FILE" ]; then
        echo "ERROR: cannot read unixbench archive $UNIXBENCH_TAR_FILE" >&2
        exit 2
    fi
    tar xzf "$UNIXBENCH_TAR_FILE"
}

# Run unixbench speed fs tests
run_unixbench()
{
    local output_dir="$1"
    local UB_PARALLEL=${2:-$(nproc)}
    local UB_COUNT=${3:-1}

    export UB_RESULTDIR="$output_dir"/results
    mkdir -p "$UB_RESULTDIR"

    echo "Debug: Current directory: $(pwd)"
    echo "Debug: Looking for UnixBench directory:"
    find . -name "*UnixBench*" -type d
    echo "Debug: Contents of extracted directory:"
    ls -la ./"$UNIXBENCH_VERSION"/ || echo "Directory $UNIXBENCH_VERSION not found"

    # Change to UnixBench directory and run test
    pushd ./"$UNIXBENCH_VERSION"/UnixBench
    echo "Debug: Inside UnixBench directory: $(pwd)"
    echo "Debug: Contents:"
    ls -la

    ./Run -q -c $UB_PARALLEL -i $UB_COUNT speed fs 2>&1 | tee "$output_dir"/unixbench.log
    local run_exit_code=${PIPESTATUS[0]}

    popd

    return $run_exit_code
}

# Turn benchmark log into structured format
summarize_unixbench_log()
{
    local unixbench_log="$1"
    local output_csv_file="$2"

    # Get kernel version
    local kernel_version=$(uname -r)
    local instance_id=$(ec2-metadata --instance-id | cut -d" " -f2 || hostname || echo "unknown")
    local instance_type=$(ec2-metadata --instance-type | cut -d" " -f2 || echo "unknown")
    local arch=$(uname -m)

    # Write CSV header
    echo "metric,unit,value,more_is_better,kernel_version,instance_id,instance_type,arch" >"$output_csv_file"

    # Parse the benchmark results section
    awk -v kernel_version="$kernel_version" -v instance_id="$instance_id" -v instance_type="$instance_type" -v arch="$arch" -v benchmark_version="$UNIXBENCH_VERSION" '
    BEGIN { in_results = 0; in_index = 0 }

    # Start parsing when we hit the results section
    /^Arithmetic Test \(double\)/ { in_results = 1 }

    # Start parsing index section
    /^System Benchmarks Partial Index/ { in_results = 0; in_index = 1; next }

    # Stop parsing at the final score line
    /^System Benchmarks Index Score/ { in_index = 0 }

    # Parse result lines (first section) - use 6th last as value, 5th last as unit
    in_results && NF >= 6 {
        # Extract metric name (everything except last 5 fields)
        metric = ""
        for (i = 1; i <= NF-4; i++) {
            if (metric == "") {
                metric = $i
            } else {
                metric = metric "_" $i
            }
        }

        # Use 6th last field as value, 5th last as unit
        value = $(NF-5)
        unit = $(NF-4)

        # Clean up metric name
        gsub(/^\s+|\s+$/, "", metric)

        # Determine more_is_better
        if (metric ~ /System_Call_Overhead/) {
            more_is_better = "false"
        } else {
            more_is_better = "true"
        }

        printf "%s.%s,%s,%s,%s,%s,%s,%s,%s\n", benchmark_version, metric, unit, value, more_is_better, kernel_version, instance_id, instance_type, arch
    }

    # Parse index section lines - always use second-to-last column as value
    in_index && NF >= 3 && !/BASELINE/ && !/RESULT/ && !/INDEX/ && !/^=/ {
        # Extract metric name (everything except last 2 fields)
        metric = ""
        for (i = 1; i <= NF-3; i++) {
            if (metric == "") {
                metric = $i
            } else {
                metric = metric "_" $i
            }
        }

        # Use second-to-last field as value
        value = $(NF-1)

        # Skip lines with "---" values
        if (value == "---") {
            next
        }

        # Clean up metric name
        gsub(/^\s+|\s+$/, "", metric)

        unit = "score"
        more_is_better = "true"

        printf "%s.%s,%s,%s,%s,%s,%s,%s,%s\n", benchmark_version, metric, unit, value, more_is_better, kernel_version, instance_id, instance_type, arch
    }
    ' "$unixbench_log" >>"$output_csv_file"
}
