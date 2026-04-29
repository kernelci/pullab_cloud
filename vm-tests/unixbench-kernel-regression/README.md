# Kernel Regression Test with UnixBench

Automated performance regression testing that compares two Linux kernel versions using UnixBench.

## Test Flow

1. **run-01-setup-kernel-A.sh**: Install dependencies, extract UnixBench, install first (lowest version) kernel
2. **run-02-run-unixbench-setup-kernel-B.sh**: Run UnixBench on first kernel, install second kernel
3. **run-03-run-second-unixbench.sh**: Run UnixBench on second kernel

## Requirements

- Two kernel RPMs uploaded to the external storage bucket via `kernel-ci-cloud-runner aws setup upload-rpms`
- UnixBench archive `byte-unixbench-6.0.0.tar.gz` (bundled in this directory)
- System packages: gcc, make, tar (installed automatically from `dependencies.txt`)

## Output

- `benchmark-base-*.csv`: Metrics for the first (base) kernel
- `benchmark-tip-*.csv`: Metrics for the second (tip) kernel

CSV columns: `metric,unit,value,more_is_better,kernel_version,instance_id,instance_type,arch`

The pipeline's benchmark analyzer automatically compares these CSVs and reports regressions.
