FROM python:3.12-slim

LABEL maintainer="kernel-ci-team"
LABEL version="1.0"
LABEL description="Kernel CI test container for AWS Fargate"

# Install dependencies
RUN pip install --no-cache-dir boto3 && \
    rm -rf /root/.cache/pip

WORKDIR /app

# Copy scripts
COPY src/kernel_ci_cloud_labs/launch_vm.py /app/launch_vm.py
COPY src/kernel_ci_cloud_labs/debug_aws_setup.py /app/debug_aws_setup.py

# Run debug check (ignore exit code), then launch VMs
CMD python -u /app/debug_aws_setup.py || true && python -u /app/launch_vm.py
