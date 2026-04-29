#!/bin/bash

# Authors: Max Hubmann <mxhbm@amazon.de>, Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

#
# Script to control execution in the VM
# Improved watchdog logic for SSM exit code 194 compatibility
set -ex

# Check if we're already running as the target user
if [ "$EUID" -eq 0 ] && [ -z "$REEXEC_AS_USER" ]; then
    # We're running as root and haven't re-executed yet

    # Identify and set up home directory
    if [ -d "/home/ec2-user" ]; then
        export HOME="/home/ec2-user"
        export USER="ec2-user"
    elif [ -d "/home/ubuntu" ]; then
        export HOME="/home/ubuntu"
        export USER="ubuntu"
    else
        # Use first alphabetically sorted directory in /home
        first_home=$(ls -1 /home 2>/dev/null | head -n1)
        if [ -n "$first_home" ]; then
            export HOME="/home/$first_home"
            export USER="$first_home"
        else
            # Fallback if no home directories exist
            export HOME="/tmp"
            export USER="root"
        fi
    fi

    # Re-execute this script as the target user
    if [ "$USER" != "root" ]; then
        echo "Re-executing script as user: $USER"
        export REEXEC_AS_USER=1
        exec sudo -u "$USER" -E "$0" "$@"
    fi
fi

# Now we're running as the target user
cd "$HOME"

# Error trap handler to show line where error occurred
client_error_trap()
{
    local exit_code=$?
    local line_number=$1
    echo "$(date): ERROR: Script failed at line $line_number with exit code $exit_code"
    echo "$(date): ERROR: Command that failed: $(sed -n "${line_number}p" "$0")"

    # Clean up watchdog
    cleanup_watchdog

    exit $exit_code
}
trap 'client_error_trap $LINENO' ERR

