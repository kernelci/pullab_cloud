"""Unit tests for the aws-run --results-dir download helper."""

__authors__ = ["Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

import logging
import os
from types import SimpleNamespace
from unittest.mock import Mock

from kernel_ci_cloud_labs.cli import _download_run_results

logger = logging.getLogger("test")


def _make_storage(keys):
    """Build a storage stub whose paginator yields the given S3 keys."""
    s3 = Mock()
    paginator = Mock()
    paginator.paginate.return_value = [
        {"Contents": [{"Key": k} for k in keys]}
    ]
    s3.get_paginator.return_value = paginator

    written = {}

    def _download_file(_bucket, key, local_path):
        # Record the mapping and create the file so structure can be asserted.
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(key)
        written[key] = local_path

    s3.download_file.side_effect = _download_file
    storage = SimpleNamespace(bucket="results-bkt", run_prefix="run_x_123", s3=s3)
    return storage, written


def test_download_preserves_structure_under_run_prefix(tmp_path):
    keys = [
        "run_x_123/test_pgbench/output/i-1/benchmark-base-k.csv",
        "run_x_123/test_pgbench/output/i-1/result.txt",
        "run_x_123/summary.json",
    ]
    storage, written = _make_storage(keys)
    _download_run_results(storage, str(tmp_path), logger)

    # Each object lands under <results_dir>/<run_prefix>/<rest-of-key>.
    assert (tmp_path / "run_x_123" / "test_pgbench" / "output" / "i-1" / "benchmark-base-k.csv").is_file()
    assert (tmp_path / "run_x_123" / "test_pgbench" / "output" / "i-1" / "result.txt").is_file()
    assert (tmp_path / "run_x_123" / "summary.json").is_file()
    assert len(written) == 3


def test_download_skips_folder_placeholder_keys(tmp_path):
    keys = ["run_x_123/", "run_x_123/summary.json"]
    storage, written = _make_storage(keys)
    _download_run_results(storage, str(tmp_path), logger)
    # The "directory" key is skipped; only the real object is downloaded.
    assert len(written) == 1
    assert (tmp_path / "run_x_123" / "summary.json").is_file()


def test_download_noop_when_storage_incomplete(tmp_path):
    storage = SimpleNamespace(bucket=None, run_prefix=None, s3=None)
    # Should not raise, just warn and return.
    _download_run_results(storage, str(tmp_path), logger)
    assert not any(tmp_path.iterdir())
