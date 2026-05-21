# Kernel CI Cloud Labs

## Overview

Kernel CI Cloud Labs is an automated testing framework designed to validate Linux kernel builds across cloud infrastructure. The system orchestrates parallel kernel testing by providing scripts to run tests from a Fargate container on EC2 VMs:

- **Spawning multiple EC2 instances** to run different test suites simultaneously
- **Managing test execution** through containerized ECS Fargate tasks that coordinate VM operations
- **Supporting diverse test types** including kernel installation, reboots, performance benchmarks (UnixBench), and comprehensive test suites (LTP, kselftest)
- **Collecting and storing results** in S3 with detailed logs for each VM and test run

**Future Goal:** This pipeline is designed to integrate with the existing KernelCI testing architecture, receiving test triggers from KernelCI and pushing results back to the KernelCI Database (KCIDB) for centralized reporting and analysis.

This package provides the kernel-ci-cloud-runner python application as an entry point to configure and run kernel testing in AWS EC2 VMs.

## Installation

**Python 3.11 is required.** Older interpreters (including the `python3` shipped
by Amazon Linux 2023, which is 3.9) are not supported — `pip install -e .` will
refuse to run on them. On AL2023, install `python3.11 python3.11-pip
python3.11-devel`; see [AWS_INSTALL.md](AWS_INSTALL.md) for the full host setup.

Run the package in a virtual environment. We also provide the script "tests/test-in-venv.sh" to wrap these steps in en environment.

If you do not have the code already, get your copy (git URL to be defined).
```bash
# Clone the repository
git clone <repository-url>
cd kernel-ci-cloud-labs
```

Setup virtual environment:

```bash
# Create virtual environment (Python 3.11 required)
python3.11 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install package (runtime only - boto3)
python3.11 -m pip install -e .

# Optional: Install with dev dependencies (pytest, black, pylint, pre-commit, pytest-cov)
python3.11 -m pip install -e ".[dev]"

# Optional: Install with analysis dependencies (pandas, matplotlib, seaborn)
python3.11 -m pip install -e ".[analysis]"

# Recommended: Install everything (dev + analysis)
python3.11 -m pip install -e ".[dev,analysis]"

# Optional: Install pre-commit hooks (only if you plan to commit code)
pre-commit install
```

## Quick Start

Below steps guide you through the project to be able to trigger testing with the integration test.

### 1. Configure AWS Credentials

Use one of the following methods:

- **AWS default credentials** (recommended): Configure via `aws configure`, IAM role, or environment variables. If you already have working AWS CLI access, skip to Step 2.
- **Explicit credentials file**: Create `examples/aws/credentials.json`:
  ```json
  {
    "access_key_id": "YOUR_ACCESS_KEY_ID",
    "secret_access_key": "YOUR_SECRET_ACCESS_KEY"
  }
  ```

### 2. Configure the project

Customize resource names with your own prefix and region, using the default configuration file (used for integration testing)

```bash
kernel-ci-cloud-runner aws setup configure --prefix kernel-ci-$USER- --region us-west-2
```

This sets unique names for all AWS resources and avoids conflicts with other users. Run with `--dry-run` to preview changes. Use `--test-filter` to limit which tests are included (e.g., `--test-filter unixbench`).

With `--prefix kernel-ci-$USER-`, the following resources will be created:
- S3: `kernel-ci-$USER-results-<ACCOUNT_ID>` (test results), `kernel-ci-$USER-storage` (kernel RPMs)
- IAM: `kernel-ci-$USER-ecs-role`
- ECS: cluster `kernel-ci-$USER-cluster`, task `kernel-ci-$USER-task`
- ECR: `kernel-ci-$USER-ecr`
- CloudWatch: `/ecs/kernel-ci-$USER-task`, `/ec2/kernel-ci-$USER-vms`


Actually writing a configuration file for a given setup can be done with an explicit configuration:

```bash
kernel-ci-cloud-runner aws setup configure --prefix kernel-ci-$USER- --region us-west-2 --output my-config.config
```

### Validate setup (optional)

Before launching real VMs, run a pre-flight check of AWS permissions, IAM resources, the results bucket, and KernelCI/KCIDB tokens. The command is read-only by default — pass `--fix` to create the S3 bucket if it doesn't exist yet.

```bash
kernel-ci-cloud-runner aws setup validate \
  --bucket kernel-ci-$USER-results \
  --role kernel-ci-$USER-vm-role \
  --region us-west-2
```

What it checks:

