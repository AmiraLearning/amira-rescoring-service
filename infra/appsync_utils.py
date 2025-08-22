from gql import gql, Client
from pydantic import BaseModel
from typing import Any
from utils.config import Config
from gql.transport.requests import RequestsHTTPTransport

UPDATE_ACTIVITY_FIELDS_MUTATION: str = gql(
    """
    mutation UpdateActivityFields($activityId: ID!, $fieldValues: AWSJSON!) {
        updateActivity(activityId: $activityId, fieldValues: $fieldValues) {
            activityId
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
    *, activity_id: str, field_values: dict[str, Any], config: Config
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

    client: Client = Client(transport=RequestsHTTPTransport(url=config.appsync_url))

    response: dict[str, Any] = client.execute_query(
        query=UPDATE_ACTIVITY_FIELDS_MUTATION, variables=variables
    )

    return response
