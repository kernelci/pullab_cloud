FROM python:3.12-slim

LABEL maintainer="kernel-ci-team"
LABEL version="1.0"
LABEL description="Kernel CI test container for AWS Fargate"

# Install dependencies
RUN pip install --no-cache-dir boto3 && \
    rm -rf /root/.cache/pip

WORKDIR /app

# Copy scripts (with minimal package skeleton so launch_vm.py's
# `from kernel_ci_cloud_labs.core.log_scrub import scrub_text` resolves)
COPY src/kernel_ci_cloud_labs/__init__.py        /app/kernel_ci_cloud_labs/__init__.py
COPY src/kernel_ci_cloud_labs/core/__init__.py   /app/kernel_ci_cloud_labs/core/__init__.py
COPY src/kernel_ci_cloud_labs/core/log_scrub.py  /app/kernel_ci_cloud_labs/core/log_scrub.py
COPY src/kernel_ci_cloud_labs/launch_vm.py       /app/launch_vm.py
COPY src/kernel_ci_cloud_labs/debug_aws_setup.py /app/debug_aws_setup.py

# Run debug check (ignore exit code), then launch VMs
CMD python -u /app/debug_aws_setup.py || true && python -u /app/launch_vm.py
