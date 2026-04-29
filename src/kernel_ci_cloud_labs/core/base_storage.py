"""Base storage interface for test results."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from abc import ABC, abstractmethod


class BaseStorage(ABC):
    """Abstract base class for storage backend implementations."""

    @abstractmethod
    def save_results(self, data: dict):
        """Save test results to storage backend."""

    @abstractmethod
    def upload_tests(
        self,
        test_name: str,
        test_id: str,
        test_dir: str = "vm-tests",
        script_name: str = "test-vm-client.sh",
    ) -> bool:
        """Upload test script from test_dir to storage backend.

        Returns True if upload successful, False otherwise.
        """
