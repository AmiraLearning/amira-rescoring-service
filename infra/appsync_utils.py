import os
from typing import Any, Final

import requests
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from loguru import logger
from pydantic import BaseModel
from requests import adapters
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from infra.secrets_utils import get_appsync_credentials
from utils.config import AwsConfig

_GRAPHQL_ALLOW_MOCK: Final[bool] = os.environ.get("APPSYNC_ALLOW_MOCK", "false").lower() == "true"

# Cache for AppSync credentials - will be loaded lazily
_cached_appsync_url: str | None = None
_cached_appsync_key: str | None = None

# Global cache for GraphQL client and session to optimize Lambda warm starts
_cached_session: requests.Session | None = None
_cached_graphql_client: Client | None = None
_cached_client_config_hash: int | None = None


def _get_appsync_credentials(config: AwsConfig) -> tuple[str, str]:
    """Get AppSync credentials from Secrets Manager or environment variables.

    Args:
        config: AWS configuration containing environment info

    Returns:
        Tuple of (appsync_url, api_key)
    """
    global _cached_appsync_url, _cached_appsync_key

    if _cached_appsync_url and _cached_appsync_key:
        return _cached_appsync_url, _cached_appsync_key

    try:
        # Try to get from Secrets Manager based on environment
        env = config.appsync_env if hasattr(config, "appsync_env") else "legacy"
        _cached_appsync_url, _cached_appsync_key = get_appsync_credentials(env=env)
        logger.info(f"Loaded AppSync credentials from Secrets Manager for env: {env}")
    except Exception as e:
        # Fallback to environment variables
        _cached_appsync_url = os.environ.get("APPSYNC_URL", "")
        _cached_appsync_key = os.environ.get("APPSYNC_API_KEY", "")

        if _cached_appsync_url and _cached_appsync_key:
            logger.info("Using AppSync credentials from environment variables")
        else:
            logger.warning(f"Failed to load AppSync credentials: {e}")

    return _cached_appsync_url or "", _cached_appsync_key or ""


UPDATE_ACTIVITY_FIELDS_MUTATION = gql(
    """
    mutation UpdateActivityFields($activityId: ID!, $fieldValues: AWSJSON!) {
        updateActivity(activityId: $activityId, fieldValues: $fieldValues) {
            activityId
        }
    }
    """
)

ACTIVITIES_WITH_FILTER_QUERY = gql(
    """
    query ActivitiesWithFilter($filter: ActivityFilter!, $limit: Int) {
        activities(filter: $filter, limit: $limit) {
            activityId
            studentId
            storyId
            createdAt
            updatedAt
            errors
            story {
                content
                referenceStoryPhrases
            }
            audio {
                url
            }
        }
    }
    """
)

INTROSPECTION_QUERY = gql(
    """
    query IntrospectionQuery {
        __schema {
            queryType {
                name
                fields {
                    name
                    args {
                        name
                        type {
                            name
                            kind
                            ofType {
                                name
                                kind
                            }
                        }
                    }
                }
            }
            types {
                name
                kind
                fields {
                    name
                    type {
                        name
                        kind
                        ofType {
                            name
                            kind
                        }
                    }
                }
                inputFields {
                    name
                    type {
                        name
                        kind
                        ofType {
                            name
                            kind
                        }
                    }
                }
            }
        }
    }
    """
)

GET_ACTIVITY_QUERY = gql(
    """
    query GetActivity($activityId: [String]!) {
        getActivity(activityId: $activityId) {
            activityId
            studentId
            storyId
            createdAt
            updatedAt
            errors
            story {
                content
                referenceStoryPhrases
            }
            audio {
                url
            }
        }
    }
    """
)


class ActivityFieldValues(BaseModel):
    alignment_errors: list[list[bool]] | None = None
    error_count: int | None = None
    total_alignments: int | None = None
    alignment_accuracy: float | None = None


class UpdateActivityFieldsInput(BaseModel):
    """Input for updating activity fields in AppSync.

    Attributes:
        activityId: Unique identifier for the activity.
        fieldValues: Dictionary of field names and values to update.
    """

    activityId: str
    fieldValues: ActivityFieldValues | dict[str, Any]


class UpdateActivityPayload(BaseModel):
    activityId: str


class UpdateActivityData(BaseModel):
    updateActivity: UpdateActivityPayload


class UpdateActivityResponse(BaseModel):
    data: UpdateActivityData


