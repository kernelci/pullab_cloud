"""Unit tests for nested virtualization support in launch_vm."""
# pylint: disable=protected-access

__authors__ = ["Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import patch, MagicMock

import pytest

from kernel_ci_cloud_labs.launch_vm import VMLauncher


@pytest.fixture
def _mock_boto3():
    """Patch boto3 so VMLauncher.__init__ doesn't need real AWS credentials."""
    with patch("kernel_ci_cloud_labs.launch_vm.boto3") as mock_boto:
        mock_boto.client.return_value = MagicMock()
        yield mock_boto


class TestNestedVirtualization:
    """Tests for _supports_nested_virtualization and CpuOptions."""

    def _make_launcher(self, instance_type, _mock_boto3):
        """Create a VMLauncher with a given instance_type, mocking AWS."""
        vm_config = {
            "instance_type": instance_type,
            "ami_id": "ami-test123",
            "role_name": "test-role",
            "s3_bucket": "test-bucket",
            "run_prefix": "run_test",
        }
        return VMLauncher(vm_config)

    def test_c8i_supports_nested(self, _mock_boto3):
        launcher = self._make_launcher("c8i.4xlarge", _mock_boto3)
        assert launcher._supports_nested_virtualization() is True

    def test_c8i_flex_supports_nested(self, _mock_boto3):
        launcher = self._make_launcher("c8i-flex.2xlarge", _mock_boto3)
        assert launcher._supports_nested_virtualization() is True

    def test_m8i_supports_nested(self, _mock_boto3):
        launcher = self._make_launcher("m8i.large", _mock_boto3)
        assert launcher._supports_nested_virtualization() is True

    def test_r8i_supports_nested(self, _mock_boto3):
        launcher = self._make_launcher("r8i.xlarge", _mock_boto3)
        assert launcher._supports_nested_virtualization() is True

    def test_c7i_supports_nested(self, _mock_boto3):
        launcher = self._make_launcher("c7i.2xlarge", _mock_boto3)
        assert launcher._supports_nested_virtualization() is True

    def test_m7i_supports_nested(self, _mock_boto3):
        launcher = self._make_launcher("m7i.xlarge", _mock_boto3)
        assert launcher._supports_nested_virtualization() is True

    def test_x8i_supports_nested(self, _mock_boto3):
        launcher = self._make_launcher("x8i.large", _mock_boto3)
        assert launcher._supports_nested_virtualization() is True

    def test_i7i_supports_nested(self, _mock_boto3):
        launcher = self._make_launcher("i7i.xlarge", _mock_boto3)
        assert launcher._supports_nested_virtualization() is True

    def test_c5a_does_not_support_nested(self, _mock_boto3):
        launcher = self._make_launcher("c5a.4xlarge", _mock_boto3)
        assert launcher._supports_nested_virtualization() is False

    def test_c6g_does_not_support_nested(self, _mock_boto3):
        launcher = self._make_launcher("c6g.4xlarge", _mock_boto3)
        assert launcher._supports_nested_virtualization() is False

    def test_t3_does_not_support_nested(self, _mock_boto3):
        launcher = self._make_launcher("t3.micro", _mock_boto3)
        assert launcher._supports_nested_virtualization() is False

    def test_m5_does_not_support_nested(self, _mock_boto3):
        launcher = self._make_launcher("m5.large", _mock_boto3)
        assert launcher._supports_nested_virtualization() is False
