"""Validate AWS setup, KernelCI/KCIDB tokens, and optionally fix missing resources.

Wired into the CLI as `aws setup validate [--fix]`. Read-only by default;
`--fix` is allowed to create missing resources (currently: the S3 bucket).

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
SPDX-License-Identifier: Apache-2.0
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from kernel_ci_cloud_labs.debug_aws_setup import (
    check_aws_credentials,
    check_ec2_permissions,
    check_iam_role,
    check_instance_profile,
    check_ssm_permissions,
)


# Env var names mirror the ones used by pull_labs_poller. Kept here as plain
# strings rather than imported to avoid a circular dependency on the poller.
ENV_API_BASE_URI = "KERNELCI_API_BASE_URI"
ENV_API_TOKEN = "KERNELCI_API_TOKEN"
ENV_KCIDB_URL = "KCIDB_SUBMIT_URL"
ENV_KCIDB_JWT = "KCIDB_JWT"
ENV_KCIDB_REST = "KCIDB_REST"
ENV_UNIFIED_TOKEN = "UNIFIED_TOKEN"


def check_s3_bucket(bucket_name: str, region: str, fix: bool = False) -> bool:
    """Check S3 bucket exists. With fix=True, create it if missing."""
    print(f"\n=== Checking S3 Bucket: {bucket_name} ===")
    s3 = boto3.client("s3", region_name=region)
    try:
        s3.head_bucket(Bucket=bucket_name)
        print(f"✓ S3 bucket accessible: {bucket_name}")
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # 404 = bucket does not exist; 403 = exists but no access (different owner)
        if code in ("404", "NoSuchBucket"):
            print(f"✗ S3 bucket does not exist: {bucket_name}")
            if not fix:
                print("  (pass --fix to create)")
                return False
            return _create_s3_bucket(s3, bucket_name, region)
        print(f"✗ S3 bucket check failed ({code}): {e}")
        return False


def _create_s3_bucket(s3, bucket_name: str, region: str) -> bool:
    """Create an S3 bucket honoring region constraints (us-east-1 quirk)."""
    print(f"  Creating bucket in {region}...")
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        # Block public ACLs (modern best practice) but allow bucket
        # *policies* to grant access — kernel boot logs published to KCIDB
        # are made public-readable via a narrow bucket-policy statement
        # added by check_s3_logs_public_policy(). Without these two flags
        # set to False, that policy would be rejected by AWS.
        s3.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            },
        )
        print(f"✓ Created S3 bucket: {bucket_name}")
        return True
    except ClientError as e:
        print(f"✗ Failed to create bucket: {e}")
        return False


# Resource pattern allowed by the public-read bucket policy. We expose
# *only* kernel boot console logs (one file per VM instance per test),
# so the rest of the bucket — test payloads, results.txt, stats.json,
# benchmark CSVs — stays private. The pattern matches the layout written
# by `launch_vm.capture_console_output`:
#   <run_prefix>/test_<name>/output/<instance_id>/console-output.log
_PUBLIC_LOGS_KEY_PATTERN = "*/test_*/output/*/console-output.log"
_PUBLIC_LOGS_SID = "PublicReadKernelBootLogs"


def _expected_bucket_policy(bucket_name: str) -> dict:
    """Bucket policy that grants anonymous GET on the boot-log prefix only."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": _PUBLIC_LOGS_SID,
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket_name}/{_PUBLIC_LOGS_KEY_PATTERN}",
            }
        ],
    }


def check_s3_logs_public_policy(bucket_name: str, region: str, fix: bool = False) -> bool:
    """Verify the bucket allows anonymous GET on kernel boot logs.

    Two preconditions:
      1. The bucket's PublicAccessBlock must permit bucket policies
         (BlockPublicPolicy=False, RestrictPublicBuckets=False). ACL-based
         public access stays blocked — we publish via bucket policy only.
      2. A statement with Sid='PublicReadKernelBootLogs' must grant
         s3:GetObject on the console-output.log pattern to Principal:*.

    With ``fix=True``, both preconditions are repaired:
      - PublicAccessBlock is rewritten with ACLs still blocked but bucket
        policies permitted;
      - the bucket policy is merged with the expected statement (existing
        statements are preserved; an existing statement with the same Sid
        is replaced).
    """
    print(f"\n=== Checking S3 logs public-read policy: {bucket_name} ===")
    s3 = boto3.client("s3", region_name=region)

    # --- PublicAccessBlock ----------------------------------------------------
    pab_ok = _check_public_access_block(s3, bucket_name, fix=fix)

    # --- Bucket policy --------------------------------------------------------
    policy_ok = _check_bucket_policy_statement(s3, bucket_name, fix=fix)

    return pab_ok and policy_ok


