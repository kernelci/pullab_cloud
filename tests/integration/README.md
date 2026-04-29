# Integration Tests

**⚠️ These tests spawn real AWS resources and incur costs (~$0.01-0.10 per run).**

## Prerequisites

- AWS credentials configured (via `aws configure`, IAM role, or environment variables)
- Project configured: `kernel-ci-cloud-runner aws setup configure --prefix kernel-ci-$USER- --region us-west-2`
- The test will fail if `examples/aws/config.json` still has template defaults

## Running

```bash
pytest tests/integration/ -v -m integration
```

## What It Does

1. Validates AWS authentication (STS GetCallerIdentity)
2. Checks IAM role, ECS cluster, task definition, and CloudWatch log groups exist
3. Runs the full pipeline with 2 VMs:
   - 1x `t3.micro` (x86_64) running `basic-test`
   - 1x `t4g.micro` (arm64) running `basic-test`
4. Verifies test payload uploaded to S3
5. Checks pipeline logs, container logs, and VM logs exist
6. Validates summary: all VMs spawned and succeeded

Completes in ~2-5 minutes. Logs saved to `tests/integration/logs/`.

## Cleanup

The pipeline's `finally` block stops the ECS task and terminates VMs tagged with the run prefix.
Infrastructure (cluster, roles, ECR, buckets) is **not** deleted — use `kernel-ci-cloud-runner aws setup cleanup` for that.
