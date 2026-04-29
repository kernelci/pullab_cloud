"""Unit tests for registry system"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from kernel_ci_cloud_labs.core.base_auth import BaseAuth
from kernel_ci_cloud_labs.core.base_provider import BaseProvider
from kernel_ci_cloud_labs.core.base_storage import BaseStorage
from kernel_ci_cloud_labs.core.registry import (
    get_auth,
    get_provider,
    get_storage,
    register_auth,
    register_provider,
    register_storage,
)


class TestRegistry:
    """Test registry system for providers, storage, and auth"""

    def test_register_and_get_provider(self):
        """Test registering and retrieving a provider"""

        @register_provider("test_provider")
        class TestProvider(BaseProvider):
            """Test provider for registry testing"""

            def authenticate(self):
                """Authenticate"""

            def spawn_container(self):
                """Spawn container"""

            def terminate_container(self, container_id):
                """Terminate container"""

            def stop_all_tasks(self):
                """Stop all tasks"""

        provider_class = get_provider("test_provider")
        assert provider_class == TestProvider

    def test_register_and_get_storage(self):
        """Test registering and retrieving a storage backend"""

        @register_storage("test_storage")
        class TestStorage(BaseStorage):
            """Test storage for registry testing"""

            def save_results(self, data):
                """Save results"""

        storage_class = get_storage("test_storage")
        assert storage_class == TestStorage

    def test_register_and_get_auth(self):
        """Test registering and retrieving an auth provider"""

        @register_auth("test_auth")
        class TestAuth(BaseAuth):
            """Test auth for registry testing"""

            def authenticate(self):
                """Authenticate"""

            def get_credentials(self):
                """Get credentials"""

            def check_credentials(self):
                """Check credentials"""

        auth_class = get_auth("test_auth")
        assert auth_class == TestAuth

    def test_get_nonexistent_provider_returns_none(self):
        """Test getting non-existent provider returns None"""
        provider = get_provider("nonexistent")
        assert provider is None

    def test_get_nonexistent_storage_returns_none(self):
        """Test getting non-existent storage returns None"""
        storage = get_storage("nonexistent")
        assert storage is None

    def test_get_nonexistent_auth_returns_none(self):
        """Test getting non-existent auth returns None"""
        auth = get_auth("nonexistent")
        assert auth is None