# Check arguments
if [ $# -lt 4 ]; then
    echo "Usage: $0 <bucket-name> <run-prefix> <test-name> [timeout-seconds]"
    exit 1
fi

BUCKET_NAME="$1"
RUN_PREFIX="$2"
TEST_NAME="$3"
SAFETY_TIMEOUT=${4:-1800} # Default 30 minutes if not provided

# Build S3 path prefix: run_{test_id}_{datetime}/test_{test_name}/
S3_PREFIX="${RUN_PREFIX}/test_${TEST_NAME}"

# Create working directory in current directory (persistent across reboots)
WORK_DIR="$PWD/test-${RUN_PREFIX}-work"
mkdir -p "$WORK_DIR"
ORIGINAL_CWD=$(pwd)

WATCHDOG_SCRIPT="$WORK_DIR/watchdog_runner.sh"
WATCHDOG_PIDFILE="$WORK_DIR/watchdog.pid"
WATCHDOG_ACTIVE_FLAG="$WORK_DIR/watchdog_active"

# Function to create and start the watchdog
start_watchdog()
{
    local timeout=$1

    # Create the watchdog script as a separate file
    cat >"$WATCHDOG_SCRIPT" <<'WATCHDOG_SCRIPT_EOF'
#!/bin/bash
# Watchdog script - runs independently and can be cleanly terminated
TIMEOUT=$1
PIDFILE=$2
ACTIVE_FLAG=$3

# Write our PID to the pidfile
echo $$ > "$PIDFILE"

# Create active flag
touch "$ACTIVE_FLAG"

# Trap to clean up on termination
cleanup_on_exit() {
    rm -f "$ACTIVE_FLAG" 2>/dev/null
    rm -f "$PIDFILE" 2>/dev/null
    exit 0
}
trap cleanup_on_exit SIGTERM SIGINT EXIT

# Sleep in small increments so we can respond to signals quickly
elapsed=0
while [ $elapsed -lt $TIMEOUT ]; do
    # Check if we should still be running
    if [ ! -f "$ACTIVE_FLAG" ]; then
        echo "Watchdog: Active flag removed, exiting gracefully"
        exit 0
    fi

    sleep 5
    elapsed=$((elapsed + 5))
done

# Timeout reached - initiate shutdown
echo "Watchdog: Timeout of ${TIMEOUT}s reached, initiating shutdown"
sudo shutdown -h now
WATCHDOG_SCRIPT_EOF

    chmod +x "$WATCHDOG_SCRIPT"

    # Start the watchdog in background with nohup to detach it
    nohup "$WATCHDOG_SCRIPT" "$timeout" "$WATCHDOG_PIDFILE" "$WATCHDOG_ACTIVE_FLAG" \
        >"$WORK_DIR/watchdog.log" 2>&1 &

    # Wait briefly for PID file to be created
    sleep 1

    if [ -f "$WATCHDOG_PIDFILE" ]; then
        local wpid=$(cat "$WATCHDOG_PIDFILE")
        echo "$(date): Started watchdog with PID $wpid (timeout: ${timeout}s)"
    else
        echo "$(date): WARNING: Watchdog PID file not created, watchdog may not have started"
    fi
}

# Function to cleanly stop the watchdog
cleanup_watchdog()
{
    echo "$(date): Stopping watchdog..."

    # Remove the active flag first - this signals the watchdog to exit gracefully
    rm -f "$WATCHDOG_ACTIVE_FLAG" 2>/dev/null

    # Give it a moment to notice
    sleep 1

    # If PID file exists, send termination signal
    if [ -f "$WATCHDOG_PIDFILE" ]; then
        local wpid=$(cat "$WATCHDOG_PIDFILE" 2>/dev/null)
        if [ -n "$wpid" ]; then
            # Check if process is still running
            if kill -0 "$wpid" 2>/dev/null; then
                echo "$(date): Sending SIGTERM to watchdog PID $wpid"
                kill -TERM "$wpid" 2>/dev/null || true

                # Wait for it to terminate (up to 5 seconds)
                local wait_count=0
                while kill -0 "$wpid" 2>/dev/null && [ $wait_count -lt 10 ]; do
                    sleep 0.5
                    wait_count=$((wait_count + 1))
                done

                # Force kill if still running
                if kill -0 "$wpid" 2>/dev/null; then
                    echo "$(date): Force killing watchdog PID $wpid"
                    kill -KILL "$wpid" 2>/dev/null || true
                fi
            fi
        fi
        rm -f "$WATCHDOG_PIDFILE" 2>/dev/null
    fi

    # Clean up the watchdog script
    rm -f "$WATCHDOG_SCRIPT" 2>/dev/null

    echo "$(date): Watchdog stopped"
}

# Start the watchdog
start_watchdog "$SAFETY_TIMEOUT"

cleanup()
{
    cd "$ORIGINAL_CWD"
    # Don't remove work dir to allow persistence across reboots
}
trap cleanup EXIT

cd "$WORK_DIR"

# Get instance ID for organizing logs and state
INSTANCE_ID=$(ec2-metadata --instance-id | cut -d" " -f2)

# Initialize RUN_ID - track iteration of execution per instance
# Try to download existing run_id from S3 first (instance-specific)
RUN_ID_FILE="run_id.txt"
aws s3 cp --no-progress "s3://$BUCKET_NAME/${S3_PREFIX}/state/$INSTANCE_ID/run_id.txt" "$RUN_ID_FILE" 2>/dev/null || echo "0" >"$RUN_ID_FILE"

RUN_ID=$(cat "$RUN_ID_FILE")
RUN_ID=$((RUN_ID + 1))
echo "$RUN_ID" >"$RUN_ID_FILE"

# Upload updated run_id back to S3 (instance-specific)
aws s3 cp --no-progress "$RUN_ID_FILE" "s3://$BUCKET_NAME/${S3_PREFIX}/state/$INSTANCE_ID/run_id.txt"

# Create output directory for logs
LOG_DIR="$PWD/test-run-output"
mkdir -p "$LOG_DIR"

# Track cumulative runtime across all runs
CUMULATIVE_RUNTIME_FILE="$LOG_DIR/cumulative_runtime.txt"
if [ $RUN_ID -eq 1 ]; then
    echo "0" >"$CUMULATIVE_RUNTIME_FILE"
    OVERALL_START_TIME=$(date +%s)
    echo "$OVERALL_START_TIME" >"$LOG_DIR/overall_start_time.txt"
else
    OVERALL_START_TIME=$(cat "$LOG_DIR/overall_start_time.txt" 2>/dev/null || date +%s)
fi

# Setup logging with RUN_ID
LOG_FILE="$LOG_DIR/client-$RUN_ID.log"
exec 1> >(tee -a "$LOG_FILE")
exec 2> >(tee -a "$LOG_FILE" >&2)

echo "Run ID: $RUN_ID"
echo "$(date): [RUN_ID:$RUN_ID] Starting test execution for run: $RUN_PREFIX"
echo "$(date): [RUN_ID:$RUN_ID] Working directory: $WORK_DIR"

# Download and extract test payload only on first run
TEST_DIR="test"
mkdir -p "$TEST_DIR"
if [ $RUN_ID -eq 1 ]; then
    PAYLOAD_KEY="${S3_PREFIX}/input/${TEST_NAME}_test_payload.zip"
    echo "$(date): [RUN_ID:$RUN_ID] Downloading test payload from s3://$BUCKET_NAME/$PAYLOAD_KEY"
    if ! aws s3 cp --no-progress "s3://$BUCKET_NAME/$PAYLOAD_KEY" test-payload.zip; then
        echo "$(date): [RUN_ID:$RUN_ID] ERROR: Failed to download test payload"
        exit 1
    fi

    echo "$(date): [RUN_ID:$RUN_ID] Extracting test payload"
    if ! unzip -q test-payload.zip -d "$TEST_DIR"; then
        echo "$(date): [RUN_ID:$RUN_ID] ERROR: Failed to extract test payload"
        exit 1
    fi
else
    echo "$(date): [RUN_ID:$RUN_ID] Skipping download/extract - using existing files from previous run"
fi

# Ensure TEST_DIR exists before cd
if [ ! -d "$TEST_DIR" ]; then
    echo "$(date): [RUN_ID:$RUN_ID] ERROR: Test directory $TEST_DIR does not exist"
    exit 1
fi

cd "$TEST_DIR"

# Find and sort run scripts
RUN_SCRIPTS=($(ls run*.sh 2>/dev/null | sort -V))

if [ ${#RUN_SCRIPTS[@]} -eq 0 ]; then
    echo "$(date): [RUN_ID:$RUN_ID] ERROR: No run*.sh scripts found"
    exit 1
fi

echo "$(date): [RUN_ID:$RUN_ID] Found scripts: ${RUN_SCRIPTS[*]}"
TOTAL_SCRIPTS=${#RUN_SCRIPTS[@]}

# Check if this RUN_ID is valid
if [ $RUN_ID -gt $TOTAL_SCRIPTS ]; then
    echo "$(date): [RUN_ID:$RUN_ID] ERROR: RUN_ID $RUN_ID exceeds number of scripts $TOTAL_SCRIPTS"
    exit 1
fi

# Execute the script for this RUN_ID
SCRIPT_INDEX=$((RUN_ID - 1))
CURRENT_SCRIPT="${RUN_SCRIPTS[$SCRIPT_INDEX]}"

echo "$(date): [RUN_ID:$RUN_ID] Executing script $RUN_ID/$TOTAL_SCRIPTS: $CURRENT_SCRIPT"
chmod +x "$CURRENT_SCRIPT"

START_TIME=$(date +%s)
SCRIPT_OUTPUT_LOG="$LOG_DIR/run-$RUN_ID-output.log"

# Temporarily disable exit on error to capture exit code
set +e
bash -x ./"$CURRENT_SCRIPT" 2>&1 | tee "$SCRIPT_OUTPUT_LOG"
SCRIPT_EXIT_CODE=${PIPESTATUS[0]}
set -e

END_TIME=$(date +%s)
RUNTIME=$((END_TIME - START_TIME))

# Update cumulative runtime
CUMULATIVE_RUNTIME=$(cat "$CUMULATIVE_RUNTIME_FILE" 2>/dev/null || echo "0")
CUMULATIVE_RUNTIME=$((CUMULATIVE_RUNTIME + RUNTIME))
echo "$CUMULATIVE_RUNTIME" >"$CUMULATIVE_RUNTIME_FILE"

echo "$(date): [RUN_ID:$RUN_ID] Script '$CURRENT_SCRIPT' returned exit code: $SCRIPT_EXIT_CODE"

# Handle exit codes properly
if [ $SCRIPT_EXIT_CODE -eq 0 ]; then
    echo "$(date): [RUN_ID:$RUN_ID] Script completed successfully"
elif [ $SCRIPT_EXIT_CODE -eq 194 ]; then
    echo "$(date): [RUN_ID:$RUN_ID] Script requested reboot (exit code 194)"
else
    # Script failed with non-zero, non-reboot exit code
    if [ $SCRIPT_EXIT_CODE -gt 100 ]; then
        echo "$(date): [RUN_ID:$RUN_ID] WARNING: Script exit code $SCRIPT_EXIT_CODE > 100, capping at 100"
        SCRIPT_EXIT_CODE=100
    fi
    echo "$(date): [RUN_ID:$RUN_ID] Script failed with exit code $SCRIPT_EXIT_CODE"
fi

echo "$(date): [RUN_ID:$RUN_ID] Script runtime: ${RUNTIME}s"

cd ..

# Upload current iteration logs with instance ID
echo "$(date): [RUN_ID:$RUN_ID] Uploading logs for run $RUN_ID"
if ! aws s3 cp --no-progress "$LOG_FILE" "s3://$BUCKET_NAME/${S3_PREFIX}/output/$INSTANCE_ID/client-$RUN_ID.log"; then
    echo "$(date): [RUN_ID:$RUN_ID] WARNING: Failed to upload client log"
fi
if ! aws s3 cp --no-progress "$SCRIPT_OUTPUT_LOG" "s3://$BUCKET_NAME/${S3_PREFIX}/output/$INSTANCE_ID/run-$RUN_ID-output.log"; then
    echo "$(date): [RUN_ID:$RUN_ID] WARNING: Failed to upload run output log"
fi

# Check if script failed (not success and not reboot request)
if [ $SCRIPT_EXIT_CODE -ne 0 ] && [ $SCRIPT_EXIT_CODE -ne 194 ]; then
    echo "$(date): [RUN_ID:$RUN_ID] ERROR: Script failed - stopping execution chain"

    # Create failure status files
    RESULT="FAILED: Exit code $SCRIPT_EXIT_CODE at run $RUN_ID"

    CUMULATIVE_RUNTIME=$(cat "$CUMULATIVE_RUNTIME_FILE" 2>/dev/null || echo "$RUNTIME")

    cat >"$LOG_DIR/stats.json" <<EOF
{
  "run_prefix": "$RUN_PREFIX",
  "exit_code": $SCRIPT_EXIT_CODE,
  "runtime_seconds": $CUMULATIVE_RUNTIME,
  "last_run_runtime_seconds": $RUNTIME,
  "start_time": $OVERALL_START_TIME,
  "end_time": $END_TIME,
  "success": false,
  "total_runs": $TOTAL_SCRIPTS,
  "completed_runs": $((RUN_ID - 1)),
  "failed_at_run": $RUN_ID
}
EOF

    echo "$RESULT" >"$LOG_DIR/result.txt"

    # Upload failure status files
    aws s3 cp --no-progress "$LOG_DIR/result.txt" "s3://$BUCKET_NAME/${S3_PREFIX}/output/$INSTANCE_ID/result.txt" || true
    aws s3 cp --no-progress "$LOG_DIR/stats.json" "s3://$BUCKET_NAME/${S3_PREFIX}/output/$INSTANCE_ID/stats.json" || true

    echo "$(date): [RUN_ID:$RUN_ID] Test execution failed: $RESULT"

    # Clean up watchdog before exit
    cleanup_watchdog

    exit $SCRIPT_EXIT_CODE
fi

# Check if this is the last script
if [ $RUN_ID -eq $TOTAL_SCRIPTS ]; then
    echo "$(date): [RUN_ID:$RUN_ID] Final script completed. Creating final status files."

    # Determine overall result
    if [ $SCRIPT_EXIT_CODE -eq 0 ]; then
        RESULT="SUCCESS"
    else
        RESULT="FAILED: Exit code $SCRIPT_EXIT_CODE"
    fi

    CUMULATIVE_RUNTIME=$(cat "$CUMULATIVE_RUNTIME_FILE" 2>/dev/null || echo "$RUNTIME")

    # Create metadata
    cat >"$LOG_DIR/stats.json" <<EOF
{
  "run_prefix": "$RUN_PREFIX",
  "exit_code": $SCRIPT_EXIT_CODE,
  "runtime_seconds": $CUMULATIVE_RUNTIME,
  "last_run_runtime_seconds": $RUNTIME,
  "start_time": $OVERALL_START_TIME,
  "end_time": $END_TIME,
  "success": $([ $SCRIPT_EXIT_CODE -eq 0 ] && echo "true" || echo "false"),
  "total_runs": $TOTAL_SCRIPTS
}
EOF

    echo "$RESULT" >"$LOG_DIR/result.txt"

    # Upload final status files with instance ID
    if ! aws s3 cp --no-progress "$LOG_DIR/result.txt" "s3://$BUCKET_NAME/${S3_PREFIX}/output/$INSTANCE_ID/result.txt"; then
        echo "$(date): [RUN_ID:$RUN_ID] WARNING: Failed to upload result.txt"
    fi
    if ! aws s3 cp --no-progress "$LOG_DIR/stats.json" "s3://$BUCKET_NAME/${S3_PREFIX}/output/$INSTANCE_ID/stats.json"; then
        echo "$(date): [RUN_ID:$RUN_ID] WARNING: Failed to upload stats.json"
    fi

    # Upload all benchmark-*.csv files from the test directory
    for csv_file in "$TEST_DIR"/benchmark-*.csv; do
        if [ -f "$csv_file" ]; then
            csv_filename=$(basename "$csv_file")
            echo "$(date): [RUN_ID:$RUN_ID] Uploading benchmark file: $csv_filename"
            if ! aws s3 cp --no-progress "$csv_file" "s3://$BUCKET_NAME/${S3_PREFIX}/output/$INSTANCE_ID/$csv_filename"; then
                echo "$(date): [RUN_ID:$RUN_ID] WARNING: Failed to upload $csv_filename"
            fi
        fi
    done

    # Upload all results_* files from the test directory
    for results_file in "$TEST_DIR"/results_*; do
        if [ -f "$results_file" ]; then
            results_filename=$(basename "$results_file")
            echo "$(date): [RUN_ID:$RUN_ID] Uploading results file: $results_filename"
            if ! aws s3 cp --no-progress "$results_file" "s3://$BUCKET_NAME/${S3_PREFIX}/output/$INSTANCE_ID/$results_filename"; then
                echo "$(date): [RUN_ID:$RUN_ID] WARNING: Failed to upload $results_filename"
            fi
        fi
    done

    echo "$(date): [RUN_ID:$RUN_ID] Test execution completed: $RESULT"

    # Clean up watchdog before shutdown
    cleanup_watchdog

    # Brief pause for SSM to capture status
    sleep 2

    echo "$(date): [RUN_ID:$RUN_ID] Initiating shutdown"
    # This provides a fallback in case the script exits but instance doesn't terminate
    SHUTDOWN_DELAY=$((5))
    sudo shutdown +$(SHUTDOWN_DELAY) "Scheduled shutdown: Test completed. Shutting down in ${SHUTDOWN_DELAY} minutes to prevent orphaned instance."

    # Exit with the appropriate exit code
    # The shutdown is scheduled but script exits immediately with proper status
    exit $SCRIPT_EXIT_CODE

else
    # More scripts to run - need reboot via SSM exit code 194
    echo "$(date): [RUN_ID:$RUN_ID] Completed run $RUN_ID of $TOTAL_SCRIPTS"
    echo "$(date): [RUN_ID:$RUN_ID] Next run will be RUN_ID $((RUN_ID + 1))"

    echo "$(date): [RUN_ID:$RUN_ID] Cleaning up watchdog before SSM reboot"
    cleanup_watchdog

    # Ensure all filesystem writes are complete
    sync

    # Wait for watchdog cleanup to fully complete
    sleep 2

    # Verify watchdog is actually stopped
    if [ -f "$WATCHDOG_PIDFILE" ]; then
        echo "$(date): [RUN_ID:$RUN_ID] WARNING: Watchdog PID file still exists, forcing cleanup"
        local remaining_pid=$(cat "$WATCHDOG_PIDFILE" 2>/dev/null)
        if [ -n "$remaining_pid" ]; then
            kill -KILL "$remaining_pid" 2>/dev/null || true
        fi
        rm -f "$WATCHDOG_PIDFILE" 2>/dev/null
    fi

    echo "$(date): [RUN_ID:$RUN_ID] Exiting with code 194 to signal reboot needed"

    # Brief pause to allow SSM Agent to prepare for the exit code
    # This is important for the SSM Agent to properly intercept the exit code
    sleep 3

    # Exit with 194 to signal SSM that a reboot is needed
    # The SSM Agent will handle the actual reboot and restart this script
    exit 194
fi
