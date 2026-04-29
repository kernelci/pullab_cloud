"""Registry system for auto-discovery of providers, storage, and auth modules."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


# core/registry.py
PROVIDER_REGISTRY = {}
STORAGE_REGISTRY = {}
AUTH_REGISTRY = {}


def register_provider(name):
    """Decorator to register a provider class."""

    def decorator(cls):
        PROVIDER_REGISTRY[name] = cls
        return cls

    return decorator


def register_storage(name):
    """Decorator to register a storage class."""

    def decorator(cls):
        STORAGE_REGISTRY[name] = cls
        return cls

    return decorator


def register_auth(name):
    """Decorator to register an auth class."""

    def decorator(cls):
        AUTH_REGISTRY[name] = cls
        return cls

    return decorator


def get_provider(name):
    """Get provider class by name."""
    return PROVIDER_REGISTRY.get(name)


def get_storage(name):
    """Get storage class by name."""
    return STORAGE_REGISTRY.get(name)


def get_auth(name):
    """Get auth class by name."""
    return AUTH_REGISTRY.get(name)
