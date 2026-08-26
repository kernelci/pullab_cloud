# PostgreSQL pgbench Kernel Regression Test

Kernel A/B performance regression test using PostgreSQL's `pgbench`. It installs
two kernels in turn on a **single VM** and runs the same read-only and
read-write `pgbench` workload against each, so the pipeline's benchmark analyzer
can flag database-performance regressions between kernel versions. It follows
the same three-stage pattern as `unixbench-kernel-regression`.

- Dependencies are installed from `dependencies.txt` at run time (not from test
  metadata).
- Results are emitted as `benchmark-*.csv` in the schema the pipeline already
  supports

## Test Flow

1. **run-01-setup-kernel-A.sh** — install PostgreSQL packages, record the running
   kernel, install the first (lowest-version) kernel RPM from the shared
   `kernel-rpms` area, then reboot.
2. **run-02-run-pgbench-setup-kernel-B.sh** — confirm the kernel changed, run
   `pgbench` (read-only + read-write) on the base kernel, then install the second
   (highest-version) kernel and reboot.
3. **run-03-run-second-pgbench.sh** — confirm the kernel changed, run `pgbench`
   on the tip kernel.

Single VM, CPU-pinned: PostgreSQL is pinned to the first half of the cores and
the `pgbench` client to the second half, to reduce client/server interference.

## Output

- `benchmark-base-<kernel>.csv` — metrics for the first (base) kernel.
- `benchmark-tip-<kernel>.csv` — metrics for the second (tip) kernel.

CSV columns: `metric,unit,value,more_is_better,kernel_version,instance_id,instance_type,arch`

Metrics captured (per kernel):
- `postgresql.readonly.tps` / `postgresql.readwrite.tps`: transactions/sec (more is better).
- `postgresql.readonly.latency_avg` / `postgresql.readwrite.latency_avg`: average latency in ms (less is better).

The pipeline's benchmark analyzer compares the base and tip CSVs and reports
regressions.

## Requirements

- x86_64 or aarch64 instance with at least 4 vCPUs (CPU pinning splits
  server/client across the two halves); `c8i.4xlarge` recommended, `us-west-2`
  preferred to raise the chance of landing on the same hardware for both kernels.
- Two kernel RPMs uploaded to the shared kernel-rpms area
  (`external_requirements.json` sets `kernel-rpms/binary: true`). The lowest
  version becomes the base, the highest becomes the tip.
- System packages from `dependencies.txt` (`postgresql16-server`,
  `postgresql16-contrib`, `postgresql16`) are installed in run-01.

## Configuration (environment overrides)

| Variable | Default | Purpose |
|---|---|---|
| `PGBENCH_DURATION` | `240` | Duration in seconds of each pgbench run. |
| `PGBENCH_SCALING_FACTOR` | `100` | Database size multiplier passed to `pgbench -i -s`. |
