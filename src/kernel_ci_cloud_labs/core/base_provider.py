"""Base provider interface for cloud infrastructure."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from abc import ABC, abstractmethod


class BaseProvider(ABC):
    """Abstract base class for cloud provider implementations."""

    def __init__(self, auth=None):
        self.auth = auth

    @abstractmethod
    def authenticate(self):
        """Authenticate with the cloud provider."""

    @abstractmethod
    def spawn_container(self):
        """Spawn a container/task on the cloud provider."""

    @abstractmethod
    def stop_all_tasks(self):
        """Stop all running tasks/containers"""