def _check_public_access_block(s3, bucket_name: str, fix: bool) -> bool:
    """Ensure BlockPublicPolicy / RestrictPublicBuckets are False."""
    desired = {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": False,
        "RestrictPublicBuckets": False,
    }
    try:
        current = s3.get_public_access_block(Bucket=bucket_name)[
            "PublicAccessBlockConfiguration"
        ]
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "NoSuchPublicAccessBlockConfiguration":
            print(f"✗ Could not read PublicAccessBlock ({code}): {e}")
            return False
        current = {}

    blockers = [k for k in ("BlockPublicPolicy", "RestrictPublicBuckets") if current.get(k, True)]
    if not blockers:
        print("✓ PublicAccessBlock permits bucket policies")
        return True

    print(f"✗ PublicAccessBlock blocks public policies: {', '.join(blockers)}=True")
    if not fix:
        print("  (pass --fix to relax; ACLs will stay blocked)")
        return False
    try:
        s3.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration=desired,
        )
        print("✓ PublicAccessBlock updated (ACLs still blocked)")
        return True
    except ClientError as e:
        print(f"✗ Failed to update PublicAccessBlock: {e}")
        return False


def _check_bucket_policy_statement(s3, bucket_name: str, fix: bool) -> bool:
    """Ensure the bucket policy has our PublicReadKernelBootLogs statement."""
    expected_stmt = _expected_bucket_policy(bucket_name)["Statement"][0]
    expected_resource = expected_stmt["Resource"]

    try:
        policy_str = s3.get_bucket_policy(Bucket=bucket_name)["Policy"]
        existing = json.loads(policy_str)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "NoSuchBucketPolicy":
            print(f"✗ Could not read bucket policy ({code}): {e}")
            return False
        existing = None
    except (ValueError, json.JSONDecodeError) as e:
        print(f"✗ Bucket policy is not valid JSON: {e}")
        return False

    statements = (existing or {}).get("Statement", [])
    match = next((s for s in statements if s.get("Sid") == _PUBLIC_LOGS_SID), None)
    if match and _statement_matches(match, expected_stmt):
        print(f"✓ Bucket policy grants public-read on {_PUBLIC_LOGS_KEY_PATTERN}")
        return True

    if match:
        print(f"✗ Statement '{_PUBLIC_LOGS_SID}' exists but does not match expected shape")
    else:
        print(f"✗ Statement '{_PUBLIC_LOGS_SID}' missing from bucket policy")
    print(f"  Expected Resource: {expected_resource}")
    if not fix:
        print("  (pass --fix to add/replace the statement)")
        return False

    # Merge: drop any existing statement with our Sid, append the expected one.
    new_statements = [s for s in statements if s.get("Sid") != _PUBLIC_LOGS_SID]
    new_statements.append(expected_stmt)
    new_policy = {
        "Version": (existing or {}).get("Version", "2012-10-17"),
        "Statement": new_statements,
    }
    try:
        s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(new_policy))
        print("✓ Bucket policy updated with PublicReadKernelBootLogs statement")
        return True
    except ClientError as e:
        print(f"✗ Failed to update bucket policy: {e}")
        return False


def _statement_matches(actual: dict, expected: dict) -> bool:
    """Compare the fields we care about; tolerate extra keys AWS may add."""
    if actual.get("Effect") != expected["Effect"]:
        return False
    if actual.get("Principal") != expected["Principal"]:
        return False
    # Action and Resource may come back as strings or single-item lists.
    if _as_set(actual.get("Action")) != _as_set(expected["Action"]):
        return False
    if _as_set(actual.get("Resource")) != _as_set(expected["Resource"]):
        return False
    return True


def _as_set(value) -> set:
    if value is None:
        return set()
    if isinstance(value, list):
        return set(value)
    return {value}


def check_console_output_permission(region: Optional[str] = None) -> bool:
    """Probe ec2:GetConsoleOutput. Uses a non-existent instance id so the only
    permission we test is the IAM action itself."""
    print("\n=== Checking ec2:GetConsoleOutput permission ===")
    ec2 = boto3.client("ec2", region_name=region)
    try:
        ec2.get_console_output(InstanceId="i-0000000000000000f")
        print("✓ Call accepted (unexpected; permission OK)")
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "UnauthorizedOperation" or "AccessDenied" in str(e):
            print(f"✗ Missing ec2:GetConsoleOutput permission ({code})")
            return False
        # InvalidInstanceID.NotFound / Malformed means the call was authorized.
        print(f"✓ ec2:GetConsoleOutput permitted (saw expected {code or 'error'})")
        return True


