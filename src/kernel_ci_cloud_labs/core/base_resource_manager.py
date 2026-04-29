"""Base class for managing cloud resources with check/create pattern"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from abc import ABC, abstractmethod
from typing import Any, Dict

from kernel_ci_cloud_labs.core.logging_config import get_logger

logger = get_logger(__name__)


class BaseResourceManager(ABC):
    """
    Abstract base class for resource managers that follow the pattern:
    1. Check if resource exists
    2. Create if missing
    3. Return resource identifier
    """

    def __init__(self, client, config: Dict[str, Any]):
        """
        Initialize resource manager.

        Args:
            client: Cloud provider client (e.g., boto3 client)
            config: Configuration dictionary for resources
        """
        self.client = client
        self.config = config

    @abstractmethod
    def check_exists(self, resource_name: str) -> bool:
        """
        Check if a resource exists.

        Args:
            resource_name: Name/ID of the resource

        Returns:
            True if exists, False otherwise
        """

    @abstractmethod
    def create(self, resource_name: str, resource_config: Dict[str, Any]) -> str:
        """
        Create a resource.

        Args:
            resource_name: Name for the resource
            resource_config: Configuration for the resource

        Returns:
            Resource identifier (ARN, ID, etc.)
        """

    @abstractmethod
    def get_identifier(self, resource_name: str) -> str:
        """
        Get the identifier for an existing resource.

        Args:
            resource_name: Name of the resource

        Returns:
            Resource identifier (ARN, ID, etc.)
        """

    def ensure_exists(
        self,
        resource_name: str,
        resource_config: Dict[str, Any] = None,
        force_recreate: bool = False,
    ) -> tuple:
        """
        Ensure resource exists, create if missing.

        Args:
            resource_name: Name of the resource
            resource_config: Configuration (uses self.config if not provided)
            force_recreate: Delete and recreate even if exists

        Returns:
            Tuple of (resource_identifier, was_created)
        """
        if force_recreate and self.check_exists(resource_name):
            logger.info("[%s] Force recreate: deleting %s", self.__class__.__name__, resource_name)
            if hasattr(self, "delete_role"):
                self.delete_role(resource_name)

        if self.check_exists(resource_name):
            logger.info("[%s] ✓ Resource exists: %s", self.__class__.__name__, resource_name)
            return self.get_identifier(resource_name), False

        logger.info("[%s] Creating resource: %s", self.__class__.__name__, resource_name)
        config = resource_config or self.config.get(resource_name, {})
        identifier = self.create(resource_name, config)
        logger.info("[%s] ✓ Created: %s", self.__class__.__name__, resource_name)
        return identifier, True
