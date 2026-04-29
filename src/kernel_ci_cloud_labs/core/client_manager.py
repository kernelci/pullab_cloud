"""Auto-refreshing client manager for AWS services"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import time
from typing import Callable


class ClientManager:
    """Manages boto3 clients with automatic refresh"""

    def __init__(self, client_factory: Callable, refresh_interval: int = 59):
        self._client_factory = client_factory
        self._refresh_interval = refresh_interval
        self._clients = {}
        self._timestamps = {}

    def get_client(self, service_name: str):
        """Get or refresh client for specified service"""
        now = time.time()

        if service_name not in self._clients or now - self._timestamps.get(service_name, 0) > self._refresh_interval:
            self._clients[service_name] = self._client_factory(service_name)
            self._timestamps[service_name] = now

        return self._clients[service_name]