| Check | What it does |
| --- | --- |
| `aws_credentials` | `sts:GetCallerIdentity` — prints account + principal ARN |
| `ec2_describe` | confirms `ec2:DescribeInstances` works |
| `ec2_console_output` | probes `ec2:GetConsoleOutput` (needed to capture kernel boot logs) |
| `ssm` | `ssm:DescribeInstanceInformation` — needed to drive the test client |
| `iam_role` / `instance_profile` | only when `--role` is given — verifies trust policy and attached managed policies |
| `s3_bucket` | `head_bucket`; with `--fix`, creates the bucket (region-aware) and enables Block Public Access |
| `kernelci_api_token` | `GET <api_base_uri>/whoami` with `Bearer` from `KERNELCI_API_TOKEN` or `UNIFIED_TOKEN` |
| `kcidb_jwt` | decodes the JWT payload (no signature verification) and reports `exp`, `iss`, `sub`; sources the token from `KCIDB_JWT`, `KCIDB_REST=https://<jwt>@host/path`, or `UNIFIED_TOKEN` |

Exits non-zero if any check fails. Useful when iterating on IAM policies, rotating tokens, or onboarding a new AWS account.

### 3. Run integration test to verify setup

The integration test uses only `basic-test` and `example-reboot-test` — no kernel RPMs needed. This is the fastest way to verify everything works. The test will fail if you do not provide your configuration.

```bash
pytest tests/integration/ -v -m integration
```

- Completes in ~2-5 minutes, spawns 2 EC2 VMs (1 x86_64 + 1 ARM64)
- Check status by pipeline log message: "VMs: 2/2 spawned, 2 successful, 0 failed, 0 missing"
- **Logs:** `tests/integration/logs/`

### 4. Upload kernel RPMs (required for kernel-install tests)

Tests that install custom kernels (`example-kernel-reboot-test`, `unixbench-kernel-regression`) need kernel RPMs in an S3 bucket. Tests like `basic-test`, `example-reboot-test`, and `simple-unixbench` do **not** need this step.

```bash
# Upload local RPMs to the external storage bucket
kernel-ci-cloud-runner aws setup upload-rpms \
  --bucket kernel-ci-$USER-storage \
  --local-rpms /path/to/rpms/x86_64/ /path/to/rpms/aarch64/
```

RPMs are classified by filename suffix (`.x86_64.rpm`, `.aarch64.rpm`, `.src.rpm`) and uploaded to:
```
s3://bucket-name/kernel-rpms/
├── src/              # *.src.rpm
└── binary/
    ├── x86_64/       # *.x86_64.rpm
    └── aarch64/      # *.aarch64.rpm
```

The bucket name must match `external_storage.bucket` in `examples/aws/config.json` (set automatically by `setup configure`). If this is your first run or you changed IAM policies, set `"force_recreate_roles": true` in `config.json` to apply the updated policies.

### 5. Run the Pipeline

Using the default configured configuration

```bash
kernel-ci-cloud-runner aws run
```

Using an explicit configuration file (recommended)
```
# Or with a custom config file
kernel-ci-cloud-runner aws run --config my-config.json
```

- Check status by pipeline log message: "VMs: X/X spawned, Y successful, 0 failed, 0 missing"
- **Logs:** `logs/`

### 6. Check results

Open AWS console → S3 → bucket `kernel-ci-$USER-results`, or use the CLI:

```bash
# List all test runs
aws s3 ls s3://kernel-ci-$USER-results/ --region us-west-2

# List output files for a specific run and test
aws s3 ls s3://kernel-ci-$USER-results/run_<TEST_ID>_<DATETIME>/test_<TEST_NAME>/output/ \
  --recursive --region us-west-2

# Download a test result
aws s3 cp s3://kernel-ci-$USER-results/run_<TEST_ID>_<DATETIME>/test_<TEST_NAME>/output/<INSTANCE_ID>/result.txt - \
  --region us-west-2
```

### 7. Clean up resources

List all AWS resources created by the pipeline
```bash
kernel-ci-cloud-runner aws setup cleanup --prefix kernel-ci-$USER- --region us-west-2
```

Actually delete resources related to the configured infrastructure
```bash
kernel-ci-cloud-runner aws setup cleanup --prefix kernel-ci-$USER- --region us-west-2 --delete
```

This finds and removes: EC2 instances, ECS clusters/tasks/task definitions, IAM roles, ECR repositories, CloudWatch log groups, and S3 buckets matching the prefix.

## Example: Upstream Kernel Performance Regression Test

This walkthrough builds two upstream Linux kernel versions as RPMs on an x86_64 machine, uploads them, and runs a performance regression test.
The example uses `v6.1.141` (base) and `v6.1.150` (tip) from the stable kernel tree.

### Prerequisites

- An x86_64 Linux machine with at least 16 GB RAM and 20 GB free disk space
- Build tools: `gcc`, `make`, `flex`, `bison`, `elfutils-libelf-devel`, `openssl-devel`, `rpm-build`

