# Authors: Norbert Manthey <nmanthey@amazon.de>
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Common library for the pgbench PostgreSQL kernel A/B benchmark.
#
# The kernel-management half (install first/last kernel from the shared
# kernel-rpms area, reboot between stages, assert the running kernel changed)
# is reused from the unixbench-kernel-regression test so behaviour stays
# consistent across the benchmark suites. The PostgreSQL specifics are in this
# file. Results are written as the same benchmark-*.csv the pipeline's benchmark
# analyzer supports.

# ---------------------------------------------------------------------------
# Configuration (overridable via environment)
# ---------------------------------------------------------------------------
PGBENCH_DURATION="${PGBENCH_DURATION:-240}"
PGBENCH_SCALING_FACTOR="${PGBENCH_SCALING_FACTOR:-100}"

PGDATA="/tmp/pgdata"
PGPORT=5432
PGDATABASE="pgbench"
export PGDATA PGPORT

NUM_CPUS=$(nproc)
HALF_CPUS=$((NUM_CPUS / 2))
# Single VM with CPU pinning: PostgreSQL on the first half of the cores,
# pgbench client on the second half, to reduce client/server interference.
SERVER_CPUS="0-$((HALF_CPUS - 1))"
CLIENT_CPUS="${HALF_CPUS}-$((NUM_CPUS - 1))"

# ---------------------------------------------------------------------------
# Kernel management (shared across kernel A/B tests)
# ---------------------------------------------------------------------------
# Sets RESULTS_BUCKET/ARCH/KERNEL_RPM_DIR/KERNEL_FILE, validates the pipeline
# environment, and defines the kernel install/reboot helpers. SOURCE_DIR must
# already be set by the run script before this file is sourced.
source "${SOURCE_DIR}/kernel_helpers.sh"

# ---------------------------------------------------------------------------
# PostgreSQL / pgbench (test-specific)
# ---------------------------------------------------------------------------
run_as_postgres()
{
    if [ "$(id -u)" -eq 0 ]; then
        sudo -u postgres "$@"
    else
        "$@"
    fi
}

# Shared-buffer size: 25% of RAM, capped at 4GB, floored at 128MB.
get_shared_buffer_size()
{
    local mem_kb buffer_mb
    mem_kb=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
    buffer_mb=$((mem_kb / 1024 / 4))
    [ "$buffer_mb" -gt 4096 ] && buffer_mb=4096
    [ "$buffer_mb" -lt 128 ] && buffer_mb=128
    echo "$buffer_mb"
}

setup_postgresql()
{
    echo "Initializing PostgreSQL database cluster..."
    rm -rf "$PGDATA"
    mkdir -p "$PGDATA"

    if [ "$(id -u)" -eq 0 ]; then
        id postgres &>/dev/null || sudo useradd -r postgres
        sudo chown -R postgres:postgres "$PGDATA"
    fi

    run_as_postgres /usr/bin/initdb -D "$PGDATA" --encoding=SQL_ASCII --locale=C

    local shared_buffers max_connections
    shared_buffers=$(get_shared_buffer_size)
    max_connections=$((NUM_CPUS * 4 + 100))

    cat >>"$PGDATA/postgresql.conf" <<EOF
listen_addresses = 'localhost'
port = $PGPORT
max_connections = $max_connections
shared_buffers = ${shared_buffers}MB
work_mem = 64MB
maintenance_work_mem = 256MB
synchronous_commit = off
wal_level = minimal
max_wal_senders = 0
fsync = off
full_page_writes = off
logging_collector = off
unix_socket_directories = '$PGDATA'
dynamic_shared_memory_type = sysv
max_parallel_workers_per_gather = 0
EOF

    cat >"$PGDATA/pg_hba.conf" <<EOF
local   all             all                                     trust
host    all             all             127.0.0.1/32            trust
host    all             all             ::1/128                 trust
EOF

    [ "$(id -u)" -eq 0 ] && sudo chown -R postgres:postgres "$PGDATA"

    echo "Starting PostgreSQL (pinned to CPUs $SERVER_CPUS)..."
    run_as_postgres taskset -c "$SERVER_CPUS" /usr/bin/postgres -D "$PGDATA" >>"$PGDATA/logfile" 2>&1 &

    local i
    for i in {1..30}; do
        sleep 1
        /usr/bin/pg_isready -h localhost -p "$PGPORT" && break
    done
    if ! /usr/bin/pg_isready -h localhost -p "$PGPORT"; then
        echo "ERROR: PostgreSQL failed to start:" >&2
        cat "$PGDATA/logfile" >&2
        return 1
    fi

    run_as_postgres /usr/bin/createdb -h localhost -p "$PGPORT" "$PGDATABASE"
    echo "PostgreSQL started"
}

