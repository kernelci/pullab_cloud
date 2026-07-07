"""
Kernel-ci-cloud-labs packaging setup.
"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from setuptools import find_packages, setup

# PLACEHOLDER: Add configuration files if needed
# Uncomment and modify if you have configuration files to include:
# data_files = []
# for root, dirs, files in os.walk("configuration"):
#     data_files.append(
#         (os.path.relpath(root, "configuration"), [os.path.join(root, f) for f in files])
#     )

with open("README.md", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="kernel-ci-cloud-labs",
    version="1.0.0",
    description="Kernel-ci-cloud-labs",
    long_description=long_description,
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    python_requires=">=3.11",
    install_requires=[
        "boto3>=1.26.0",
    ],
    entry_points={
        "console_scripts": [
            "kernel-ci-cloud-runner=kernel_ci_cloud_labs.cli:main",
        ],
    },
    extras_require={
        "dev": [
            "pytest>=6.0",
            "black>=21.0",
            "pylint>=2.0",
            "flake8>=5.0",
            "isort>=5.0",
            "pre-commit>=2.0",
            "pytest-cov>=2.0",
            "pandas>=1.3.0",
            "matplotlib>=3.4.0",
            "seaborn>=0.11.0",
        ],
        "analysis": [
            "pandas>=1.3.0",
            "matplotlib>=3.4.0",
            "seaborn>=0.11.0",
        ],
    },
    # PLACEHOLDER: Uncomment if you have data files
    # data_files=data_files,
)