Install build dependencies for e.g AL2023 or Fedora:
```bash
sudo dnf install -y gcc make flex bison elfutils-libelf-devel openssl-devel \
  rpm-build perl-devel bc
```

### Project Setup

All `kernel-ci-cloud-runner` commands below require the package to be installed in a virtual environment.
If not done already, set this up once:

```bash
cd kernel-ci-cloud-labs
python3.11 -m venv .venv
source .venv/bin/activate
python3.11 -m pip install -e .
```

Activate the virtual environment. After this, `kernel-ci-cloud-runner` is available in your shell.
If you open a new terminal, re-activate the venv first:

```bash
source .venv/bin/activate
```

### Build Kernel RPMs

The below steps build two versions of the 6.1 kernel, compatible to work in AWS EC2 VMs.

Create directory for RPMs
```bash
mkdir -p kernel-rpms
```


Clone the stable kernel tree (one-time, ~3 GB) and select 6.1 tree.
```bash
git clone --branch linux-6.1.y \
  https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git linux-stable
```

Build the base kernel (v6.1.141)
```bash
cd linux-stable
git checkout v6.1.141
git clean -xdf
make olddefconfig
# Enable AWS networking support (required for EC2 instances)
./scripts/config --enable CONFIG_NET_VENDOR_AMAZON --enable CONFIG_ENA_ETHERNET
# Build kernel with multiple processes
make binrpm-pkg -j$(nproc) 2>&1 | tee build-6.1.141.log

# Collect the built RPM
find ~/rpmbuild/RPMS/x86_64/ -name "kernel-*.rpm" ! -name "*headers*" \
  -exec cp {} ../kernel-rpms/ \;

cd ..

# Show current RPMs
ls kernel-rpms
```


Build the tip kernel (v6.1.150)
```bash
cd linux-stable
git checkout v6.1.150
git clean -xdf
make olddefconfig
# Enable AWS networking support (required for EC2 instances)
./scripts/config --enable CONFIG_NET_VENDOR_AMAZON --enable CONFIG_ENA_ETHERNET
# Build kernel with multiple processes
make binrpm-pkg -j$(nproc) 2>&1 | tee build-6.1.150.log

# Collect the built RPM
find ~/rpmbuild/RPMS/x86_64/ -name "kernel-*.rpm" ! -name "*headers*" \
  -exec cp {} ../kernel-rpms/ \;
cd ..
```

Check resulting RPM files:
```
ls kernel-rpms/
# Expected: kernel-6.1.141-1.x86_64.rpm  kernel-6.1.150-1.x86_64.rpm
```

### Upload and Run