init_pgbench()
{
    local scaling_factor="${1:-$PGBENCH_SCALING_FACTOR}"
    echo "Initializing pgbench tables (scaling factor: $scaling_factor)..."
    run_as_postgres /usr/bin/pgbench -h localhost -p "$PGPORT" -i -s "$scaling_factor" "$PGDATABASE"
}

# Run one pgbench mode (readonly|readwrite) into output_file.
run_pgbench()
{
    local mode="$1"
    local output_file="$2"
    local duration="${3:-$PGBENCH_DURATION}"

    local clients threads mode_flag=""
    [ "$mode" = "readonly" ] && mode_flag="-S"
    clients=$((HALF_CPUS * 2))
    threads=$HALF_CPUS

    echo "Running pgbench $mode (clients=$clients, threads=$threads, duration=${duration}s)"
    run_as_postgres taskset -c "$CLIENT_CPUS" /usr/bin/pgbench \
        -h localhost -p "$PGPORT" --protocol=prepared \
        -c "$clients" -j "$threads" -T "$duration" -r $mode_flag \
        "$PGDATABASE" >"$output_file" 2>&1
}

stop_postgresql()
{
    echo "Stopping PostgreSQL..."
    run_as_postgres /usr/bin/pg_ctl -D "$PGDATA" stop -m fast 2>/dev/null || true
}

# Set up PostgreSQL, run the read-only and read-write benchmarks into
# results_dir, then stop PostgreSQL. Requires at least 4 CPUs.
run_pgbench_suite()
{
    local results_dir="$1"
    if [ "$(nproc)" -lt 4 ]; then
        echo "ERROR: pgbench benchmark requires at least 4 CPUs" >&2
        return 1
    fi
    mkdir -p "$results_dir"

    setup_postgresql
    init_pgbench "$PGBENCH_SCALING_FACTOR"

    local mode output
    for mode in readonly readwrite; do
        echo "=== Running $mode benchmark ==="
        output="$results_dir/pgbench_${mode}.txt"
        run_pgbench "$mode" "$output"
        cat "$output"
    done

    stop_postgresql
}

# Parse pgbench read-only and read-write output into a benchmark CSV that the
# pipeline's benchmark analyzer consumes (same schema as the unixbench test):
#   metric,unit,value,more_is_better,kernel_version,instance_id,instance_type,arch
summarize_pgbench_output()
{
    local readonly_file="$1"
    local readwrite_file="$2"
    local output_csv_file="$3"

    local kernel_version instance_id instance_type arch
    kernel_version=$(uname -r)
    instance_id=$(ec2-metadata --instance-id 2>/dev/null | cut -d" " -f2 || hostname || echo "unknown")
    instance_type=$(ec2-metadata --instance-type 2>/dev/null | cut -d" " -f2 || echo "unknown")
    arch=$(uname -m)

    echo "metric,unit,value,more_is_better,kernel_version,instance_id,instance_type,arch" >"$output_csv_file"

    local mode file tps latency
    for mode in readonly readwrite; do
        [ "$mode" = "readonly" ] && file="$readonly_file" || file="$readwrite_file"
        [ -f "$file" ] || { echo "WARNING: $file not found, skipping $mode" >&2; continue; }

        # "tps = NNN (without initial connection time)" / "(excluding connections establishing)"
        tps=$(grep "tps = " "$file" | grep -E "(excluding|without)" | awk '{print $3}' | head -1 || true)
        # "latency average = NNN ms"
        latency=$(grep "latency average" "$file" | awk '{print $4}' | head -1 || true)

        [ -n "$tps" ] && \
            echo "postgresql.${mode}.tps,TPS,${tps},true,${kernel_version},${instance_id},${instance_type},${arch}" >>"$output_csv_file"
        [ -n "$latency" ] && \
            echo "postgresql.${mode}.latency_avg,ms,${latency},false,${kernel_version},${instance_id},${instance_type},${arch}" >>"$output_csv_file"
    done
}
