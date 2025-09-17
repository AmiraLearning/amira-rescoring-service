from infra.contracts import UpdateActivityResponse


def test_update_activity_response_contract() -> None:
    sample = {"data": {"updateActivity": {"activityId": "abc123"}}}
    UpdateActivityResponse.model_validate(sample)
