"""Utilities for managing secrets from AWS Secrets Manager."""

import json
import os
from functools import lru_cache
from typing import Any

import boto3
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from utils.constants import APPSYNC_SECRET_PREFIX, DEFAULT_REGION, MAX_CACHE_SIZE_DEFAULT

MAX_CACHE_SIZE = MAX_CACHE_SIZE_DEFAULT


def mask_sensitive_data(*, data: str, prefix_length: int = 4, suffix_length: int = 4) -> str:
    """Mask sensitive data for logging, keeping only prefix and suffix.

    Args:
        data: The sensitive string to mask
        prefix_length: Number of characters to show at the beginning
        suffix_length: Number of characters to show at the end

    Returns:
        Masked string with format: "prefix***suffix"
    """
    if not data or len(data) <= (prefix_length + suffix_length):
        return "***"
    return f"{data[:prefix_length]}***{data[-suffix_length:]}"


class AppSyncCredentials(BaseModel):
    """AppSync credentials model."""

    url: str = Field(..., min_length=1, description="AppSync GraphQL endpoint URL")
    api_key: str = Field(..., min_length=1, description="AppSync API key")


class SecretNotFoundError(Exception):
    """Raised when a secret cannot be found or retrieved."""


class InvalidSecretDataError(Exception):
    """Raised when secret data is malformed or incomplete."""


def _get_region() -> str:
    """Get AWS region from environment or default.

    Returns:
        AWS region name.
    """
    return os.environ.get("AWS_REGION", DEFAULT_REGION)


def _create_secrets_client(*, region_name: str) -> Any:
    """Create AWS Secrets Manager client.

    Args:
        region_name: AWS region name.

    Returns:
        Configured Secrets Manager client.
    """
    session = boto3.session.Session()
    return session.client(service_name="secretsmanager", region_name=region_name)


@lru_cache(maxsize=MAX_CACHE_SIZE)
def get_secret(*, secret_name: str, region_name: str | None = None) -> dict[str, Any]:
    """Get secret from AWS Secrets Manager with caching.

    Args:
        secret_name: Name of the secret in Secrets Manager.
        region_name: AWS region name (defaults to AWS_REGION env var).

    Returns:
        Dictionary containing secret key-value pairs.

    Raises:
        SecretNotFoundError: If secret cannot be retrieved.
        InvalidSecretDataError: If secret data is malformed.
    """
    region = region_name or _get_region()
    client = _create_secrets_client(region_name=region)

    try:
        response = client.get_secret_value(SecretId=secret_name)
        secret_string: str = response["SecretString"]
        data = json.loads(secret_string)
        if not isinstance(data, dict):
            raise InvalidSecretDataError("SecretString must decode to a JSON object")
        return data
    except client.exceptions.ResourceNotFoundException as e:
        logger.error(f"Secret not found: {secret_name}")
        raise SecretNotFoundError(f"Secret '{secret_name}' not found in region '{region}'") from e
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in secret {secret_name}")
        raise InvalidSecretDataError(f"Secret '{secret_name}' contains invalid JSON") from e
    except Exception as e:
        logger.error(f"Failed to retrieve secret {secret_name}: {e}")
        raise SecretNotFoundError(f"Failed to retrieve secret '{secret_name}': {e}") from e


def _get_appsync_secret_name(*, env: str) -> str:
    """Build AppSync secret name for environment.

    Args:
        env: Environment name.

    Returns:
        Formatted secret name.
    """
    return f"{APPSYNC_SECRET_PREFIX}/{env}"


def _get_appsync_from_environment() -> AppSyncCredentials | None:
    """Get AppSync credentials from environment variables.

    Returns:
        AppSync credentials if available, None otherwise.
    """
    appsync_url = os.environ.get("APPSYNC_URL")
    api_key = os.environ.get("APPSYNC_API_KEY")

    if not appsync_url or not api_key:
        return None

    try:
        return AppSyncCredentials(url=appsync_url, api_key=api_key)
    except ValidationError as e:
        logger.warning(f"Invalid AppSync credentials in environment: {e}")
        return None


def get_appsync_credentials(*, env: str = "legacy") -> tuple[str, str]:
    """Get AppSync URL and API key from Secrets Manager with environment fallback.

    Args:
        env: Environment name (legacy, stage, dev2, prod).

    Returns:
        Tuple of (appsync_url, api_key).

    Raises:
        SecretNotFoundError: If credentials cannot be retrieved from any source.
        InvalidSecretDataError: If secret data is malformed.
    """
    secret_name: str = _get_appsync_secret_name(env=env)

    try:
        secret_data: dict[str, Any] = get_secret(secret_name=secret_name)
        credentials = AppSyncCredentials.model_validate(secret_data)
        masked_url = mask_sensitive_data(data=credentials.url, prefix_length=8, suffix_length=0)
        masked_key = mask_sensitive_data(data=credentials.api_key)
        logger.debug(
            f"Retrieved AppSync credentials from Secrets Manager: {secret_name} (URL: {masked_url}, Key: {masked_key})"
        )
        return credentials.url, credentials.api_key

    except (SecretNotFoundError, InvalidSecretDataError, ValidationError) as e:
        logger.warning(f"Failed to get AppSync credentials from Secrets Manager: {e}")

        env_credentials: AppSyncCredentials | None = _get_appsync_from_environment()
        if env_credentials:
            masked_url = mask_sensitive_data(
                data=env_credentials.url, prefix_length=8, suffix_length=0
            )
            masked_key = mask_sensitive_data(data=env_credentials.api_key)
            logger.info(
                f"Using AppSync credentials from environment variables (URL: {masked_url}, Key: {masked_key})"
            )
            return env_credentials.url, env_credentials.api_key

        raise SecretNotFoundError(
            f"AppSync credentials not available from Secrets Manager ({secret_name}) "
            "or environment variables (APPSYNC_URL, APPSYNC_API_KEY)"
        ) from e
