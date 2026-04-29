"""Tests for AWS ECR Manager"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from unittest.mock import MagicMock

from kernel_ci_cloud_labs.auth.aws_ecr_manager import AWSECRManager


def test_check_exists_true():
    """Test check_exists returns True when repository exists"""
    mock_client = MagicMock()
    mock_client.describe_repositories.return_value = {"repositories": [{"repositoryName": "test-repo"}]}

    manager = AWSECRManager(mock_client, {})
    assert manager.check_exists("test-repo") is True


def test_check_exists_false():
    """Test check_exists returns False when repository not found"""
    mock_client = MagicMock()
    mock_client.exceptions.RepositoryNotFoundException = Exception
    mock_client.describe_repositories.side_effect = Exception()

    manager = AWSECRManager(mock_client, {})
    assert manager.check_exists("test-repo") is False


def test_create_repository():
    """Test create returns repository URI"""
    mock_client = MagicMock()
    mock_client.create_repository.return_value = {
        "repository": {"repositoryUri": "123456.dkr.ecr.us-west-2.amazonaws.com/test-repo"}
    }

    manager = AWSECRManager(mock_client, {})
    uri = manager.create("test-repo", {"scan_on_push": False})

    assert uri == "123456.dkr.ecr.us-west-2.amazonaws.com/test-repo"
    mock_client.create_repository.assert_called_once()


def test_get_identifier():
    """Test get_identifier returns repository URI"""
    mock_client = MagicMock()
    mock_client.describe_repositories.return_value = {
        "repositories": [{"repositoryUri": "123456.dkr.ecr.us-west-2.amazonaws.com/test-repo"}]
    }

    manager = AWSECRManager(mock_client, {})
    uri = manager.get_identifier("test-repo")

    assert uri == "123456.dkr.ecr.us-west-2.amazonaws.com/test-repo"


def test_ensure_exists_creates_when_missing():
    """Test ensure_exists creates repository when it doesn't exist"""
    mock_client = MagicMock()
    mock_client.exceptions.RepositoryNotFoundException = Exception
    mock_client.describe_repositories.side_effect = [
        Exception(),  # check_exists fails
        {
            "repositories": [{"repositoryUri": "123456.dkr.ecr.us-west-2.amazonaws.com/test-repo"}]
        },  # get_identifier succeeds
    ]
    mock_client.create_repository.return_value = {
        "repository": {"repositoryUri": "123456.dkr.ecr.us-west-2.amazonaws.com/test-repo"}
    }

    manager = AWSECRManager(mock_client, {})
    uri, created = manager.ensure_exists("test-repo", {"scan_on_push": False})

    assert created is True
    assert uri == "123456.dkr.ecr.us-west-2.amazonaws.com/test-repo"


def test_ensure_exists_skips_when_exists():
    """Test ensure_exists skips creation when repository exists"""
    mock_client = MagicMock()
    mock_client.describe_repositories.return_value = {
        "repositories": [{"repositoryUri": "123456.dkr.ecr.us-west-2.amazonaws.com/test-repo"}]
    }

    manager = AWSECRManager(mock_client, {})
    uri, created = manager.ensure_exists("test-repo", {"scan_on_push": False})

    assert created is False
    assert uri == "123456.dkr.ecr.us-west-2.amazonaws.com/test-repo"
    mock_client.create_repository.assert_not_called()
