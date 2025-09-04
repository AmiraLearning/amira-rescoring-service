#!/usr/bin/env python3
"""Script to set up AppSync credentials in AWS Secrets Manager.

This script helps configure AppSync GraphQL endpoint URL and API key
for different environments (legacy, stage, dev2, prod).
"""
# TODO switch all scripts to using typer

import argparse
import json
import sys
from enum import StrEnum

import boto3
from loguru import logger
from pydantic import BaseModel, Field, field_validator

from utils.constants import APPSYNC_SECRET_PREFIX, DEFAULT_REGION, MIN_API_KEY_LENGTH


class Environment(StrEnum):
    """Supported AppSync environments."""

    LEGACY = "legacy"
    STAGE = "stage"
    DEV2 = "dev2"
    PROD = "prod"


class AppSyncCredentials(BaseModel):
    """AppSync credentials model with validation."""

    url: str = Field(..., description="AppSync GraphQL endpoint URL")
    api_key: str = Field(..., description="AppSync API key")

    @field_validator("url")
    def validate_url(cls, v: str) -> str:
        """Validate AppSync GraphQL URL format."""
        if not v:
            raise ValueError("URL cannot be empty")

        if not (v.startswith("https://") and "appsync" in v and v.endswith("/graphql")):
            raise ValueError(
                f"Invalid AppSync URL format: {v}. "
                "Expected format: https://xxxxx.appsync.region.amazonaws.com/graphql"
            )
        return v

    @field_validator("api_key")
    def validate_api_key(cls, v: str) -> str:
        """Validate API key format."""
        if not v or len(v) < MIN_API_KEY_LENGTH:
            raise ValueError(f"API key must be at least {MIN_API_KEY_LENGTH} characters")
        return v


class SecretsManagerClient:
    """AWS Secrets Manager client wrapper."""

    def __init__(self, *, region: str) -> None:
        """Initialize Secrets Manager client.

        Args:
            region: AWS region name.
        """
        self._client = boto3.client("secretsmanager", region_name=region)
        self._region = region

    def create_or_update_secret(self, *, secret_name: str, credentials: AppSyncCredentials) -> None:
        """Create or update a secret in Secrets Manager.

        Args:
            secret_name: Name of the secret.
            credentials: AppSync credentials to store.

        Raises:
            Exception: If secret operation fails.
        """
        secret_value = credentials.model_dump()
        secret_string = json.dumps(secret_value)

        try:
            self._update_existing_secret(secret_name=secret_name, secret_string=secret_string)
            logger.info(f"Updated existing secret: {secret_name}")

        except self._client.exceptions.ResourceNotFoundException:
            self._create_new_secret(secret_name=secret_name, secret_string=secret_string)
            logger.info(f"Created new secret: {secret_name}")

        except Exception as e:
            logger.error(f"Failed to create/update secret {secret_name}: {e}")
            raise

    def _update_existing_secret(self, *, secret_name: str, secret_string: str) -> None:
        """Update existing secret."""
        self._client.update_secret(SecretId=secret_name, SecretString=secret_string)

    def _create_new_secret(self, *, secret_name: str, secret_string: str) -> None:
        """Create new secret."""
        env_name = secret_name.split("/")[-1]
        self._client.create_secret(
            Name=secret_name,
            SecretString=secret_string,
            Description=f"AppSync credentials for {env_name} environment",
        )


def _format_secret_name(*, env: Environment) -> str:
    """Format secret name for environment.

    Args:
        env: Environment name.

    Returns:
        Formatted secret name.
    """
    return f"{APPSYNC_SECRET_PREFIX}/{env.value}"


def setup_appsync_secret(
    *, env: Environment, credentials: AppSyncCredentials, region: str = DEFAULT_REGION
) -> None:
    """Set up AppSync secret for an environment.

    Args:
        env: Environment name.
        credentials: AppSync credentials.
        region: AWS region name.
    """
    secret_name = _format_secret_name(env=env)

    client = SecretsManagerClient(region=region)
    client.create_or_update_secret(secret_name=secret_name, credentials=credentials)

    logger.info(f"AppSync credentials configured for {env.value} environment in {region}")
    logger.info(f"Secret name: {secret_name}")


def _get_credentials_interactively(*, env: Environment) -> AppSyncCredentials:
    """Get credentials through interactive prompts.

    Args:
        env: Environment name.

    Returns:
        Validated AppSync credentials.
    """
    print(f"Setting up AppSync credentials for {env.value} environment...")
    url = input("AppSync GraphQL endpoint URL: ").strip()
    api_key = input("AppSync API key: ").strip()

    return AppSyncCredentials(url=url, api_key=api_key)


def _get_credentials_from_args(*, url: str, api_key: str) -> AppSyncCredentials:
    """Get credentials from command line arguments.

    Args:
        url: AppSync GraphQL endpoint URL.
        api_key: AppSync API key.

    Returns:
        Validated AppSync credentials.
    """
    return AppSyncCredentials(url=url, api_key=api_key)


def _create_argument_parser() -> argparse.ArgumentParser:
    """Create command line argument parser.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Set up AppSync credentials in AWS Secrets Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Set up legacy environment credentials
  python setup_appsync_secrets.py legacy \\
    --url https://abc123.appsync.us-east-1.amazonaws.com/graphql \\
    --api-key da2-xxxxxxxxxxxxxxxxxxxxxxxxxx

  # Set up dev2 environment credentials
  python setup_appsync_secrets.py dev2 \\
    --url https://xyz789.appsync.us-east-1.amazonaws.com/graphql \\
    --api-key da2-yyyyyyyyyyyyyyyyyyyyyyyyyy \\
    --region us-east-1

  # Interactive mode (will prompt for credentials)
  python setup_appsync_secrets.py stage --interactive
        """,
    )

    parser.add_argument("environment", type=Environment, help="Environment name")
    parser.add_argument("--url", help="AppSync GraphQL endpoint URL")
    parser.add_argument("--api-key", help="AppSync API key")
    parser.add_argument(
        "--region", default=DEFAULT_REGION, help=f"AWS region (default: {DEFAULT_REGION})"
    )
    parser.add_argument(
        "--interactive", "-i", action="store_true", help="Interactive mode - prompt for credentials"
    )

    return parser


def _validate_non_interactive_args(*, url: str | None, api_key: str | None) -> None:
    """Validate required arguments for non-interactive mode.

    Args:
        url: AppSync GraphQL endpoint URL.
        api_key: AppSync API key.

    Raises:
        SystemExit: If required arguments are missing.
    """
    if not url or not api_key:
        logger.error("--url and --api-key are required unless using --interactive mode")
        sys.exit(1)


def main() -> None:
    """Main function."""
    parser = _create_argument_parser()
    args = parser.parse_args()

    try:
        if args.interactive:
            credentials = _get_credentials_interactively(env=args.environment)
        else:
            _validate_non_interactive_args(url=args.url, api_key=args.api_key)
            credentials = _get_credentials_from_args(url=args.url, api_key=args.api_key)

        setup_appsync_secret(env=args.environment, credentials=credentials, region=args.region)

        print("\nSuccess! AppSync credentials are now configured.")
        print(
            f"The application will automatically use these credentials when AUDIO_ENV={args.environment.value}"
        )

    except Exception as e:
        logger.error(f"Setup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
