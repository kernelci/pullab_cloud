# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 Collabora Limited
# Author: Denys Fedoryshchenko <denys.f@collabora.com>

"""Unit tests for core.artifacts (boot-log collection + KCIDB manifest)."""

import json
from unittest.mock import MagicMock

from kernel_ci_cloud_labs.core.artifacts import (
    ARTIFACTS_MANIFEST_VERSION,
    _discover_instances,
    collect_run_artifacts,
    s3_public_url,
)


# Minimal fake S3 client. boto3 attaches an exceptions.NoSuchKey class to
# the client at construction; the artifacts module references that exact
# class, so we have to expose it on the mock the same way.
class _NoSuchKey(Exception):
    pass


def _make_s3_mock(*, listings=None, objects=None):
    """Build a MagicMock S3 client.

    Args:
        listings: dict keyed by (Prefix, Delimiter) → list_objects_v2 response.
        objects: dict keyed by S3 key → bytes for get_object. Missing keys
            raise NoSuchKey.
    """
    listings = listings or {}
    objects = objects or {}

    s3 = MagicMock()
    s3.exceptions.NoSuchKey = _NoSuchKey

    def fake_list(Bucket, Prefix, Delimiter=None):  # pylint: disable=invalid-name,unused-argument
        return listings.get((Prefix, Delimiter), {"CommonPrefixes": [], "Contents": []})

    def fake_get(Bucket, Key):  # pylint: disable=invalid-name,unused-argument
        if Key not in objects:
            raise _NoSuchKey(Key)
        body = MagicMock()
        body.read.return_value = objects[Key]
        return {"Body": body}

    s3.list_objects_v2.side_effect = fake_list
    s3.get_object.side_effect = fake_get
    return s3


def test_s3_public_url_shape():
    """Public URL is virtual-hosted style with the region path segment."""
    url = s3_public_url("kernel-ci-bucket", "us-west-2", "run_x/foo.log")
    assert url == "https://kernel-ci-bucket.s3.us-west-2.amazonaws.com/run_x/foo.log"


def test_discover_instances_filters_non_test_dirs():
    """Only ``test_<name>/`` subdirectories are walked; stray files ignored."""
    s3 = _make_s3_mock(
        listings={
            ("run_abc/", "/"): {
                "CommonPrefixes": [
                    {"Prefix": "run_abc/test_baseline/"},
                    {"Prefix": "run_abc/test_unixbench/"},
                    # Stray non-test directory (e.g. the TEST_CONFIG dir).
                    {"Prefix": "run_abc/_config/"},
                ],
            },
            ("run_abc/test_baseline/output/", "/"): {
                "CommonPrefixes": [
                    {"Prefix": "run_abc/test_baseline/output/i-aaaa1111/"},
                ],
            },
            ("run_abc/test_unixbench/output/", "/"): {
                "CommonPrefixes": [
                    {"Prefix": "run_abc/test_unixbench/output/i-bbbb2222/"},
                    {"Prefix": "run_abc/test_unixbench/output/i-cccc3333/"},
                ],
            },
            ("run_abc/_config/output/", "/"): {"CommonPrefixes": []},
        }
    )

    pairs = sorted(_discover_instances(s3, "bucket", "run_abc"))
    assert pairs == [
        ("baseline", "i-aaaa1111"),
        ("unixbench", "i-bbbb2222"),
        ("unixbench", "i-cccc3333"),
    ]


def test_collect_run_artifacts_ready_and_missing(tmp_path):
    """End-to-end: one instance has a console log, another does not."""
    # i-aaaa1111 has a buffer in S3; i-bbbb2222 was terminated before upload.
    console_bytes = b"[    0.000000] Linux version 6.10 ...\n"
    s3 = _make_s3_mock(
        listings={
            ("run_xy/", "/"): {
                "CommonPrefixes": [{"Prefix": "run_xy/test_boot/"}],
            },
            ("run_xy/test_boot/output/", "/"): {
                "CommonPrefixes": [
                    {"Prefix": "run_xy/test_boot/output/i-aaaa1111/"},
                    {"Prefix": "run_xy/test_boot/output/i-bbbb2222/"},
                ],
            },
        },
        objects={
            "run_xy/test_boot/output/i-aaaa1111/console-output.log": console_bytes,
            # i-bbbb2222/console-output.log intentionally absent → NoSuchKey
        },
    )

    manifest = collect_run_artifacts(
        tmp_path,
        s3_client=s3,
        bucket="my-bucket",
        region="eu-west-1",
        run_prefix="run_xy",
        origin="myorigin",
    )

    # On-disk manifest exists and matches the returned dict.
    on_disk = json.loads((tmp_path / "artifacts.json").read_text())
    assert on_disk == manifest

    assert manifest["schema_version"] == ARTIFACTS_MANIFEST_VERSION
    assert manifest["s3_bucket"] == "my-bucket"
    assert manifest["origin"] == "myorigin"
    assert manifest["run_prefix"] == "run_xy"

    by_id = {e["instance_id"]: e for e in manifest["artifacts"]}
    assert set(by_id) == {"i-aaaa1111", "i-bbbb2222"}

    ready = by_id["i-aaaa1111"]
    assert ready["status"] == "ready"
    assert ready["kcidb_role"] == "log"
    assert ready["size_bytes"] == len(console_bytes)
    # sha256 of known bytes is deterministic — assert it's a 64-char hex.
    assert ready["sha256"] is not None and len(ready["sha256"]) == 64
    assert ready["log_url"] == (
        "https://my-bucket.s3.eu-west-1.amazonaws.com/"
        "run_xy/test_boot/output/i-aaaa1111/console-output.log"
    )
    # Local file actually written.
    local = tmp_path / ready["local_path"]
    assert local.read_bytes() == console_bytes

    missing = by_id["i-bbbb2222"]
    assert missing["status"] == "missing"
    assert missing["log_url"] is None
    assert missing["local_path"] is None
    assert missing["sha256"] is None
    assert missing["size_bytes"] == 0


def test_collect_run_artifacts_empty_run(tmp_path):
    """Run prefix with no test_* subdirs produces an empty (but valid) manifest."""
    s3 = _make_s3_mock(listings={("run_empty/", "/"): {"CommonPrefixes": []}})

    manifest = collect_run_artifacts(
        tmp_path,
        s3_client=s3,
        bucket="b",
        region="us-east-1",
        run_prefix="run_empty",
    )
    assert manifest["artifacts"] == []
    assert (tmp_path / "artifacts.json").exists()
