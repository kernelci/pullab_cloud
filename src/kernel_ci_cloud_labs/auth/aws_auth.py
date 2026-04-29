__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

# auth/aws_auth.py
"""AWS Authentication implementation for Kernel CI Cloud Labs"""

import os
import subprocess
from typing import Any, Dict, Optional

import boto3

from kernel_ci_cloud_labs.auth.aws_role_manager import AWSRoleManager
from kernel_ci_cloud_labs.core.base_auth import BaseAuth
from kernel_ci_cloud_labs.core.client_manager import ClientManager
from kernel_ci_cloud_labs.core.logging_config import get_logger
from kernel_ci_cloud_labs.core.registry import register_auth

logger = get_logger(__name__)


@register_auth("aws")
class AWSAuth(BaseAuth):
    """
    AWS Authentication handler that supports multiple credential methods.

    Supports:y
    - Default AWS credential chain (env vars, ~/.aws/credentials, IAM roles)
    - Explicit credentials from config.json
    - Named profiles
    - Role assumption
    """

    def __init__(self, config: dict = None, credentials: dict = None):
        """
        Initialize AWS authenticator with configuration.

        Args:
            config: Configuration dict or path to JSON configuration file
        """
        if isinstance(config, dict):
            self.config = config
        else:
            raise TypeError("Wrong variabele type given for authentication object")

        self.credentials = credentials if credentials is not None else {}

        self.region = self.config.get("region", "us-west-2")
        self._session: Optional[boto3.Session] = None
        self._client_manager: Optional[ClientManager] = None
        self._resources_created = False  # Track if resources were created
        self._authenticated = False  # Track if already authenticated
        self.authenticate()

    @property
    def is_authenticated(self) -> bool:
        """Check if AWS authentication is complete."""
        return self._authenticated

    @is_authenticated.setter
    def is_authenticated(self, value: bool):
        """Set authentication status."""
        self._authenticated = value

    def _check_credentials(self) -> bool:
        """
        Check if AWS credentials are configured and valid.

        Returns:
            True if credentials are valid, False otherwise
        """
        try:
            sts = boto3.client("sts")
            sts.get_caller_identity()
            return True
        except (
            boto3.exceptions.Boto3Error,
            boto3.exceptions.botocore.exceptions.BotoCoreError,
            boto3.exceptions.botocore.exceptions.ClientError,
        ):
            return False

    def _run_setup_script(self) -> None:
        """
        Run the IAM user setup script to configure AWS credentials.
        Keeps running until valid credentials are configured.
        """
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            "scripts",
            "aws",
            "setup-iam-user.sh",
        )

        logger.error("AWS credentials not found or invalid.")
        logger.info("Running setup script: %s", script_path)

        while True:
            try:
                subprocess.run([script_path], check=True)
                if self._check_credentials():
                    break
            except subprocess.CalledProcessError:
                logger.error("Setup failed. Please try again.")
            except KeyboardInterrupt:
                logger.info("\nSetup cancelled by user.")
                raise

        logger.info("AWS credentials configured successfully!")

    def authenticate(  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
        self,
    ) -> bool:
        """
        Perform AWS authentication using existing credentials or config file.

        First checks if valid credentials already exist (env vars, ~/.aws/credentials, IAM role).
        If not, uses explicit credentials from config file.

        Returns:
            True if authentication successful

        Raises:
            ValueError: If no valid credentials found
            Exception: If authentication fails for any reason
        """
        # Skip if already authenticated
        if self._authenticated:
            logger.debug("Already authenticated, skipping...")
            return True

        # Load config
        logger.debug("Loaded config - Region: %s", self.region)

        logger.info("Authenticating with AWS credentials. First try given, then default environment...")

        # First, try explicit credentials from config if provided
        if "access_key_id" in self.credentials and "secret_access_key" in self.credentials:
            logger.info("Using explicit credentials from credentials.json file")
            try:
                self._session = boto3.Session(
                    aws_access_key_id=self.credentials["access_key_id"],
                    aws_secret_access_key=self.credentials["secret_access_key"],
                    aws_session_token=self.credentials.get("session_token"),
                    region_name=self.region,
                )
                # Test if credentials are valid
                logger.debug("Testing AWS connection with STS GetCallerIdentity")
                sts = self._session.client("sts")
                sts.get_caller_identity()
                logger.info("✓ Explicit credentials are valid")
            except Exception as e:
                logger.error("Authentication failed: %s", e)
                logger.debug("Exception type: %s", type(e).__name__)
                logger.info("Falling back to default credentials...")
                self._session = None

        # If explicit credentials failed or not provided, try default credentials
        if self._session is None and self._check_credentials():
            logger.info("Using existing AWS credentials")
            self._session = boto3.Session(region_name=self.region)
            logger.debug("Created boto3 session with default credentials")

        if self._session is None:
            # No valid credentials found
            logger.error("No valid AWS credentials found")
            raise ValueError(
                "No valid AWS credentials found. "
                "Please add valid 'access_key_id' and 'secret_access_key' to config file "
                "or configure AWS CLI with 'aws configure'."
            )

        try:
            # Test connection by getting caller identity (if not already tested)
            if "access_key_id" not in self.credentials or "secret_access_key" not in self.credentials:
                logger.debug("Testing AWS connection with STS GetCallerIdentity")
                sts = self._session.client("sts")
                identity = sts.get_caller_identity()
                logger.info("Authenticated as: %s", identity.get("Arn", "Unknown"))
                logger.debug(
                    "Account ID: %s, User ID: %s",
                    identity.get("Account"),
                    identity.get("UserId"),
                )

            # Initialize client manager — factory creates a fresh session each
            # time so that temporary credentials (e.g. Isengard assumed-role)
            # are re-resolved before they expire.
            if self.credentials:
                # Explicit credentials: always reuse them
                def _make_client(service):
                    s = boto3.Session(
                        aws_access_key_id=self.credentials["access_key_id"],
                        aws_secret_access_key=self.credentials["secret_access_key"],
                        aws_session_token=self.credentials.get("session_token"),
                        region_name=self.region,
                    )
                    return s.client(service, region_name=self.region)

            else:
                # Default credential chain: new session picks up refreshed creds
                def _make_client(service):
                    return boto3.Session().client(service, region_name=self.region)

            self._client_manager = ClientManager(_make_client)
            logger.debug("Initialized ClientManager for service clients")

            # Check and create required IAM roles
            role_arns = {}
            if self.config.get("roles"):
                logger.info("Checking required IAM roles...")
                logger.debug("Roles to check: %s", list(self.config["roles"].keys()))
                iam_client = self._client_manager.get_client("iam")
                role_manager = AWSRoleManager(iam_client, self.config["roles"])
                force_recreate = self.config.get("force_recreate_roles", False)
                if force_recreate:
                    logger.warning("force_recreate_roles=True - will delete and recreate existing roles")
                for role_name, role_config in self.config["roles"].items():
                    logger.debug("Processing role: %s", role_name)
                    arn, created = role_manager.ensure_exists(role_name, role_config, force_recreate=force_recreate)
                    role_arns[role_name] = arn
                    if created:
                        self._resources_created = True
                        logger.debug("Role %s was created (new resource)", role_name)

            # Check and create ECR repository
            if self.config.get("ecr"):
                logger.info("Setting up ECR repository...")
                ecr_client = self._client_manager.get_client("ecr")
                from kernel_ci_cloud_labs.auth.aws_ecr_manager import AWSECRManager

                ecr_manager = AWSECRManager(ecr_client, self.config["ecr"])
                repo_uri, created = ecr_manager.ensure_exists(self.config["ecr"]["repository"], self.config["ecr"])
                if created:
                    self._resources_created = True

                # Build and push Docker image if dockerfile specified
                if self.config.get("docker"):
                    self._build_and_push_docker_image(repo_uri)

            # Check and create ECS resources
            if self.config.get("ecs"):
                logger.info("Setting up ECS resources...")
                logger.debug("ECS cluster: %s", self.config["ecs"].get("cluster"))
                ecs_client = self._client_manager.get_client("ecs")
                ec2_client = self._client_manager.get_client("ec2")

                # Ensure cluster exists
                from kernel_ci_cloud_labs.auth.aws_cluster_manager import (
                    AWSClusterManager,
                )

                cluster_manager = AWSClusterManager(ecs_client, self.config["ecs"])
                _, created = cluster_manager.ensure_exists(self.config["ecs"]["cluster"])
                if created:
                    self._resources_created = True
                    logger.debug("ECS cluster was created (new resource)")

                # Setup CloudWatch log groups
                if self.config.get("cloudwatch"):
                    logger.info("Setting up CloudWatch log groups...")
                    log_groups = self.config["cloudwatch"].get("log_groups", {})
                    logger.debug("Log groups to create: %s", list(log_groups.keys()))
                    logs_client = self._client_manager.get_client("logs")
                    from kernel_ci_cloud_labs.auth.aws_cloudwatch_manager import (
                        AWSCloudWatchManager,
                    )

                    cw_manager = AWSCloudWatchManager(logs_client, self.config["cloudwatch"])

                    for log_group, log_config in log_groups.items():
                        logger.debug("Ensuring log group exists: %s", log_group)
                        cw_manager.ensure_exists(log_group, log_config)
                else:
                    logger.warning("No CloudWatch configuration found - logs may not be available")

                # Ensure task definition exists
                from kernel_ci_cloud_labs.auth.aws_task_definition_manager import (
                    AWSTaskDefinitionManager,
                )

                task_config = self.config["ecs"]["task_definition"].copy()
                # Use the first role ARN — role name is configurable via config.json
                first_role_arn = next(iter(role_arns.values()), None)
                task_config["execution_role_arn"] = first_role_arn
                task_config["task_role_arn"] = first_role_arn
                logger.debug("Task definition family: %s", task_config.get("family"))
                logger.debug("Execution role ARN: %s", task_config.get("execution_role_arn"))

                task_manager = AWSTaskDefinitionManager(ecs_client, {})
                task_manager.ensure_exists(task_config["family"], task_config)

                # Store network manager for later use
                from kernel_ci_cloud_labs.auth.aws_network_manager import (
                    AWSNetworkManager,
                )

                self._network_manager = AWSNetworkManager(ec2_client, {})
                logger.debug("Initialized NetworkManager for VPC/subnet configuration")

            # Mark as authenticated
            self._authenticated = True
            logger.debug("Authentication completed successfully")
            return True

        except (
            boto3.exceptions.Boto3Error,
            boto3.exceptions.botocore.exceptions.BotoCoreError,
            boto3.exceptions.botocore.exceptions.ClientError,
            KeyError,
        ) as e:
            logger.error("Authentication failed: %s", e)
            logger.debug("Exception type: %s", type(e).__name__)
            raise

    def get_credentials(self) -> Dict[str, Any]:
        """
        Get AWS credentials for downstream usage.

        Returns:
            Dictionary containing boto3 session and region information

        Raises:
            Exception: If authentication hasn't been performed yet
        """
        if not self._session:
            self.authenticate()
        return {"session": self._session, "region": self.region}

    def get_client(self, service_name: str):
        """
        Get boto3 client for specified AWS service with auto-refresh.

        Args:
            service_name: AWS service name (e.g., 's3', 'ec2', 'ssm')

        Returns:
            Configured boto3 client for the service

        Raises:
            Exception: If authentication fails or service is invalid
        """
        if not self._client_manager:
            self.authenticate()
        return self._client_manager.get_client(service_name)

    def get_network_config(self):
        """Get VPC network configuration for Fargate"""
        if not hasattr(self, "_network_manager"):
            raise RuntimeError("Network manager not initialized. Run authenticate() first.")
        return self._network_manager.get_network_config()

    def resources_were_created(self) -> bool:
        """Check if any AWS resources were created during authentication"""
        return self._resources_created

    def wait_for_resources(self):
        """Wait for newly created AWS resources to propagate using boto3 waiters."""

        iam = self.get_client("iam")
        ecs = self.get_client("ecs")

        # Wait for IAM roles to exist
        for role_name in self.config.get("roles", {}).keys():
            logger.info("Waiting for IAM role to propagate: %s", role_name)
            waiter = iam.get_waiter("role_exists")
            waiter.wait(RoleName=role_name)
            logger.debug("✓ Role ready: %s", role_name)

        # Wait for ECS service-linked role if ECS is configured
        if self.config.get("ecs"):
            logger.info("Waiting for ECS service-linked role to propagate...")
            waiter = iam.get_waiter("role_exists")
            waiter.wait(RoleName="AWSServiceRoleForECS")
            logger.debug("✓ ECS service-linked role ready")

        # Verify ECS cluster exists
        cluster = self.config.get("ecs", {}).get("cluster")
        if cluster:
            logger.info("Verifying ECS cluster: %s", cluster)
            ecs.describe_clusters(clusters=[cluster])
            logger.debug("✓ Cluster ready: %s", cluster)

    def _build_and_push_docker_image(self, repo_uri: str) -> str:
        """Build Docker image and push to ECR if not exists"""
        import base64

        docker_config = self.config.get("docker")
        dockerfile = docker_config["dockerfile"]
        build_context = docker_config.get("build_context", ".")
        image_tag = docker_config.get("tag", "latest")
        force_rebuild = docker_config.get("force_rebuild", False)
        repo_name = self.config["ecr"]["repository"]

        # Check if image already exists (unless force_rebuild is True)
        ecr_client = self._client_manager.get_client("ecr")
        image_uri = f"{repo_uri}:{image_tag}"

        if not force_rebuild:
            try:
                ecr_client.describe_images(repositoryName=repo_name, imageIds=[{"imageTag": image_tag}])
                logger.info("✓ Image already exists: %s", image_uri)

                # Update task definition config with existing image
                if self.config.get("ecs", {}).get("task_definition"):
                    self.config["ecs"]["task_definition"]["image"] = image_uri

                return image_uri
            except ecr_client.exceptions.ImageNotFoundException:
                logger.info("Image not found, building from %s", dockerfile)
        else:
            logger.info("Force rebuild enabled, building from %s", dockerfile)

        # Get ECR login credentials
        auth_data = ecr_client.get_authorization_token()
        token = base64.b64decode(auth_data["authorizationData"][0]["authorizationToken"]).decode()
        username, password = token.split(":")
        endpoint = auth_data["authorizationData"][0]["proxyEndpoint"]

        # Login to ECR
        logger.info("Logging in to ECR...")
        subprocess.run(
            ["docker", "login", "--username", username, "--password-stdin", endpoint],
            input=password.encode(),
            check=True,
            capture_output=True,
        )

        # Build image
        image_uri = f"{repo_uri}:{image_tag}"
        logger.info("Building image: %s", image_uri)
        subprocess.run(
            [
                "docker",
                "build",
                "--network",
                "host",
                "-f",
                dockerfile,
                "-t",
                image_uri,
                build_context,
            ],
            check=True,
        )

        # Push image
        logger.info("Pushing image to ECR...")
        subprocess.run(["docker", "push", image_uri], check=True)

        logger.info("✓ Image pushed: %s", image_uri)

        # Update task definition config with new image
        if self.config.get("ecs", {}).get("task_definition"):
            self.config["ecs"]["task_definition"]["image"] = image_uri

        return image_uri
