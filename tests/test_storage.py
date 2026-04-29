"""Unit tests for storage backends"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import logging
import os
import tempfile
import zipfile
from unittest.mock import Mock, patch

from kernel_ci_cloud_labs.storage.s3_storage import S3Storage


class TestS3Storage:
    """Test S3 storage backend"""

    @patch("kernel_ci_cloud_labs.storage.s3_storage.boto3")
    def test_save_results_prints_output(self, mock_boto3, caplog):
        """Test that save_results logs output"""
        caplog.set_level(logging.INFO)
        mock_s3 = Mock()
        mock_s3.head_bucket.return_value = {}
        mock_boto3.client.return_value = mock_s3

        storage = S3Storage({"bucket": "test-bucket"})
        data = {"status": "success", "task_id": "123"}

        storage.save_results(data)

        assert "Saving" in caplog.text
        assert "success" in caplog.text

    @patch("kernel_ci_cloud_labs.storage.s3_storage.boto3")
    def test_upload_test_payload(self, mock_boto3, tmp_path, caplog):
        """Test that upload_test_payload creates and uploads zip"""
        caplog.set_level(logging.INFO)
        mock_s3 = Mock()
        mock_s3.head_bucket.return_value = {}
        # Mock head_object to raise exception (file doesn't exist)
        from botocore.exceptions import ClientError

        mock_s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        mock_boto3.client.return_value = mock_s3

        # Create test directory structure
        test_dir = tmp_path / "vm-tests"
        test_dir.mkdir()
        test_path = test_dir / "example-test"
        test_path.mkdir()
        (test_path / "run.sh").write_text("#!/bin/bash\necho test")
        (test_path / "README.md").write_text("Test readme")

        storage = S3Storage({"bucket": "test-bucket"})
        storage.set_run_prefix("test_run_123")
        result = storage.upload_test_payload("example-test", str(test_dir))

        assert result is True
        assert "Creating test payload" in caplog.text
        assert "Uploaded test payload" in caplog.text
        mock_s3.put_object.assert_called()

        # Verify the uploaded content is a valid zip
        call_args = mock_s3.put_object.call_args
        assert call_args[1]["Key"] == "test_run_123/test_example-test/input/example-test_test_payload.zip"
        zip_content = call_args[1]["Body"]

        # Verify zip contains expected files
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(zip_content)
            tmp_zip = tmp.name

        try:
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                names = zf.namelist()
                assert "run.sh" in names
                assert "README.md" in names
        finally:
            os.remove(tmp_zip)
