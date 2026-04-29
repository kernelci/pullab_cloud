"""Integration tests for resource managers"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from unittest.mock import Mock

from kernel_ci_cloud_labs.auth.aws_cloudwatch_manager import AWSCloudWatchManager
from kernel_ci_cloud_labs.auth.aws_cluster_manager import AWSClusterManager
from kernel_ci_cloud_labs.auth.aws_task_definition_manager import (
    AWSTaskDefinitionManager,
)


class TestCloudWatchManager:
    """Tests for CloudWatch log group manager"""

    def test_ensure_exists_creates_missing_log_group(self):
        """Test that ensure_exists creates log group when missing"""
        mock_client = Mock()
        mock_client.describe_log_groups.return_value = {"logGroups": []}

        manager = AWSCloudWatchManager(mock_client, {})
        _result, created = manager.ensure_exists("/ecs/test-logs", {"retention_days": 7})

        assert created is True
        mock_client.create_log_group.assert_called_once()
        mock_client.put_retention_policy.assert_called_once()

    def test_ensure_exists_skips_existing_log_group(self):
        """Test that ensure_exists skips creation for existing log group"""
        mock_client = Mock()
        mock_client.describe_log_groups.return_value = {"logGroups": [{"logGroupName": "/ecs/test-logs"}]}

        manager = AWSCloudWatchManager(mock_client, {})
        result, created = manager.ensure_exists("/ecs/test-logs", {})

        assert result == "/ecs/test-logs"
        assert created is False
        mock_client.create_log_group.assert_not_called()

    def test_get_logs_returns_messages(self):
        """Test retrieving log messages"""
        mock_client = Mock()
        mock_client.get_log_events.return_value = {
            "events": [
                {"message": "Log 1", "timestamp": 1000},
                {"message": "Log 2", "timestamp": 2000},
            ]
        }

        manager = AWSCloudWatchManager(mock_client, {})
        logs = manager.get_logs("/ecs/test", "stream1")

        assert len(logs) == 2
        assert logs[0]["message"] == "Log 1"
        assert logs[1]["message"] == "Log 2"

    def test_list_log_streams(self):
        """Test listing log streams"""
        mock_client = Mock()
        mock_client.describe_log_streams.return_value = {
            "logStreams": [{"logStreamName": "stream1"}, {"logStreamName": "stream2"}]
        }

        manager = AWSCloudWatchManager(mock_client, {})
        streams = manager.list_log_streams("/ecs/test")

        assert len(streams) == 2
        assert "stream1" in streams


class TestClusterManager:
    """Tests for ECS cluster manager"""

    def test_ensure_exists_creates_missing_cluster(self):
        """Test that ensure_exists creates cluster when missing"""
        mock_client = Mock()
        mock_client.describe_clusters.return_value = {
            "clusters": [],
            "failures": [{"arn": "test-cluster"}],
        }
        mock_client.create_cluster.return_value = {"cluster": {"clusterArn": "arn:aws:ecs:us-west-2:123:cluster/test"}}

        manager = AWSClusterManager(mock_client, {})
        result, created = manager.ensure_exists("test-cluster", {})

        assert created is True
        mock_client.create_cluster.assert_called_once_with(clusterName="test-cluster")
        assert "arn:aws:ecs" in result

    def test_ensure_exists_skips_existing_cluster(self):
        """Test that ensure_exists skips creation for existing cluster"""
        mock_client = Mock()
        mock_client.describe_clusters.return_value = {
            "clusters": [
                {
                    "clusterName": "test-cluster",
                    "clusterArn": "arn:aws:ecs:us-west-2:123:cluster/test",
                    "status": "ACTIVE",
                }
            ],
            "failures": [],
        }

        manager = AWSClusterManager(mock_client, {})
        result, created = manager.ensure_exists("test-cluster", {})

        assert created is False
        mock_client.create_cluster.assert_not_called()
        assert "arn:aws:ecs" in result


class TestTaskDefinitionManager:
    """Tests for ECS task definition manager"""

    def test_ensure_exists_creates_missing_task_definition(self):
        """Test that ensure_exists registers task definition when missing"""
        mock_client = Mock()
        mock_client.describe_task_definition.side_effect = Exception("Not found")
        mock_client.register_task_definition.return_value = {
            "taskDefinition": {"taskDefinitionArn": "arn:aws:ecs:us-west-2:123:task-definition/test:1"}
        }

        config = {
            "family": "test-task",
            "cpu": "256",
            "memory": "512",
            "execution_role_arn": "arn:aws:iam::123:role/exec",
            "task_role_arn": "arn:aws:iam::123:role/task",
            "container_name": "test-container",
            "image": "ubuntu:latest",
            "command": ["echo", "test"],
            "log_group": "/ecs/test",
        }

        manager = AWSTaskDefinitionManager(mock_client, {})
        result, created = manager.ensure_exists("test-task", config)

        assert created is True
        mock_client.register_task_definition.assert_called_once()
        assert "arn:aws:ecs" in result

    def test_ensure_exists_skips_existing_task_definition(self):
        """Test that ensure_exists skips registration for existing task definition"""
        mock_client = Mock()
        mock_client.describe_task_definition.return_value = {
            "taskDefinition": {
                "family": "test-task",
                "taskDefinitionArn": "arn:aws:ecs:us-west-2:123:task-definition/test:1",
                "status": "ACTIVE",
            }
        }

        manager = AWSTaskDefinitionManager(mock_client, {})
        result, created = manager.ensure_exists("test-task", {})

        assert created is False
        mock_client.register_task_definition.assert_not_called()
        assert "arn:aws:ecs" in result

    def test_task_definition_includes_cloudwatch_logs(self):
        """Test that registered task definition includes CloudWatch logs config"""
        mock_client = Mock()
        mock_client.describe_task_definition.side_effect = Exception("Not found")
        mock_client.register_task_definition.return_value = {
            "taskDefinition": {"taskDefinitionArn": "arn:aws:ecs:us-west-2:123:task-definition/test:1"}
        }

        config = {
            "family": "test-task",
            "cpu": "256",
            "memory": "512",
            "execution_role_arn": "arn:aws:iam::123:role/exec",
            "task_role_arn": "arn:aws:iam::123:role/task",
            "container_name": "test-container",
            "image": "ubuntu:latest",
            "command": ["echo", "test"],
            "log_group": "/ecs/test",
        }

        manager = AWSTaskDefinitionManager(mock_client, {})
        _result, _created = manager.ensure_exists("test-task", config)

        call_args = mock_client.register_task_definition.call_args
        assert call_args is not None
        kwargs = call_args[1]
        container_def = kwargs["containerDefinitions"][0]

        assert "logConfiguration" in container_def
        assert container_def["logConfiguration"]["logDriver"] == "awslogs"
        # Log group defaults to /ecs/{family} if not explicitly set
        assert "/ecs/" in container_def["logConfiguration"]["options"]["awslogs-group"]
