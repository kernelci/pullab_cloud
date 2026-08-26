"""CLI entry point for kernel-ci-cloud-runner.

Usage:
    kernel-ci-cloud-runner aws run [--config CONFIG] [--config-s3 S3_URI] [--results-dir DIR]
    kernel-ci-cloud-runner aws analyze --bucket BUCKET --run-prefix PREFIX [--output-dir DIR]
    kernel-ci-cloud-runner aws setup configure [--prefix PREFIX] [--region REGION] [--output FILE]
    kernel-ci-cloud-runner aws setup upload-rpms --bucket BUCKET --local-rpms DIR [--region REGION]
    kernel-ci-cloud-runner aws setup upload-tests --bucket BUCKET [--test-dir DIR] [--region REGION]
    kernel-ci-cloud-runner aws setup cleanup --prefix PREFIX [--region REGION] [--delete]
    kernel-ci-cloud-runner aws setup validate [--bucket BUCKET] [--role ROLE] [--region REGION] [--fix]
"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import argparse
import sys


def cmd_run(args):
    """Run the kernel CI pipeline."""
    import json
    import logging
    import os
    import tempfile

    import boto3

    from kernel_ci_cloud_labs.core.logging_config import (
        create_run_directory,
        get_logger,
        setup_run_logging,
    )
    from kernel_ci_cloud_labs.core.pipeline import run_pipeline
    from kernel_ci_cloud_labs.core.registry import (
        AUTH_REGISTRY,
        PROVIDER_REGISTRY,
        STORAGE_REGISTRY,
    )
    from kernel_ci_cloud_labs.main import import_all_packages, load_credentials

    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    run_dir = create_run_directory()
    setup_run_logging(run_dir, level=log_level)
    logger = get_logger(__name__)

    # S3 config takes precedence (for EventBridge triggers)
    config_path = args.config
    if args.config_s3:
        logger.info("Downloading config from %s", args.config_s3)
        parts = args.config_s3.replace("s3://", "").split("/", 1)
        s3 = boto3.client("s3", region_name=args.region)
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        s3.download_file(parts[0], parts[1], tmp.name)
        config_path = tmp.name

    for pkg in [
        "kernel_ci_cloud_labs.providers",
        "kernel_ci_cloud_labs.storage",
        "kernel_ci_cloud_labs.auth",
    ]:
        import_all_packages(pkg)

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    credentials = load_credentials(config_path)

    auth = AUTH_REGISTRY[config["auth_credentials"]["auth_provider"]](config, credentials)
    storage_config = {
        **config["storage"],
        "region": config.get("region"),
        "external_storage": config.get("external_storage", {}),
    }
    storage = STORAGE_REGISTRY[config["storage"]["type"]](storage_config, auth)
    provider = PROVIDER_REGISTRY[config["provider"]](auth, config, storage)

    run_pipeline(provider, storage, run_dir=run_dir)

    # Optionally persist all of this run's S3 result objects to a local
    # directory (benchmark CSVs, result.txt, console logs, etc.). aws run
    # otherwise only keeps the orchestrator logs under logs/run_*/.
    if getattr(args, "results_dir", None):
        _download_run_results(storage, args.results_dir, logger)


def _download_run_results(storage, results_dir, logger):
    """Download every S3 object under this run's prefix into results_dir.

    Preserves the S3 key structure under results_dir/<run_prefix>/ so the
    layout matches the bucket. Best-effort: logs and continues on error.
    """
    import os as _os

    bucket = getattr(storage, "bucket", None)
    run_prefix = getattr(storage, "run_prefix", None)
    s3 = getattr(storage, "s3", None)
    if not (bucket and run_prefix and s3 is not None):
        logger.warning("Cannot download results: bucket/run_prefix/s3 client unavailable")
        return

    dest_root = _os.path.join(results_dir, run_prefix)
    logger.info("Downloading run results from s3://%s/%s/ to %s", bucket, run_prefix, dest_root)
    count = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{run_prefix}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                # Strip the run_prefix so files land under dest_root/<rest>.
                rel = key[len(run_prefix) + 1:] if key.startswith(run_prefix + "/") else key
                local_path = _os.path.join(dest_root, rel)
                _os.makedirs(_os.path.dirname(local_path), exist_ok=True)
                s3.download_file(bucket, key, local_path)
                count += 1
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Error downloading run results: %s", e)
    logger.info("✓ Downloaded %d result file(s) to %s", count, dest_root)


def cmd_setup_configure(args):
    """Configure project resources."""
    import json
    from pathlib import Path

    from kernel_ci_cloud_labs.setup_configure import (
        get_default_prefix,
        print_resource_summary,
        update_config,
    )

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    prefix = args.prefix or get_default_prefix()
    region = args.region or "us-west-2"

    config = update_config(config, prefix, region, args.test_filter)
    if args.force_recreate_roles:
        config["force_recreate_roles"] = True

    print_resource_summary(config, prefix, region)

    if args.dry_run:
        print("DRY RUN - no changes made")
        print("\nUpdated config would be:")
        print(json.dumps(config, indent=2))
        return

    output_path = Path(args.output) if args.output else config_path
    with open(output_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print(f"{'Wrote' if args.output else 'Updated'} {output_path}")


def cmd_setup_upload_rpms(args):
    """Upload kernel RPMs to S3."""
    argv = ["--bucket", args.bucket, "--local-rpms", args.local_rpms]
    if args.region:
        argv += ["--region", args.region]
    old_argv = sys.argv
    sys.argv = ["upload-rpms"] + argv
    try:
        from kernel_ci_cloud_labs.setup_upload_rpms import main as upload_main

        upload_main()
    finally:
        sys.argv = old_argv


def cmd_setup_cleanup(args):
    """Clean up AWS resources."""
    argv = ["--prefix", args.prefix]
    if args.region:
        argv += ["--region", args.region]
    if args.delete:
        argv.append("--delete")
    old_argv = sys.argv
    sys.argv = ["cleanup"] + argv
    try:
        from kernel_ci_cloud_labs.setup_cleanup import main as cleanup_main

        cleanup_main()
    finally:
        sys.argv = old_argv


def cmd_setup_upload_tests(args):
    """Upload test scripts to S3."""
    import boto3

    from kernel_ci_cloud_labs.setup_upload_tests import upload_test_scripts

    s3 = boto3.client("s3", region_name=args.region)
    print(f"Uploading tests from {args.test_dir} to s3://{args.bucket}/test-scripts/")
    count = upload_test_scripts(s3, args.bucket, args.test_dir)
    print(f"\nUploaded {count} test(s)")
    if count == 0:
        sys.exit(1)


def cmd_setup_validate(args):
    """Validate AWS setup and KernelCI/KCIDB tokens; optionally create missing resources."""
    from kernel_ci_cloud_labs.setup_validate import validate

    sys.exit(validate(
        bucket=args.bucket,
        role_name=args.role,
        region=args.region,
        api_base_uri=args.api_url,
        fix=args.fix,
    ))


def cmd_analyze(args):
    """Download and analyze benchmark results from a previous pipeline run."""
    try:
        from kernel_ci_cloud_labs.analysis.run_analysis import main as analysis_main
    except ImportError as e:
        print(f"Analysis requires extra dependencies: {e}")
        print("Install with: pip install -e '.[analysis]'")
        sys.exit(1)

    import types

    analysis_args = types.SimpleNamespace(
        bucket=args.bucket,
        run_prefix=args.run_prefix,
        output_dir=args.output_dir,
        region=args.region,
        file_pattern="benchmark-*.csv",
        combined_csv="combined_results.csv",
        regression_csv="regression_results.csv",
        upload_analysis=args.upload_analysis,
    )
    sys.exit(analysis_main(analysis_args))


def main():
    parser = argparse.ArgumentParser(
        prog="kernel-ci-cloud-runner",
        description="Kernel CI Cloud Labs — automated kernel testing on cloud infrastructure",
    )
    cloud_parsers = parser.add_subparsers(dest="cloud", help="Cloud provider")

    # --- aws ---
    aws_parser = cloud_parsers.add_parser("aws", help="Amazon Web Services")
    aws_sub = aws_parser.add_subparsers(dest="command", help="Command")

    # aws run
    run_parser = aws_sub.add_parser("run", help="Run the kernel CI pipeline")
    run_parser.add_argument(
        "--config",
        default="examples/aws/config.json",
        help="Path to config.json (default: examples/aws/config.json)",
    )
    run_parser.add_argument(
        "--config-s3",
        help="S3 URI to config (e.g. s3://bucket/config.json). "
        "Takes precedence over --config. Designed for EventBridge triggers.",
    )
    run_parser.add_argument("--region", help="AWS region (for S3 config download)")
    run_parser.add_argument(
        "--results-dir",
        help="Download all of this run's S3 result files (benchmark CSVs, "
        "result.txt, console logs) into DIR/<run_prefix>/ after the run",
    )
    run_parser.set_defaults(func=cmd_run)

    # aws analyze
    analyze_parser = aws_sub.add_parser("analyze", help="Download and analyze benchmark results from a previous run")
    analyze_parser.add_argument("--bucket", required=True, help="S3 results bucket name")
    analyze_parser.add_argument("--run-prefix", required=True, help="Run prefix (e.g. run_test-001_20260325_120000)")
    analyze_parser.add_argument("--output-dir", help="Output directory (default: analysis/data/{run_prefix}/)")
    analyze_parser.add_argument("--region", default="us-west-2", help="AWS region (default: us-west-2)")
    analyze_parser.add_argument("--upload-analysis", action="store_true", help="Upload analysis results back to S3")
    analyze_parser.set_defaults(func=cmd_analyze)

    # aws setup
    setup_parser = aws_sub.add_parser("setup", help="Setup and manage AWS resources")
    setup_sub = setup_parser.add_subparsers(dest="setup_command", help="Setup command")

    # aws setup configure
    cfg_parser = setup_sub.add_parser("configure", help="Configure project resource names")
    cfg_parser.add_argument("--prefix", help="Resource name prefix (default: kernel-ci-$USER-)")
    cfg_parser.add_argument("--region", help="AWS region (default: us-west-2)")
    cfg_parser.add_argument(
        "--config",
        default="examples/aws/config.json",
        help="Input config template (default: examples/aws/config.json)",
    )
    cfg_parser.add_argument("--output", help="Write config to this file instead of modifying the input")
    cfg_parser.add_argument("--test-filter", help="Only include tests matching this substring")
    cfg_parser.add_argument(
        "--force-recreate-roles",
        action="store_true",
        help="Set force_recreate_roles=true in config",
    )
    cfg_parser.add_argument("--dry-run", action="store_true", help="Preview changes only")
    cfg_parser.set_defaults(func=cmd_setup_configure)

    # aws setup upload-rpms
    rpm_parser = setup_sub.add_parser("upload-rpms", help="Upload kernel RPMs to S3")
    rpm_parser.add_argument("--bucket", required=True, help="S3 bucket name")
    rpm_parser.add_argument("--local-rpms", required=True, help="Directory containing RPMs")
    rpm_parser.add_argument("--region", help="AWS region")
    rpm_parser.set_defaults(func=cmd_setup_upload_rpms)

    # aws setup cleanup
    clean_parser = setup_sub.add_parser("cleanup", help="Find and remove AWS resources by prefix")
    clean_parser.add_argument("--prefix", required=True, help="Resource name prefix")
    clean_parser.add_argument("--region", help="AWS region")
    clean_parser.add_argument("--delete", action="store_true", help="Actually delete resources")
    clean_parser.set_defaults(func=cmd_setup_cleanup)

    # aws setup upload-tests
    test_parser = setup_sub.add_parser("upload-tests", help="Upload test scripts to S3 for EventBridge runs")
    test_parser.add_argument("--bucket", required=True, help="S3 external storage bucket")
    test_parser.add_argument("--test-dir", default="vm-tests", help="Local test directory (default: vm-tests)")
    test_parser.add_argument("--region", default="us-west-2", help="AWS region")
    test_parser.set_defaults(func=cmd_setup_upload_tests)

    # aws setup validate
    val_parser = setup_sub.add_parser(
        "validate",
        help="Validate AWS setup and tokens (read-only; use --fix to create missing resources)",
    )
    val_parser.add_argument("--bucket", help="S3 bucket to verify (and create with --fix)")
    val_parser.add_argument("--role", help="IAM role name used by VM instance profiles")
    val_parser.add_argument("--region", default="us-west-2", help="AWS region (default: us-west-2)")
    val_parser.add_argument("--api-url", help="KernelCI API base URI (overrides $KERNELCI_API_BASE_URI)")
    val_parser.add_argument(
        "--fix", action="store_true",
        help="Create missing resources (S3 bucket) instead of just reporting them",
    )
    val_parser.set_defaults(func=cmd_setup_validate)

    args = parser.parse_args()

    if not hasattr(args, "func"):
        # Show help for the deepest subparser reached
        if args.cloud == "aws" and args.command == "setup":
            setup_parser.print_help()
        elif args.cloud == "aws":
            aws_parser.print_help()
        else:
            parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
