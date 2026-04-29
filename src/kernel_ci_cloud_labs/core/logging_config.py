"""Centralized logging configuration for Kernel CI Cloud Labs."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import logging
import os
import sys
from datetime import datetime
from pathlib import Path


def create_run_directory(base_dir="logs"):
    """
    Create a timestamped directory for this pipeline run.

    Args:
        base_dir: Base directory for logs (default: "logs")

    Returns:
        Path to the run directory
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(base_dir) / f"run_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def setup_run_logging(run_dir, level=logging.INFO):
    """
    Setup logging with separate files for pipeline, container, and VMs.

    Args:
        run_dir: Directory for this run's logs
        level: Console logging level

    Returns:
        Dict with logger names and file paths
    """
    # Create formatters
    formatter = logging.Formatter("[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Capture everything
    root_logger.handlers.clear()

    # Suppress verbose AWS SDK loggers
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Console handler (INFO level)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Pipeline log file (everything)
    pipeline_log = run_dir / "pipeline.log"
    pipeline_handler = logging.FileHandler(pipeline_log)
    pipeline_handler.setLevel(logging.DEBUG)
    pipeline_handler.setFormatter(formatter)
    root_logger.addHandler(pipeline_handler)

    root_logger.info("Pipeline logs: %s", pipeline_log)

    return {
        "run_dir": run_dir,
        "pipeline_log": pipeline_log,
        "container_log": run_dir / "container.log",
        "vms_log": run_dir / "vms.log",
        "summary_file": run_dir / "summary.json",
    }


def setup_logging(level=logging.INFO, log_to_file=True, log_dir="logs"):
    """
    Configure logging for the entire application.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_to_file: Whether to save logs to file
        log_dir: Directory to save log files
    """
    # Create logs directory if it doesn't exist
    if log_to_file:
        Path(log_dir).mkdir(exist_ok=True)

    # Create formatters
    formatter = logging.Formatter("[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers
    root_logger.handlers.clear()

    # Suppress verbose AWS SDK loggers
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler with timestamp
    if log_to_file:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.join(log_dir, f"kernel_ci_{timestamp}.log")

        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)  # Always log DEBUG to file
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

        # Log the file location
        root_logger.info("Logging to file: %s", log_file)


def get_logger(name):
    """Get a logger instance for a module."""
    return logging.getLogger(name)