def _get_cached_session() -> requests.Session:
    """Get or create a cached HTTP session with connection pooling."""
    global _cached_session
    if _cached_session is None:
        _cached_session = requests.Session()

        # Configure connection pooling for better performance
        adapter = adapters.HTTPAdapter(
            pool_connections=2,  # Number of connection pools
            pool_maxsize=5,  # Maximum number of connections in pool
            max_retries=0,  # Let tenacity handle retries
        )
        _cached_session.mount("https://", adapter)
        _cached_session.mount("http://", adapter)

        # Set keep-alive and timeout defaults
        _cached_session.headers.update(
            {"Connection": "keep-alive", "Keep-Alive": "timeout=30, max=100"}
        )

    return _cached_session


def _get_cached_graphql_client(
    *, url: str, api_key: str, timeout: int, correlation_id: str | None = None
) -> Client:
    """Get or create a cached GraphQL client with connection pooling."""
    global _cached_graphql_client, _cached_client_config_hash

    # Create config hash to detect changes that require new client
    config_items = [url, api_key, str(timeout)]
    config_hash = hash(tuple(config_items))

    if _cached_graphql_client is None or _cached_client_config_hash != config_hash:
        session = _get_cached_session()

        headers = {"x-api-key": api_key}
        if correlation_id:
            headers["x-correlation-id"] = correlation_id

        transport = RequestsHTTPTransport(
            url=url,
            headers=headers,
            timeout=timeout,
            session=session,  # Use cached session with connection pooling
        )

        _cached_graphql_client = Client(transport=transport)
        _cached_client_config_hash = config_hash

    # Update correlation ID in headers if provided (client is cached, but headers can change)
    if correlation_id and hasattr(_cached_graphql_client.transport, "headers"):
        _cached_graphql_client.transport.headers["x-correlation-id"] = correlation_id

    return _cached_graphql_client