def check_kernelci_api_token(api_base_uri: Optional[str] = None,
                             token: Optional[str] = None) -> bool:
    """Probe the KernelCI API with the configured token."""
    print("\n=== Checking KernelCI API token ===")

    api_base_uri = api_base_uri or os.getenv(ENV_API_BASE_URI)
    token = token or os.getenv(ENV_API_TOKEN) or os.getenv(ENV_UNIFIED_TOKEN)

    if not api_base_uri:
        print(f"✗ {ENV_API_BASE_URI} not set; cannot probe")
        return False
    if not token:
        print(f"✗ Neither {ENV_API_TOKEN} nor {ENV_UNIFIED_TOKEN} is set")
        return False

    base = api_base_uri.rstrip("/")
    whoami_url = f"{base}/whoami"
    print(f"  GET {whoami_url}")
    req = urllib.request.Request(
        whoami_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(f"✓ API accepted token (HTTP {resp.status})")
            try:
                data = json.loads(body)
                user = data.get("username") or data.get("email") or data.get("id")
                if user:
                    print(f"  Authenticated as: {user}")
            except (json.JSONDecodeError, AttributeError):
                pass
            return True
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print(f"✗ Token rejected (HTTP {e.code})")
        else:
            print(f"✗ Unexpected HTTP {e.code} from {whoami_url}")
        return False
    except urllib.error.URLError as e:
        print(f"✗ Could not reach KernelCI API: {e.reason}")
        return False


def _parse_kcidb_rest(rest_url: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse https://<token>@<host>/path into (submit_url_without_token, token)."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(rest_url)
    token = parsed.username
    if not token:
        return rest_url, None
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    clean = urlunparse(parsed._replace(netloc=netloc))
    return clean, token


def check_kcidb_jwt() -> bool:
    """Decode the KCIDB JWT (no signature verification) and check expiry."""
    print("\n=== Checking KCIDB JWT ===")

    jwt = os.getenv(ENV_KCIDB_JWT)
    submit_url = os.getenv(ENV_KCIDB_URL)
    rest = os.getenv(ENV_KCIDB_REST)
    unified = os.getenv(ENV_UNIFIED_TOKEN)

    if not jwt and rest:
        submit_url, jwt = _parse_kcidb_rest(rest)
    if not jwt and unified:
        jwt = unified

    if not jwt:
        print(f"✗ No JWT found ({ENV_KCIDB_JWT}, {ENV_KCIDB_REST}, or {ENV_UNIFIED_TOKEN})")
        return False
    if submit_url:
        print(f"  Submit URL: {submit_url}")

    parts = jwt.split(".")
    if len(parts) != 3:
        print(f"✗ Token is not a JWT (expected 3 dot-separated parts, got {len(parts)})")
        return False

    try:
        # JWT payload is base64url; pad to a multiple of 4 before decoding.
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as e:
        print(f"✗ JWT payload not decodable: {e}")
        return False

    exp = payload.get("exp")
    if exp is None:
        print("✓ JWT decoded; no expiry claim")
    else:
        remaining = int(exp - time.time())
        if remaining <= 0:
            print(f"✗ JWT expired {-remaining}s ago")
            return False
        days = remaining // 86400
        print(f"✓ JWT valid for {days}d ({remaining}s remaining)")

    issuer = payload.get("iss")
    sub = payload.get("sub") or payload.get("email")
    if issuer:
        print(f"  Issuer: {issuer}")
    if sub:
        print(f"  Subject: {sub}")
    return True


def validate(bucket: Optional[str] = None,
             role_name: Optional[str] = None,
             region: str = "us-west-2",
             api_base_uri: Optional[str] = None,
             api_token: Optional[str] = None,
             fix: bool = False) -> int:
    """Run all validation checks; return process exit code."""
    print("=" * 60)
    print("Setup validation")
    print(f"  Region:  {region}")
    print(f"  Bucket:  {bucket or '(not provided)'}")
    print(f"  Role:    {role_name or '(not provided)'}")
    print(f"  Fix:     {'yes' if fix else 'no (read-only)'}")
    print("=" * 60)

    results = {}

    results["aws_credentials"] = check_aws_credentials()
    results["ec2_describe"] = check_ec2_permissions(region)
    results["ec2_console_output"] = check_console_output_permission(region)
    results["ssm"] = check_ssm_permissions(region)

    if role_name:
        results["iam_role"] = check_iam_role(role_name)
        results["instance_profile"] = check_instance_profile(role_name)

    if bucket:
        results["s3_bucket"] = check_s3_bucket(bucket, region, fix=fix)
        # Only check the policy if the bucket exists/was created — checking
        # against a missing bucket produces a confusing cascade of errors.
        if results["s3_bucket"]:
            results["s3_logs_public_policy"] = check_s3_logs_public_policy(
                bucket, region, fix=fix
            )

    results["kernelci_api_token"] = check_kernelci_api_token(api_base_uri, api_token)
    results["kcidb_jwt"] = check_kcidb_jwt()

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    for name, ok in results.items():
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")

    return 0 if all(results.values()) else 1
