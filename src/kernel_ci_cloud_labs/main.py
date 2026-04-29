"""Main entry point for kernel CI cloud labs."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


# main.py
import importlib
import json
import logging
import os
import pkgutil

from kernel_ci_cloud_labs.core.logging_config import (
    create_run_directory,
    get_logger,
    setup_run_logging,
)
from kernel_ci_cloud_labs.core.pipeline import run_pipeline
from kernel_ci_cloud_labs.core.registry import (
    AUTH_REGISTRY,
    PROVIDER_REGISTRY,
    STORAGE_REGISTRY,
)

# Configure logging from environment
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

# Create run directory first
run_dir = create_run_directory()

# Setup logging inside the run directory
log_paths = setup_run_logging(run_dir, level=log_level)
logger = get_logger(__name__)


def import_all_packages(package_name):
    """Import all submodules of a package so classes register themselves."""
    package = importlib.import_module(package_name)
    for _, module_name, _ in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package_name}.{module_name}")


def load_credentials(config_path) -> dict:
    """Load credentials from credentials.json and merge into config."""
    credentials_path = os.path.join(os.path.dirname(config_path), "credentials.json")

    logger.debug("Loading credentials from %s", credentials_path)

    if not os.path.exists(credentials_path):
        logger.warning("credentials.json not found, using config.json values")
        return None

    with open(credentials_path, encoding="utf-8") as f:
        credentials = json.load(f)

    logger.debug("Loaded credentials from %s", credentials_path)
    return credentials


def main(config_path: str = "examples/aws/config.json"):
    """Main function to run the kernel CI pipeline."""
    # Dynamically import all modules so registries populate
    for pkg in [
        "kernel_ci_cloud_labs.providers",
        "kernel_ci_cloud_labs.storage",
        "kernel_ci_cloud_labs.auth",
    ]:
        import_all_packages(pkg)

    logger.debug("Current working directory: %s", os.getcwd())

    # Load config
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    # Load credentials from credentials.json if it exists
    credentials = load_credentials(config_path)

    # Instantiate objects from registries
    auth_class = AUTH_REGISTRY[config["auth_credentials"]["auth_provider"]]
    provider_class = PROVIDER_REGISTRY[config["provider"]]
    storage_class = STORAGE_REGISTRY[config["storage"]["type"]]

    # Create auth
    auth = auth_class(config, credentials)

    # Create storage - merge storage config with region and external_storage from root
    storage_config = {
        **config["storage"],
        "region": config.get("region"),
        "external_storage": config.get("external_storage", {}),
    }
    storage = storage_class(storage_config, auth)

    # Create provider - it will get bucket name from storage object
    provider = provider_class(auth, config, storage)

    # Run pipeline
    run_pipeline(provider, storage, run_dir=run_dir)


if __name__ == "__main__":
    main()
