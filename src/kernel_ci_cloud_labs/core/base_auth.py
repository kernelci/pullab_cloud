"""Base authentication interface for cloud providers."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


# core/base_auth.py
from abc import ABC, abstractmethod


class BaseAuth(ABC):
    """Abstract base class for authentication handlers."""

    @abstractmethod
    def _check_credentials(self) -> bool:
        """
        Check if credentials are configured and valid.
        """

    @abstractmethod
    def authenticate(self):
        """Perform authentication logic. Must raise an exception if it fails."""

    @abstractmethod
    def get_credentials(self):
        """Return credentials or token for downstream usage."""

    @abstractmethod
    def resources_were_created(self) -> bool:
        """Check if any resources were created during authentication."""
