# QUICKSTART — KernelCI pull-lab integration

This guide gets pullab_cloud polling KernelCI for pull-lab jobs and pushing
results to KCIDB. The flow is:

1. kernelci-api->poll (/events)
2. pull_labs_poller translates the job definition
3. pull_labs_poller calls the existing AWS pipeline with the translated config
4. pull_labs_poller submits tests-only revisions to kcidb-restd-rs

For the underlying AWS setup (IAM roles, S3 buckets, ECS cluster, ECR
image, etc.) see the main `README.md`. This file only covers the
KernelCI/KCIDB wiring on top of an already-working AWS pipeline.

## 1. Set up the AWS pipeline first

The KernelCI poller drives the *existing* AWS pipeline — it does not
provision AWS resources on its own. Before continuing, make sure the
pipeline can run a job end-to-end. The full walkthrough lives in
[`README.md`](README.md); the minimum steps are:

1. **Install the package** in a venv — see
   [README → Installation](README.md#installation):
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e .
   ```
2. **Configure AWS credentials** — see
   [README 1. Configure AWS Credentials](README.md#1-configure-aws-credentials).
   Either `aws configure` / an IAM role, or drop
   `examples/aws/credentials.json` in place.
3. **Generate the pipeline config** — see
   [README 2. Configure the project](README.md#2-configure-the-project):
   ```bash
   kernel-ci-cloud-runner aws setup configure \
     --prefix kernel-ci-$USER- --region us-west-2
   ```
   This populates `examples/aws/config.json` with unique S3/IAM/ECS/ECR
   names. Use `--output my-config.json` to write to a different path
   (then pass `PULLAB_BASE_CONFIG=my-config.json` to the poller).
4. **Verify the pipeline works** — see
   [README 3. Run integration test to verify setup](README.md#3-run-integration-test-to-verify-setup):
   ```bash
   pytest tests/integration/ -v -m integration
   ```
   Look for `VMs: 2/2 spawned, 2 successful, 0 failed`. If this passes,
   the AWS side is ready and you can proceed below.

If your jobs install custom kernels, also follow
[README 4. Upload kernel RPMs](README.md#4-upload-kernel-rpms-required-for-kernel-install-tests).

To tear everything down afterwards, see
[README 7. Clean up resources](README.md#7-clean-up-resources).

## Prerequisites

- A working AWS pipeline — `kernel-ci-cloud-runner aws setup configure`
  has been run and `examples/aws/config.json` is populated (see
  section 1 above).
- A reachable kernelci-api with at least one pull-lab job scheduled to
  `pull-labs-aws-ec2` (or whatever runtime name you use).
- A reachable `kcidb-restd-rs` `/submit` endpoint.
- A JWT signed with the kcidb-restd-rs `unified_secret` carrying the
  origin you'll use for these rows.

## 2. Configure the kernelci section

Open `examples/aws/config.json` and edit the `kernelci` block that was
added alongside `test_config`:

```json
"kernelci": {
  "api_base_uri":    "https://staging.kernelci.org:9000/latest",
  "api_token":       null,
  "runtime_name":    "pull-labs-aws-ec2",
  "poll_interval_sec": 30,
  "cursor_file":     "/tmp/pullab_cloud_cursor.json",
  "kcidb_submit_url":"https://kcidb-restd.example.org/submit",
  "kcidb_origin":    "pullab_cloud_aws",
  "kcidb_jwt":       null
}
```

Secrets (`api_token`, `kcidb_jwt`) are normally injected via environment
variables, not committed to the file:

| Env var | Falls back to | Purpose |
| --- | --- | --- |
| `KERNELCI_API_BASE_URI` | `kernelci.api_base_uri` | API URL |
| `KERNELCI_API_TOKEN` | `kernelci.api_token` | Bearer token for the API (optional for public endpoints) |
| `KERNELCI_RUNTIME_NAME` | `kernelci.runtime_name` | Lab/runtime to consume jobs for |
| `KCIDB_SUBMIT_URL` | `kernelci.kcidb_submit_url` | kcidb-restd-rs submit URL |
| `KCIDB_JWT` | `kernelci.kcidb_jwt` | JWT bearer token |
| `KCIDB_REST` | (alternative to the two above) | `https://<token>@host[/path]` — kci-dev-compatible single URL carrying both endpoint and token |
| `KCIDB_ORIGIN` | `kernelci.kcidb_origin` | Origin string in submitted rows |
| `PULLAB_CURSOR_FILE` | `kernelci.cursor_file` | Where to persist the poll cursor |
| `PULLAB_POLL_INTERVAL_SEC` | `kernelci.poll_interval_sec` | Sleep between empty polls |
| `PULLAB_BASE_CONFIG` | `examples/aws/config.json` | Path to base config |

## 3. Run a single poll cycle (dry test)

```bash
export KCIDB_JWT="eyJ...your.token..."
make poller-once
# or:
python -m kernel_ci_cloud_labs.pull_labs_poller --config examples/aws/config.json --once
```

What happens:

1. Fetches `/events?state=available&kind=job&recursive=true&from=<cursor>`.
2. Skips events whose `node.data.data.runtime` ≠ `runtime_name`.
3. For each matching event, downloads `node.artifacts.job_definition` JSON.
4. Walks `node.parent` to find the `kbuild` ancestor and builds
   `build_id = "<kcidb_origin>:<kbuild_node_id>"`.
5. Translates the job → `test_config.vms[*]` and calls `run_pipeline()`.
6. Submits one tests-only KCIDB revision per job.
7. Persists the latest event `timestamp` to the cursor file.

## 4. Run as a long-lived service

```bash
make poller
# or:
python -m kernel_ci_cloud_labs.pull_labs_poller --config examples/aws/config.json
```

Sleep interval between polls when there is nothing to do is
`PULLAB_POLL_INTERVAL_SEC` (default 30s).

## 5. Run in AWS Lambda

The same module exposes `lambda_handler(event, context)` that runs one
poll cycle per invocation. Wire it to an EventBridge schedule (e.g.
every minute) and set the env vars on the Lambda function. The cursor
file lives on `/tmp` by default — fine for steady polling, but configure
`PULLAB_CURSOR_FILE` to a persistent path (or write a custom
`CursorStore` backed by S3/DynamoDB) if you need true cross-cold-start
deduplication.

Lambda handler entry point:
`kernel_ci_cloud_labs.pull_labs_poller.lambda_handler`

## 6. Run in a container

The poller has no AWS-specific imports at the top level (other than the
default executor which calls into the existing AWS pipeline). For a
custom executor, instantiate `PullLabsPoller` directly:

```python
from kernel_ci_cloud_labs.pull_labs_poller import PullLabsPoller
poller = PullLabsPoller(config, job_executor=my_executor)
poller.run_forever()
```

`my_executor(run_config) -> (test_rows, log_url)` is called once per
job; `test_rows` is a list of `{"name": str, "status": str,
"duration_ms": Optional[int]}` dicts.

## 7. Verify

- **Events reach the poller:** run `--once` with `--log-level DEBUG` and
  confirm the poll URL and event count are logged.
- **Cursor advances:** inspect `cat /tmp/pullab_cloud_cursor.json` after
  a cycle.
- **KCIDB receives rows:** if `kcidb-restd-rs` is local, check its
  `spool_directory` for a `submission-*.json`. Open one and confirm
  `tests[*]` rows have your `origin`, a `build_id` of the form
  `<origin>:<kbuild_node_id>`, and statuses in
  `{PASS, FAIL, SKIP, ERROR, MISS, DONE}`.
- **AWS run actually ran:** the existing pipeline logs land under
  `logs/run_*/` and `s3://<results-bucket>/run_pulllab-*/`.
- **Payload shape (optional):** if you have `kci-dev` installed, you can
  sanity-check our submission shape by capturing one payload (with
  logging) and piping it through:
  ```bash
  kci-dev submit build --from-json <captured.json> --origin <kcidb_origin> --dry-run
  ```
  Our poller speaks the same KCIDB v5.3 schema, so this should
  round-trip cleanly.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `Missing required configuration: kernelci.kcidb_jwt` | env var not set and config value is null |
| Events come back but none are processed | `runtime_name` mismatch with what the scheduler set; check `node.data.data.runtime` in a raw event |
| `Could not resolve build_id` warning | Job node has no `kbuild` ancestor reachable within 8 hops, or `api_token` is missing for a protected `/node/{id}` endpoint |
| `Translation failed … missing required artifacts.kernel` | The KernelCI build that produced this job did not upload a kernel image |
| HTTP 401 from KCIDB submit | JWT not signed by the kcidb-restd-rs `unified_secret`, expired, or origin claim mismatch |
| Same job processed repeatedly | Cursor file path not persistent across restarts (Lambda `/tmp` is ephemeral across cold starts) |

## TODO:

- We do no change yet job state from available. We have deduplication via the cursor, but if the poller restarts it may re-process some events.