def set_activity_fields(
    *,
    activity_id: str,
    field_values: dict[str, Any],
    config: AwsConfig,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Set fields for an activity using a GraphQL client.

    Args:
        activity_id: Unique identifier for the activity.
        field_values: Dictionary of field names and values to update.
        config: The configuration object.

    Returns:
        The response data from the AppSync API.
    """
    variables: UpdateActivityFieldsInput = UpdateActivityFieldsInput(
        activityId=activity_id, fieldValues=field_values
    )

    # Get credentials from Secrets Manager or environment variables
    endpoint_url, api_key = _get_appsync_credentials(config)

    if not endpoint_url or not api_key:
        if _GRAPHQL_ALLOW_MOCK:
            logger.warning(
                "Missing AppSync credentials; returning mock response due to APPSYNC_ALLOW_MOCK=true"
            )
            return {"data": {"updateActivity": {"activityId": activity_id}}}
        raise RuntimeError(
            "AppSync URL and API key must be available from Secrets Manager or environment variables"
        )

    timeout_s: int = int(os.getenv("APPSYNC_TIMEOUT", "60"))
    max_attempts: int = int(os.getenv("APPSYNC_MAX_ATTEMPTS", "3"))

    # Use cached client with connection pooling
    client = _get_cached_graphql_client(
        url=endpoint_url,
        api_key=api_key,
        timeout=timeout_s,
        correlation_id=correlation_id,
    )

    @retry(
        stop=stop_after_attempt(max(1, max_attempts)),
        wait=wait_exponential_jitter(exp_base=2, max=10),
        reraise=True,
    )
    def _do_update() -> dict[str, Any]:
        return client.execute(
            UPDATE_ACTIVITY_FIELDS_MUTATION, variable_values=variables.model_dump()
        )

    try:
        response = _do_update()
    except Exception as e:
        if _GRAPHQL_ALLOW_MOCK:
            logger.info(
                f"GraphQL update failed; returning mock due to APPSYNC_ALLOW_MOCK=true: {e}"
            )
            response = {"data": {"updateActivity": {"activityId": activity_id}}}
        else:
            raise

    try:
        UpdateActivityResponse.model_validate(response)
    except Exception as ve:
        logger.warning(f"AppSync response did not match expected shape: {ve}")

    return response


def query_activities_with_filter(
    *, activity_filter: dict[str, Any], limit: int = 10, config: AwsConfig | None = None
) -> dict[str, Any]:
    """Query activities using ActivityFilter.

    Args:
        activity_filter: Dictionary containing activity filter parameters.
        limit: Maximum number of activities to return.
        config: AWS configuration object (uses default if not provided).

    Returns:
        The response data from the AppSync API.
    """
    variables = {"filter": activity_filter, "limit": limit}

    # Use default config if not provided
    if config is None:
        from utils.config import load_config

        loaded_config = load_config()
        aws_config = loaded_config.aws
    else:
        aws_config = config

    # Get credentials from Secrets Manager or environment variables
    endpoint_url, api_key = _get_appsync_credentials(aws_config)

    if not endpoint_url or not api_key:
        if _GRAPHQL_ALLOW_MOCK:
            logger.warning(
                "Missing AppSync credentials; returning mock due to APPSYNC_ALLOW_MOCK=true"
            )
            return {"data": {"activities": []}}
        raise RuntimeError(
            "AppSync URL and API key must be available from Secrets Manager or environment variables"
        )

    timeout_s: int = int(os.getenv("APPSYNC_TIMEOUT", "60"))

    # Use cached client with connection pooling
    client = _get_cached_graphql_client(url=endpoint_url, api_key=api_key, timeout=timeout_s)

    response: dict[str, Any] = client.execute(
        ACTIVITIES_WITH_FILTER_QUERY, variable_values=variables
    )

    return response


def introspect_schema(*, config: AwsConfig | None = None) -> dict[str, Any]:
    """Perform GraphQL introspection to understand the schema.

    Args:
        config: AWS configuration object (uses default if not provided).

    Returns:
        The schema introspection data from the AppSync API.
    """
    # Use default config if not provided
    if config is None:
        from utils.config import load_config

        loaded_config = load_config()
        aws_config = loaded_config.aws
    else:
        aws_config = config

    # Get credentials from Secrets Manager or environment variables
    endpoint_url, api_key = _get_appsync_credentials(aws_config)

    if not endpoint_url or not api_key:
        if _GRAPHQL_ALLOW_MOCK:
            logger.warning(
                "Missing AppSync credentials; returning mock due to APPSYNC_ALLOW_MOCK=true"
            )
            return {"data": {"__schema": {}}}
        raise RuntimeError(
            "AppSync URL and API key must be available from Secrets Manager or environment variables"
        )

    timeout_s: int = int(os.getenv("APPSYNC_TIMEOUT", "60"))

    # Use cached client with connection pooling
    client = _get_cached_graphql_client(url=endpoint_url, api_key=api_key, timeout=timeout_s)

    response: dict[str, Any] = client.execute(INTROSPECTION_QUERY)
    return response


def get_activity(*, activity_id: str, config: AwsConfig | None = None) -> dict[str, Any]:
    """Get a specific activity by ID.

    Args:
        activity_id: The activity ID to retrieve.
        config: AWS configuration object (uses default if not provided).

    Returns:
        The response data from the AppSync API.
    """
    variables = {"activityId": [activity_id]}

    # Use default config if not provided
    if config is None:
        from utils.config import load_config

        loaded_config = load_config()
        aws_config = loaded_config.aws
    else:
        aws_config = config

    # Get credentials from Secrets Manager or environment variables
    endpoint_url, api_key = _get_appsync_credentials(aws_config)

    if not endpoint_url or not api_key:
        if _GRAPHQL_ALLOW_MOCK:
            logger.warning(
                "Missing AppSync credentials; returning mock due to APPSYNC_ALLOW_MOCK=true"
            )
            return {"getActivity": []}
        raise RuntimeError(
            "AppSync URL and API key must be available from Secrets Manager or environment variables"
        )

    timeout_s: int = int(os.getenv("APPSYNC_TIMEOUT", "60"))
    max_attempts: int = int(os.getenv("APPSYNC_MAX_ATTEMPTS", "3"))

    # Use cached client with connection pooling
    client = _get_cached_graphql_client(url=endpoint_url, api_key=api_key, timeout=timeout_s)

    @retry(
        stop=stop_after_attempt(max(1, max_attempts)),
        wait=wait_exponential_jitter(exp_base=2, max=10),
        reraise=True,
    )
    def _do_get() -> dict[str, Any]:
        return client.execute(GET_ACTIVITY_QUERY, variable_values=variables)

    response = _do_get()
    return response


def load_activity_from_graphql(
    *, activity_id: str, config: AwsConfig | None = None
) -> dict[str, Any]:
    """Load a single activity from GraphQL and convert to the expected format.

    Args:
        activity_id: The activity ID to retrieve.
        config: AWS configuration object (uses default if not provided).

    Returns:
        Dictionary containing activity data in the same format as Athena queries.
    """

    response = get_activity(activity_id=activity_id, config=config)

    if not response.get("getActivity") or not response["getActivity"]:
        raise ValueError(f"Activity {activity_id} not found")

    activity_data: dict[str, Any] = response["getActivity"][0]

    # Convert to the format expected by the pipeline (matching Athena query format)
    # TODO this is weird. We should figure out why this is happening.
    activity_row = {
        "activityId": activity_data["activityId"],
        "storyId": activity_data["storyId"],
        "studentId": activity_data["studentId"],
        "createdAt": activity_data["createdAt"],
        "status": "completed",  # Default status since GraphQL doesn't return this
        "displayStatus": "completed",  # Default display status
    }

    return activity_row
