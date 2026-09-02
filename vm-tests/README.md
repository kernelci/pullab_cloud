# VM Tests

Test scripts executed on EC2 instances. Each directory is a self-contained test.
See the main [README](../README.md) for writing new tests, configuration, and the benchmark CSV format.

## Available Tests

| Test | Stages | Needs Kernel RPMs | Description |
|------|--------|-------------------|-------------|
| `basic-test` | 1 | no | Connectivity check, prints kernel version |
| `example-reboot-test` | 2 | no | Records kernel version, reboots, verifies persistence |
| `example-kernel-reboot-test` | 3 | yes | Installs two kernels with reboot between each |
| `simple-unixbench` | 1 | no | Runs UnixBench on the default kernel |
| `unixbench-kernel-regression` | 3 | yes | Installs two kernels, runs UnixBench on each, produces benchmark CSVs |
| `pgbench-kernel-regression` | 3 | yes | Installs two kernels, runs PostgreSQL pgbench (read-only + read-write) on each, produces benchmark CSVs |
| `simple-source-reboot` | 2 | yes | Installs kernel from source RPM, reboots, verifies |

## How Multi-Stage Tests Work

```
Pipeline → Fargate → SSM command → test-vm-client.sh → run-01.sh → reboot → run-02.sh → ...
                                          ↕
                                     S3 (run_id state)
```

1. `test-vm-client.sh` downloads the test payload zip and tracks a `run_id` counter in S3
2. On each boot, it increments `run_id` and executes the corresponding `run-*.sh` script
3. If more scripts remain, it exits with code **194** — SSM reboots the VM and re-runs the client
4. After the final script, it uploads `result.txt`, `stats.json`, and any `benchmark-*.csv` files, then shuts down

The working directory (`$HOME/test-<RUN_PREFIX>-work/test/`) persists across reboots, so scripts can share data via files.

## Test Directory Structure

```
my-test/
├── run-01-setup.sh                # Stage 1 (required, at least one run*.sh)
├── run-02-verify.sh               # Stage 2 (optional, executed after reboot)
├── external_requirements.json     # Required: declares needed artifacts from external storage
├── dependencies.txt               # Optional: system packages (one per line)
├── common_lib.sh                  # Optional: shared shell functions
└── README.md                      # Optional: test documentation
```

## S3 Output Structure

```
s3://{bucket}/{run_prefix}/test_{test_name}/output/{instance_id}/
├── result.txt              # SUCCESS or FAILED
├── stats.json              # Timing and run metadata
├── client-N.log            # Client script log per stage
├── run-N-output.log        # Test script output per stage (full shell trace)
└── benchmark-*.csv         # Benchmark results (if produced by test)
```

## Troubleshooting

- **Test fails immediately**: Check script is executable and uses `set -euxo pipefail`
- **Test times out**: Increase `max_runtime` in config. Each reboot adds ~2-3 minutes overhead.
- **State not persisting**: Write files to the working directory, not `/tmp`
- **SSM agent not ready**: Ensure AMI has SSM agent and IAM role includes `AmazonSSMManagedInstanceCore`