In this step, we trigger the actual testing. The pipeline will spawn EC2 VMs, install each kernel in sequence, run UnixBench after each, and compare the results.
After completion, the benchmark regression analysis prints which metrics regressed between v6.1.141 and v6.1.150 (see [Benchmark Regression Detection](#benchmark-regression-detection) for output format).

**Note:** S3 bucket names are globally unique. If another user already created buckets with the same prefix, `setup configure` will succeed but the pipeline will fail when creating buckets.
Choose a unique `$USER` prefix or add a random suffix to avoid collisions.

Configure for regression testing only (x86_64)
```bash
kernel-ci-cloud-runner aws setup configure \
  --prefix kernel-ci-$USER-demo- --region us-west-2 \
  --test-filter unixbench-kernel-regression \
  --output demo-config.json
```

Upload the built RPMs
```bash
kernel-ci-cloud-runner aws setup upload-rpms \
  --bucket kernel-ci-$USER-demo-storage \
  --local-rpms kernel-rpms/
```

Run the pipeline
```bash
kernel-ci-cloud-runner aws run --config demo-config.json
```

Based on the output you should see that testing passed, and no performance regressions have been detected.

## Configuration

The project is configured via JSON files. By default, the file `examples/aws/config.json` is used. Explicitly specifying a config file to use is recommended.

Key sections of the configuration file are described below.

### Test Configuration
```json
"test_config": {
  "test_id": "test-001",
  "role_name": "ecsTaskExecutionRole",
  "vms": [
    {
      "ami_id": "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
      "instance_type": "t3.micro",
      "max_runtime": 3600,
      "test": ["basic-test", "example-reboot-test"],
      "min_count": 1
    }
  ]
}
```

**Test parameters:**
- `test_id` - Test id used for results folder path (will be expanded by date+time stamp)
- `role_name` - IAM role name to attach to EC2 instances (must match a role defined in the `roles` section)
- `ami_id` - AMI to use (supports SSM parameter resolution for x86_64 and arm64)
- `instance_type` - EC2 instance type
- `max_runtime` - Maximum duration in seconds per VM. The VM has a safety timeout that triggers automatic shutdown if the test hangs. SSM command timeout is `max_runtime + 3600` seconds to allow for multi-stage tests with reboots
- `test` - List of test directories from `vm-tests/`
- `min_count` - Number of VMs to spawn

### Storage Configuration
```json
"storage": {
  "type": "s3",
  "bucket": "kernel-ci-results",
  "results_prefix": "results"
}
```

S3 bucket names must be globally unique. If the name is taken, the system appends your AWS account number (e.g., `kernel-ci-results-123456789012`).

### ECS and CloudWatch Configuration

The `ecs` section defines the Fargate container that orchestrates tests (cpu, memory, cluster name, task definition). The `cloudwatch` section defines log retention for container logs (`/ecs/...`, default 7 days) and VM logs (`/ec2/...`, default 3 days). These are set automatically by `setup configure` and rarely need manual changes.

### Available Tests

Tests are located in `vm-tests/`:
- `basic-test` - Simple connectivity test
- `example-reboot-test` - Multi-stage reboot test
- `example-kernel-reboot-test` - Kernel installation and reboot
- `simple-unixbench` - UnixBench performance test
- `unixbench-kernel-regression` - Kernel regression testing with two kernel versions

Each test directory contains:
- `run.sh` or `run-*.sh` - Test scripts (executed in version-sorted order)
- `dependencies.txt` - Required packages
- `README.md` - Test documentation
- `external_requirements.json` - Declares external artifacts this test needs from the external storage bucket

Optional files: `common_lib.sh` (shared functions), test-specific data files.

#### Test Design

Tests are executed by the VM client script (`vm-tests/test-vm-client.sh`) which is downloaded and run on each EC2 instance via SSM. The client script manages the full lifecycle:

1. Downloads and extracts the test payload zip from S3 (first run only)
2. Discovers all `run*.sh` scripts in the test directory and sorts them by version (`sort -V`)
3. Executes one script per boot cycle, tracking progress via a `run_id` counter in S3
4. After each script: uploads the output log (`run-N-output.log`) and client log (`client-N.log`) to S3
5. If more scripts remain, exits with code **194** to signal SSM to reboot the VM
6. After reboot, SSM re-runs the client script, which increments `run_id` and executes the next script
7. After the final script: uploads `result.txt`, `stats.json`, and any `benchmark-*.csv` files, then shuts down

Exit code conventions for `run*.sh` scripts:
- **0** — success, continue to next script (or finish if last)
- **194** — explicitly request reboot (same effect as success for non-final scripts)
- **any other non-zero** — failure, stops the chain and reports the error

#### Writing New Tests

A test is a directory under `vm-tests/` containing at minimum:

- **`run.sh`** or **`run-*.sh`** — one or more executable scripts, run in version-sorted order (e.g. `run-01-setup.sh`, `run-02-verify.sh`). Each script runs after a fresh boot.
- **`external_requirements.json`** — declares which shared artifacts (e.g. kernel RPMs) the test needs from the external storage bucket. Set all values to `false` if none are needed.

Optional files:
- **`dependencies.txt`** — one package name per line (comments with `#`, blank lines ignored). Tests that need system packages should call an `install_test_dependencies` function from their `common_lib.sh` that reads this file and installs via `yum`/`dnf`.
- **`common_lib.sh`** — shared shell functions sourced by the `run-*.sh` scripts
- **`README.md`** — test documentation

The scripts run as `ec2-user` (or equivalent) with `sudo` available. The working directory is `$HOME/test-<RUN_PREFIX>-work/test/`, which persists across reboots. Environment variables like `RESULTS_BUCKET`, `RUN_PREFIX`, and `S3_PREFIX` are **not** passed to the scripts — the client script handles all S3 uploads. Scripts should write output files to the current directory.

#### Writing New Performance Benchmark Tests

When detecting performance regressions in a virtualized environment, comparing measured values with old known good values is error prone, as the environment can change.
Today's VM can be located in a different data center, might use updated firmware or hypervisor, might be located in a different network and other changes.
Therefore, to measure the impact of a changed kernel, we recommend to setup tests to run measurements with the current kernel (tip) as well as a reference kernel (base).
The testing system can automatically check for regressions if the two tests write two data CSV files that follow the naming pattern benchmark-base*.csv and benchmark-tip*.csv.

The CSV files must have the following columns:

```
metric,unit,value,more_is_better,kernel_version,instance_id,instance_type,arch
```

- `metric` — name of the benchmark metric (e.g. `Dhrystone_2_using_register_variables`)
- `unit` — measurement unit (e.g. `lps` for loops per second)
- `value` — numeric result
- `more_is_better` — `true` if higher values are better, `false` if lower is better
- `kernel_version` — kernel version string (e.g. `6.1.141-3.x86_64`)
- `instance_id`, `instance_type`, `arch` — EC2 instance metadata for traceability

Each row is one metric measurement from one VM. With multiple VMs per test, the analyzer aggregates values across VMs and uses statistical tests (Welch's t-test, Mann-Whitney U, Cohen's d) to detect regressions.


#### External Requirements

Tests declare which artifacts they need from the external storage bucket. Example:
```json
{
  "kernel-rpms/src": false,
  "kernel-rpms/binary": true
}
```

When set to `true`, the pipeline copies that folder from the external storage bucket to `shared/` in the results bucket before the test runs (once per run, shared across tests). VMs download from the `shared/` path. Tests that don't need external artifacts set all values to `false`.

## Architecture

This section briefly discusses how the project is setup.

### Code Structure

```
kernel-ci-cloud-labs/
├── src/
│   └── kernel_ci_cloud_labs/
│       ├── auth/           # Authentication modules (AWS)
│       │   ├── aws_auth.py
│       │   ├── aws_cluster_manager.py
│       │   ├── aws_role_manager.py
│       │   ├── aws_network_manager.py
│       │   ├── aws_task_definition_manager.py
│       │   └── aws_cloudwatch_manager.py
│       ├── core/           # Base classes and utilities
│       │   ├── base_auth.py
│       │   ├── base_provider.py
│       │   ├── base_storage.py
│       │   ├── base_resource_manager.py
│       │   ├── client_manager.py
│       │   ├── registry.py
│       │   └── pipeline.py
│       ├── providers/      # Cloud provider implementations
│       │   └── aws_provider.py
│       ├── storage/        # Storage backends
│       │   └── s3_storage.py
│       ├── cli.py                  # CLI entry point (kernel-ci-cloud-runner)
│       ├── eventbridge_handler.py  # EventBridge/Lambda entry point
│       ├── setup_configure.py      # Project configuration
│       ├── setup_upload_rpms.py    # RPM upload to S3
│       ├── setup_cleanup.py        # AWS resource cleanup
│       └── main.py
├── tests/                  # Unit tests and integration tests
├── vm-tests/               # VM test scripts
├── examples/               # Example configurations
├── .pre-commit-config.yaml # Pre-commit hooks config
├── .pylintrc               # Pylint configuration
├── pyproject.toml          # Black and pytest settings
└── setup.py
```

### S3 Storage Structure

The overview below shows how the storage in S3 for a testrun is organized.

```
s3://kernel-ci-results-{ACCOUNT_ID}/
└── run_{test_id}_{datetime}/
    ├── shared/                          # Shared resources (uploaded once per run)
    │   └── kernel-rpms/binary/
    │       ├── x86_64/*.rpm
    │       └── aarch64/*.rpm
    │
    └── test_{test_name}/
        ├── input/
        │   └── {test_name}_test_payload.zip  # Test scripts and dependencies
        ├── output/
        │   └── {instance_id}/
        │       ├── client-{run_id}.log       # Client execution logs per run
        │       ├── run-{run_id}-output.log   # Script output per run
        │       ├── result.txt                # Final test result (SUCCESS/FAIL)
        │       ├── stats.json                # Test statistics and timing
        │       └── benchmark-*.csv           # Benchmark results (if applicable)
        └── state/
            └── {instance_id}/
                └── run_id.txt                # Tracks current run stage
```

### Test Execution Flow

1. Pipeline uploads test payload zip and `test-vm-client.sh` to S3
2. Fargate container spawns EC2 VMs and sends SSM commands
3. Each VM downloads the bootstrap script to `/tmp/test-vm-client.sh`
    1. Test payload is extracted to `/home/ec2-user/test-run_<RUN_PREFIX>-work/test/`
    2. Test scripts (`run-01-*.sh`, `run-02-*.sh`, ...) execute in version-sorted order
    3. Run state is tracked via `run_id.txt` in S3 — after a reboot, the VM re-downloads the script, checks `run_id`, and continues from the next stage
    4. Results are uploaded to S3 per instance

## Benchmark Regression Detection

When a pipeline run completes, the system automatically analyzes benchmark results from tests that produce `benchmark-base-*.csv` and `benchmark-tip-*.csv` files (e.g. `unixbench-kernel-regression`). Tests without benchmark CSVs are silently skipped.

For each metric, the analyzer compares the value distributions across all VMs and computes:
- **Welch's t-test** and **Mann-Whitney U test** for statistical significance
- **Cohen's d** for practical effect size

A regression is flagged only when **both** conditions are met:
1. At least one statistical test is significant (p < 0.05)
2. The effect size is meaningful (|Cohen's d| ≥ 0.5)

### Example Output

```
============================================================
BENCHMARK REGRESSION ANALYSIS
============================================================

Test: unixbench-kernel-regression
  Base kernel: 6.1.141-165.249.amzn2023.x86_64
  Tip kernel:  6.1.150-174.273.amzn2023.x86_64
  Metrics compared: 24
  ⚠ REGRESSIONS DETECTED: 16
    Process_Creation: base=57012.12±658.82 (cv: 0.01) → tip=41959.92±306.71 (cv: 0.01) lps (-26.4%)
      [t-test p=0.0000, U-test p=0.0001, Cohen's d=29.29]
    Execl_Throughput: base=22944.75±155.73 (cv: 0.01) → tip=21684.21±154.45 (cv: 0.01) lps (-5.5%)
      [t-test p=0.0000, U-test p=0.0001, Cohen's d=8.13]

------------------------------------------------------------
Tests with benchmarks: 1 | Regressions found: 1
Tests with regressions: unixbench-kernel-regression
============================================================
```

### Notification Hooks

For future extensions, or integration into Kernel CI workflows, we might need to report test results.

The `BenchmarkAnalyzer` returns a `PipelineBenchmarkSummary` dataclass with structured regression data. To add downstream notifications (e.g. SNS alerts, KCIDB reporting, Slack), see the `NOTIFICATION HOOK` comments in:
- `src/kernel_ci_cloud_labs/core/benchmark_analyzer.py` — after summary logging
- `src/kernel_ci_cloud_labs/core/pipeline.py` — after benchmark analysis completes

The `PipelineBenchmarkSummary` contains:
- `test_results` — list of `TestBenchmarkResult`, one per test
- `tests_with_regression` / `regression_test_names` — quick summary of which tests regressed

Each `TestBenchmarkResult` contains:
- `base_kernel` / `tip_kernel` — kernel version strings
- `comparisons` — list of `MetricComparison` (one per benchmark metric)
- `regressions` — property that filters to only regressed metrics

Each `MetricComparison` contains:
- `metric`, `unit`, `more_is_better` — metric identity
- `base` / `tip` — `MetricStats` with `mean`, `median`, `stddev`, `cv`, `values`
- `pct_change`, `t_pvalue`, `u_pvalue`, `cohens_d` — statistical results
- `is_regression` — boolean flag

Example integration at the `NOTIFICATION HOOK` in `pipeline.py`:

```python
# After benchmark_summary is computed:
if benchmark_summary.tests_with_regression > 0:
    # SNS notification
    sns = provider.auth.get_client("sns")
    message = f"Regressions in: {', '.join(benchmark_summary.regression_test_names)}"
    sns.publish(TopicArn="arn:aws:sns:...:kernel-ci-alerts", Message=message)

    # KCIDB submission
    for result in benchmark_summary.test_results:
        for reg in result.regressions:
            submit_to_kcidb(result.test_name, reg.metric, reg.pct_change,
                            result.base_kernel, result.tip_kernel)

    # Write machine-readable JSON for downstream tools
    import json
    regression_data = [{
        "test": r.test_name,
        "base_kernel": r.base_kernel,
        "tip_kernel": r.tip_kernel,
        "regressions": [{
            "metric": c.metric, "pct_change": c.pct_change,
            "cohens_d": c.cohens_d, "t_pvalue": c.t_pvalue,
        } for c in r.regressions],
    } for r in benchmark_summary.test_results if r.has_regression]
    storage.upload_string(json.dumps(regression_data, indent=2),
                          f"{run_prefix}/regression_report.json")
```

### Re-Analyzing Previous Runs

To re-run the analysis on results from a previous pipeline run without re-running the pipeline:

```bash
kernel-ci-cloud-runner aws analyze \
  --bucket kernel-ci-$USER-results \
  --run-prefix run_test-001_20260325_120000 \
  --region us-west-2
```

This downloads all `benchmark-*.csv` files from S3, combines them, compares the two kernel versions, and generates regression plots (overall, x86_64, ARM64) in `analysis/data/{run_prefix}/`. Add `--upload-analysis` to upload the results back to S3.

Requires the analysis dependencies: `python3.11 -m pip install -e ".[analysis]"`

## Automated Triggering via EventBridge

The pipeline can be triggered automatically using Amazon EventBridge — either on a schedule (e.g. daily regression runs) or from custom events (e.g. a new kernel build notification).
This functionality is not fully implemented yet and needs to be adapted to the specific use case.

### How It Works

The EventBridge handler (`src/kernel_ci_cloud_labs/eventbridge_handler.py`) runs as an AWS Lambda function and performs:

1. **Downloads the pipeline config** from an S3 URI provided in the event payload.
2. **Prepares kernel RPMs** — currently expects RPMs to be pre-uploaded to the external storage bucket. A future implementation will automatically retrieve the latest tip kernel and a base kernel for comparison (see `_prepare_kernel_rpms()` in the handler).
3. **Makes the config run-local** by appending a unique suffix to `test_id`, so parallel EventBridge invocations don't collide.
4. **Runs the normal pipeline** — no additional permissions beyond what the pipeline already requires.

### Why RPM Retrieval Runs in the Lambda, Not in Fargate

Kernel RPM retrieval must happen in the Lambda handler *before* the pipeline starts — it cannot run inside the Fargate container. The Fargate container (`launch_vm.py`) is a lightweight VM orchestrator with only `boto3` available. It spawns EC2 VMs, sends SSM commands, and waits for results. It has no access to the pipeline config or the `kernel_ci_cloud_labs` package.

The pipeline copies kernel RPMs from the external storage bucket to `shared/` in the results bucket *before* spawning the Fargate container. VMs then download RPMs from `shared/` when they boot. If RPMs aren't in the external storage bucket before `run_pipeline()` is called, VMs won't find them.

```
EventBridge → Lambda handler
  1. Download config from S3
  2. Retrieve kernel RPMs → upload to external storage bucket
  3. Make config run-local (unique test_id)
  4. run_pipeline() → copies RPMs to shared/ → spawns Fargate → VMs download from shared/
```

**Note:** When triggered via EventBridge, the pipeline pulls test scripts from the external storage bucket (uploaded via `setup upload-tests`), not from the Lambda deployment zip. If you update test scripts in `vm-tests/`, re-run `setup upload-tests` for changes to take effect in EventBridge-triggered runs. CLI runs (`aws run`) always use the local `vm-tests/` directory.

### Prerequisites

- All AWS resources must already exist (`kernel-ci-cloud-runner aws setup configure` + first manual run).
- A valid `config.json` must be uploaded to S3 (e.g. `s3://kernel-ci-$USER-storage/configs/config.json`).
- Kernel RPMs must be pre-uploaded to the external storage bucket (until automatic retrieval is implemented).
- Test scripts must be uploaded to the external storage bucket:
  ```bash
  kernel-ci-cloud-runner aws setup upload-tests \
    --bucket kernel-ci-$USER-storage --region us-west-2
  ```
  Re-run this command after modifying any test scripts in `vm-tests/`.
- Set `"force_recreate_roles": false` in the config to avoid disrupting parallel runs.

### EventBridge Setup

**1. Deploy the Lambda function:**

Package `kernel_ci_cloud_labs` and its dependencies (`boto3` is provided by the Lambda runtime) into a deployment zip, then create the function:

```bash
aws lambda create-function \
  --function-name kernel-ci-daily-regression \
  --runtime python3.12 \
  --handler kernel_ci_cloud_labs.eventbridge_handler.lambda_handler \
  --role arn:aws:iam::<ACCOUNT>:role/<LAMBDA_EXECUTION_ROLE> \
  --timeout 900 \
  --memory-size 256 \
  --zip-file fileb://deployment.zip \
  --region eu-west-2
```

The Lambda execution role needs the same permissions as the pipeline (S3, ECS, EC2, SSM, IAM, ECR, CloudWatch). You can reuse the existing pipeline role or create a dedicated one.

**2. Create a scheduled EventBridge rule (e.g. daily at 02:00 UTC):**

```bash
aws events put-rule \
  --name kernel-ci-daily-regression \
  --schedule-expression "cron(0 2 * * ? *)" \
  --state ENABLED \
  --region eu-west-2
```

**3. Add the Lambda as target with the config payload:**

```bash
aws events put-targets \
  --rule kernel-ci-daily-regression \
  --targets '[{
    "Id": "kernel-ci-pipeline",
    "Arn": "arn:aws:lambda:<REGION>:<ACCOUNT>:function:kernel-ci-daily-regression",
    "Input": "{\"config_s3_uri\": \"s3://kernel-ci-$USER-storage/configs/config.json\", \"region\": \"eu-west-2\"}"
  }]' \
  --region eu-west-2
```

**4. Grant EventBridge permission to invoke the Lambda:**

```bash
aws lambda add-permission \
  --function-name kernel-ci-daily-regression \
  --statement-id eventbridge-invoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:<REGION>:<ACCOUNT>:rule/kernel-ci-daily-regression
```

### Event Payload Format

The handler expects this JSON structure (passed as the EventBridge target input):

```json
{
  "config_s3_uri": "s3://kernel-ci-$USER-storage/configs/config.json",
  "region": "eu-west-2"
}
```

- `config_s3_uri` (required): S3 URI to the pipeline config JSON. Must use resource names matching your prefix (as produced by `setup configure`).
- `region` (optional): AWS region. Defaults to `AWS_DEFAULT_REGION` environment variable or `us-west-2`.

### Custom Event Triggers

Instead of a schedule, you can trigger the pipeline from custom events (e.g. when a new kernel build completes):

```bash
aws events put-rule \
  --name kernel-ci-on-new-build \
  --event-pattern '{"source": ["custom.kernel-build"], "detail-type": ["BuildComplete"]}' \
  --state ENABLED \
  --region eu-west-2
```

Then publish events to trigger it:

```bash
aws events put-events --entries '[{
  "Source": "custom.kernel-build",
  "DetailType": "BuildComplete",
  "Detail": "{\"config_s3_uri\": \"s3://kernel-ci-$USER-storage/configs/config.json\", \"region\": \"eu-west-2\"}"
}]'
```

Note: For custom events, the handler reads from the top-level event dict. If your event source puts data in `detail`, you'll need to adjust the EventBridge target input transformer or the handler accordingly.

### Debugging EventBridge Runs

- Lambda logs go to CloudWatch Logs under `/aws/lambda/<function-name>`.
- Each invocation logs a unique invocation ID for tracing.
- Set the `LOG_LEVEL` environment variable on the Lambda to `DEBUG` for verbose output.
- The handler returns a JSON response with `status`, `invocation_id`, and `test_id` for programmatic monitoring.

## Development

### Running Tests

The project has a Makefile that simplifies running most steps. The calls should be run in the virtual environment, to make sure dependencies are present.

```bash
# Run unit tests (fast, no AWS resources needed)
make test

# Run linting (flake8 + pylint)
make lint

# Format code (black + isort)
make format

# Run integration tests (requires AWS credentials, creates real resources and incurs AWS costs)
pytest tests/integration/ -v -m integration

# Run with coverage (unit tests only)
pytest tests/ -v --cov=src --cov-report=term-missing -m "not integration"
```

### Code Quality

The project uses Black for formatting, isort for import sorting, flake8 and Pylint for linting.
All tools are configured to use 120-character line width.

Configuration files: `pyproject.toml` (Black, pytest), `.flake8`, `.pylintrc`

## Debugging

### Force Recreate Roles

The `force_recreate_roles` parameter in `config.json` controls whether IAM roles are deleted and recreated:

```json
"force_recreate_roles": true
```

- Set to `true` when you've modified role policies or trust relationships and need to apply changes
- Set to `false` for normal operation to avoid disrupting running tasks

**⚠️ Warning:** Setting `force_recreate_roles: true` will delete existing IAM roles and terminate any running ECS tasks that use those roles. Never use while other pipelines are active.

## Troubleshooting

### Investigating Failed Pipeline Runs

When a pipeline run fails, the container logs show a summary like:

```
[...] [INFO] ✓ Stopped task: 6e3b9905e4214826962a2f3a43d548fe
```

Use the task ID to investigate:

**1. Check why the ECS task stopped:**

```bash
aws ecs describe-tasks \
  --cluster kernel-ci-$USER-cluster \
  --tasks <TASK_ID> \
  --region us-west-2
```

Look for `stoppedReason`, `stopCode`, and the container's `exitCode`. Exit code 1 means the test failed; exit code 137 means the container was killed (OOM or timeout).

**2. Read the container logs from CloudWatch:**

```bash
aws logs filter-log-events \
  --log-group-name /ecs/kernel-ci-$USER-task \
  --log-stream-names ecs/kernel-ci-$USER-app/<TASK_ID> \
  --region us-west-2
```

This shows which VMs were spawned, whether tests passed or failed, and the final summary (e.g. `=== All VMs completed: 0/1 successful, 1 failed ===`).

**3. Check the VM's test output in S3:**

```bash
# List output files for a specific VM
aws s3 ls s3://kernel-ci-$USER-results/<RUN_PREFIX>/test_<TEST_NAME>/output/<INSTANCE_ID>/ \
  --region us-west-2

# Download the run output log that failed (e.g. run-2)
aws s3 cp s3://kernel-ci-$USER-results/<RUN_PREFIX>/test_<TEST_NAME>/output/<INSTANCE_ID>/run-2-output.log - \
  --region us-west-2
```

The `run-N-output.log` files contain the full shell trace (`set -x`) of each test stage. The `result.txt` file contains the final status (e.g. `FAILED: Exit code 1 at run 2`).

**4. Check the client log for the bootstrap and S3 upload sequence:**

```bash
aws s3 cp s3://kernel-ci-$USER-results/<RUN_PREFIX>/test_<TEST_NAME>/output/<INSTANCE_ID>/client-2.log - \
  --region us-west-2
```

The `RUN_PREFIX` and `INSTANCE_ID` values are visible in the container logs from step 2.

## License

See LICENSE file.
