"""Unit tests for AWS authentication"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


# pylint: disable=protected-access

from unittest.mock import Mock, patch

import pytest

from kernel_ci_cloud_labs.auth.aws_auth import AWSAuth


class TestAWSAuth:
    """Test AWS authentication functionality"""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config dict"""
        return {
            "provider": "aws",
            "region": "us-west-2",
            "storage": {"type": "s3"},
            "auth_credentials": {"auth_provider": "aws"},
        }

    @pytest.fixture
    def mock_credentials(self):
        """Create mock credentials dict"""
        return {"access_key_id": "EXAMPLE_KEY", "secret_access_key": "SECRET_EXAMPLE_KEY"}

    @patch("kernel_ci_cloud_labs.auth.aws_auth.subprocess.run")
    @patch("kernel_ci_cloud_labs.auth.aws_auth.boto3.Session")
    def test_authenticate_with_valid_credentials(self, mock_session, mock_subprocess, mock_config, mock_credentials):
        """Test authentication with valid AWS credentials"""
        # Mock credentials check to return success
        mock_subprocess.return_value = Mock(returncode=0)

        # Mock boto3 session
        mock_sts = Mock()
        mock_sts.get_caller_identity.return_value = {"Arn": "arn:aws:iam::123456789:user/test-user"}
        mock_session_instance = Mock()
        mock_session_instance.client.return_value = mock_sts
        mock_session.return_value = mock_session_instance

        auth = AWSAuth(mock_config, mock_credentials)
        result = auth.authenticate()

        assert result is True
        assert auth._session is not None

    @patch("kernel_ci_cloud_labs.auth.aws_auth.boto3.client")
    def test_check_credentials_valid(self, mock_boto_client, mock_config):
        """Test credential validation with valid credentials"""
        mock_sts = Mock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789"}
        mock_boto_client.return_value = mock_sts

        auth = AWSAuth.__new__(AWSAuth)
        auth.config_path = mock_config
        result = auth._check_credentials()

        assert result is True

    @patch("kernel_ci_cloud_labs.auth.aws_auth.boto3.client")
    def test_check_credentials_invalid(self, mock_boto_client, mock_config):
        """Test credential validation with invalid credentials"""
        import botocore.exceptions

        mock_sts = Mock()
        mock_sts.get_caller_identity.side_effect = botocore.exceptions.ClientError(
            {"Error": {"Code": "InvalidClientTokenId"}}, "GetCallerIdentity"
        )
        mock_boto_client.return_value = mock_sts

        auth = AWSAuth.__new__(AWSAuth)
        auth.config_path = mock_config
        result = auth._check_credentials()

        assert result is False

    @patch("kernel_ci_cloud_labs.auth.aws_auth.subprocess.run")
    @patch("kernel_ci_cloud_labs.auth.aws_auth.boto3.Session")
    def test_get_client(self, mock_session, mock_subprocess, mock_config, mock_credentials):
        """Test getting AWS service client"""
        mock_subprocess.return_value = Mock(returncode=0)

        mock_sts = Mock()
        mock_sts.get_caller_identity.return_value = {"Arn": "test-arn"}
        mock_s3 = Mock()

        mock_session_instance = Mock()
        mock_session_instance.client.side_effect = lambda service, **kwargs: (mock_sts if service == "sts" else mock_s3)
        mock_session.return_value = mock_session_instance

        auth = AWSAuth(mock_config, mock_credentials)
        client = auth.get_client("s3")

        assert client is not None

    @patch("kernel_ci_cloud_labs.auth.aws_auth.subprocess.run")
    @patch("kernel_ci_cloud_labs.auth.aws_auth.boto3.Session")
    def test_get_credentials(self, mock_session, mock_subprocess, mock_config, mock_credentials):
        """Test getting credentials dictionary"""
        mock_subprocess.return_value = Mock(returncode=0)

        mock_sts = Mock()
        mock_sts.get_caller_identity.return_value = {"Arn": "test-arn"}
        mock_session_instance = Mock()
        mock_session_instance.client.return_value = mock_sts
        mock_session.return_value = mock_session_instance

        auth = AWSAuth(mock_config, mock_credentials)
        creds = auth.get_credentials()

        assert "session" in creds
        assert "region" in creds
        assert creds["region"] == "us-west-2"
