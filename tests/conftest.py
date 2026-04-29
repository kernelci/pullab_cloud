"""Pytest configuration for tests"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


def pytest_collection_modifyitems(items):
    """Run integration tests last to prevent logging configuration from affecting unit tests"""
    integration_tests = []
    unit_tests = []

    for item in items:
        if "integration" in item.nodeid:
            integration_tests.append(item)
        else:
            unit_tests.append(item)

    # Reorder: unit tests first, then integration tests
    items[:] = unit_tests + integration_tests
