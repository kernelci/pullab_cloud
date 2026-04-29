"""Unit tests for provider lifecycle"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from unittest.mock import Mock

import pytest

from kernel_ci_cloud_labs.providers.aws_provider import AWSProvider


class TestAWSProviderLifecycle:
    """Tests for AWS provider lifecycle operations"""

    def test_spawn_container_returns_task_arn(self):
        """Test spawn_container returns valid task ARN"""
        mock_auth = Mock()
        mock_ecs = Mock()
        mock_auth.get_client.return_value = mock_ecs
        mock_auth.get_network_config.return_value = {
            "awsvpcConfiguration": {"subnets": ["subnet-1"], "securityGroups": ["sg-1"]}
        }
        mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/cluster/abc"}],
            "failures": [],
        }

        config = {"ecs": {"cluster_name": "test-cluster", "task_definition": {"family": "test-task"}}}

        provider = AWSProvider(mock_auth, config)
        provider.authenticate()
        task_arn = provider.spawn_container()

        assert "arn:aws:ecs" in task_arn
        mock_ecs.run_task.assert_called_once()

    def test_spawn_container_handles_failures(self):
        """Test spawn_container handles task launch failures"""
        mock_auth = Mock()
        mock_ecs = Mock()
        mock_auth.get_client.return_value = mock_ecs
        mock_auth.get_network_config.return_value = {
            "awsvpcConfiguration": {"subnets": ["subnet-1"], "securityGroups": ["sg-1"]}
        }
        mock_ecs.run_task.return_value = {"tasks": [], "failures": [{"reason": "RESOURCE:CPU"}]}

        config = {"ecs": {"cluster_name": "test-cluster", "task_definition": {"family": "test-task"}}}

        provider = AWSProvider(mock_auth, config)
        provider.authenticate()

        with pytest.raises(Exception):
            provider.spawn_container()

    def test_terminate_container_stops_task(self):
        """Test terminate_container stops specific task"""
        mock_auth = Mock()
        mock_ecs = Mock()
        mock_auth.get_client.return_value = mock_ecs

        config = {"ecs": {"cluster_name": "test-cluster", "task_definition": {"family": "test-task"}}}

        provider = AWSProvider(mock_auth, config)
        provider.authenticate()
        provider.terminate_container("arn:aws:ecs:us-west-2:123:task/cluster/abc")

        mock_ecs.stop_task.assert_called_once()

    def test_stop_all_tasks_stops_running_tasks(self):
        """Test stop_all_tasks stops all running tasks"""
        mock_auth = Mock()
        mock_ecs = Mock()
        mock_auth.get_client.return_value = mock_ecs
        mock_ecs.list_tasks.return_value = {"taskArns": ["task1", "task2"]}

        config = {"ecs": {"cluster_name": "test-cluster", "task_definition": {"family": "test-task"}}}

        provider = AWSProvider(mock_auth, config)
        provider.authenticate()
        provider.stop_all_tasks()

        assert mock_ecs.stop_task.call_count == 2
