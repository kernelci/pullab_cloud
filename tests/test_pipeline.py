"""Unit tests for pipeline execution"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from unittest.mock import MagicMock, Mock, patch

from kernel_ci_cloud_labs.core.pipeline import run_pipeline


def test_pipeline_calls_provider_methods():
    """Test that pipeline calls all required provider methods"""
    mock_provider = Mock()
    mock_provider.spawn_container.return_value = "arn:aws:ecs:us-west-2:123456789:task/cluster/abc123"
    mock_provider.wait_for_task_completion.return_value = {"status": "STOPPED"}
    mock_provider.terminate_container.return_value = None
    mock_provider.auth = Mock()
    mock_provider.auth.is_authenticated = False  # Trigger authenticate() call
    mock_ec2 = Mock()
    mock_ec2.describe_instances.return_value = {"Reservations": []}
    mock_provider.auth.get_client.return_value = mock_ec2
    mock_provider.config = {
        "ecs": {
            "task_definition": {"container_name": "test-container", "family": "test-task"},
            "cluster": "test",
        },
        "test_config": {
            "test_id": "test-001",
            "vms": [{"test": "basic-test", "min_count": 1}],
        },
        "run_prefix": "run_test-001_20251223_155704",
    }
    mock_storage = Mock()
    mock_storage.upload_tests.return_value = True
    mock_storage.upload_test_payload.return_value = True

    with patch("kernel_ci_cloud_labs.auth.aws_cloudwatch_manager.AWSCloudWatchManager"), patch(
        "kernel_ci_cloud_labs.core.pipeline.logger", MagicMock()
    ), patch("kernel_ci_cloud_labs.core.pipeline.create_summary"):
        run_pipeline(mock_provider, mock_storage, run_dir="/tmp/mock_test_run")

    mock_storage.upload_tests.assert_called_once()
    mock_provider.authenticate.assert_called_once()
    mock_provider.spawn_container.assert_called_once()
    mock_provider.wait_for_task_completion.assert_called_once()
    mock_provider.terminate_container.assert_called_once()


def test_pipeline_saves_results():
    """Test that pipeline saves results to storage"""
    mock_provider = Mock()
    mock_provider.spawn_container.return_value = "arn:aws:ecs:us-west-2:123456789:task/cluster/abc123"
    mock_provider.wait_for_task_completion.return_value = {"status": "STOPPED"}
    mock_provider.terminate_container.return_value = None
    mock_provider.auth = Mock()
    mock_provider.auth.is_authenticated = False  # Trigger authenticate() call
    mock_ec2 = Mock()
    mock_ec2.describe_instances.return_value = {"Reservations": []}
    mock_provider.auth.get_client.return_value = mock_ec2
    mock_provider.config = {
        "ecs": {
            "task_definition": {"container_name": "test-container", "family": "test-task"},
            "cluster": "test",
        },
        "test_config": {
            "test_id": "test-001",
            "vms": [{"test": "basic-test", "min_count": 1}],
        },
        "run_prefix": "run_test-001_20251223_155704",
    }
    mock_storage = Mock()
    mock_storage.upload_tests.return_value = True
    mock_storage.upload_test_payload.return_value = True

    with patch("kernel_ci_cloud_labs.auth.aws_cloudwatch_manager.AWSCloudWatchManager"), patch(
        "kernel_ci_cloud_labs.core.pipeline.logger", MagicMock()
    ), patch("kernel_ci_cloud_labs.core.pipeline.create_summary"):
        run_pipeline(mock_provider, mock_storage, run_dir="/tmp/mock_test_run")

    mock_storage.save_results.assert_called_once()
