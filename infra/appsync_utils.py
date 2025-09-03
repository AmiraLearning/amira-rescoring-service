import os
from typing import Any, Final

from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from loguru import logger
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from utils.config import AwsConfig  # TODO rename to AWSConfig

_GRAPHQL_ENDPOINT_URL: Final[str] = os.environ.get("APPSYNC_URL", "")
_GRAPHQL_ENDPOINT_KEY: Final[str] = os.environ.get("APPSYNC_API_KEY", "")
_GRAPHQL_ALLOW_MOCK: Final[bool] = os.environ.get("APPSYNC_ALLOW_MOCK", "false").lower() == "true"

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


class UpdateActivityFieldsInput(BaseModel):
    """Input for updating activity fields in AppSync.

    Attributes:
        activityId: Unique identifier for the activity.
        fieldValues: Dictionary of field names and values to update.
    """

    activityId: str
    fieldValues: dict[str, Any]


def set_activity_fields(
    *, activity_id: str, field_values: dict[str, Any], config: AwsConfig
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

    if not _GRAPHQL_ENDPOINT_URL or not _GRAPHQL_ENDPOINT_KEY:
        if _GRAPHQL_ALLOW_MOCK:
            logger.warning(
                "Missing AppSync credentials; returning mock response due to APPSYNC_ALLOW_MOCK=true"
            )
            return {"data": {"updateActivity": {"activityId": activity_id}}}
        raise RuntimeError("APPSYNC_URL and APPSYNC_API_KEY must be set")

    timeout_s: int = int(os.getenv("APPSYNC_TIMEOUT", "60"))
    max_attempts: int = int(os.getenv("APPSYNC_MAX_ATTEMPTS", "3"))
    transport = RequestsHTTPTransport(
        url=_GRAPHQL_ENDPOINT_URL,
        headers={"x-api-key": _GRAPHQL_ENDPOINT_KEY},
        timeout=timeout_s,
    )
    client: Client = Client(transport=transport)

    @retry(
        stop=stop_after_attempt(max(1, max_attempts)),
        wait=wait_exponential_jitter(exp_base=2, max=10),
        reraise=True,
    )
    def _do_update() -> dict[str, Any]:
        return client.execute(UPDATE_ACTIVITY_FIELDS_MUTATION, variable_values=variables.dict())

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

    return response


def query_activities_with_filter(
    *, activity_filter: dict[str, Any], limit: int = 10
) -> dict[str, Any]:
    """Query activities using ActivityFilter.

    Args:
        activity_filter: Dictionary containing activity filter parameters.
        limit: Maximum number of activities to return.

    Returns:
        The response data from the AppSync API.
    """
    variables = {"filter": activity_filter, "limit": limit}

    if not _GRAPHQL_ENDPOINT_URL or not _GRAPHQL_ENDPOINT_KEY:
        if _GRAPHQL_ALLOW_MOCK:
            logger.warning(
                "Missing AppSync credentials; returning mock due to APPSYNC_ALLOW_MOCK=true"
            )
            return {"data": {"activities": []}}
        raise RuntimeError("APPSYNC_URL and APPSYNC_API_KEY must be set")

    timeout_s: int = int(os.getenv("APPSYNC_TIMEOUT", "60"))
    client: Client = Client(
        transport=RequestsHTTPTransport(
            url=_GRAPHQL_ENDPOINT_URL,
            headers={"x-api-key": _GRAPHQL_ENDPOINT_KEY},
            timeout=timeout_s,
        )
    )

    response: dict[str, Any] = client.execute(
        ACTIVITIES_WITH_FILTER_QUERY, variable_values=variables
    )

    return response


def introspect_schema() -> dict[str, Any]:
    """Perform GraphQL introspection to understand the schema.

    Returns:
        The schema introspection data from the AppSync API.
    """
    if not _GRAPHQL_ENDPOINT_URL or not _GRAPHQL_ENDPOINT_KEY:
        if _GRAPHQL_ALLOW_MOCK:
            logger.warning(
                "Missing AppSync credentials; returning mock due to APPSYNC_ALLOW_MOCK=true"
            )
            return {"data": {"__schema": {}}}
        raise RuntimeError("APPSYNC_URL and APPSYNC_API_KEY must be set")

    timeout_s: int = int(os.getenv("APPSYNC_TIMEOUT", "60"))
    client: Client = Client(
        transport=RequestsHTTPTransport(
            url=_GRAPHQL_ENDPOINT_URL,
            headers={"x-api-key": _GRAPHQL_ENDPOINT_KEY},
            timeout=timeout_s,
        )
    )

    response: dict[str, Any] = client.execute(INTROSPECTION_QUERY)
    return response


def get_activity(*, activity_id: str) -> dict[str, Any]:
    """Get a specific activity by ID.

    Args:
        activity_id: The activity ID to retrieve.

    Returns:
        The response data from the AppSync API.
    """
    # TODO unused imports

    variables = {"activityId": [activity_id]}

    if not _GRAPHQL_ENDPOINT_URL or not _GRAPHQL_ENDPOINT_KEY:
        if _GRAPHQL_ALLOW_MOCK:
            logger.warning(
                "Missing AppSync credentials; returning mock due to APPSYNC_ALLOW_MOCK=true"
            )
            return {"getActivity": []}
        raise RuntimeError("APPSYNC_URL and APPSYNC_API_KEY must be set")

    timeout_s: int = int(os.getenv("APPSYNC_TIMEOUT", "60"))
    max_attempts: int = int(os.getenv("APPSYNC_MAX_ATTEMPTS", "3"))
    transport = RequestsHTTPTransport(
        url=_GRAPHQL_ENDPOINT_URL,
        headers={"x-api-key": _GRAPHQL_ENDPOINT_KEY},
        timeout=timeout_s,
    )
    client: Client = Client(transport=transport)

    @retry(
        stop=stop_after_attempt(max(1, max_attempts)),
        wait=wait_exponential_jitter(exp_base=2, max=10),
        reraise=True,
    )
    def _do_get() -> dict[str, Any]:
        return client.execute(GET_ACTIVITY_QUERY, variable_values=variables)

    response = _do_get()
    return response

    # This should never be reached due to the raise in the loop, but satisfies mypy
    raise RuntimeError("Failed to get activity after all retry attempts")


def load_activity_from_graphql(*, activity_id: str) -> dict[str, Any]:
    """Load a single activity from GraphQL and convert to the expected format.

    Args:
        activity_id: The activity ID to retrieve.

    Returns:
        Dictionary containing activity data in the same format as Athena queries.
    """

    response = get_activity(activity_id=activity_id)

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
