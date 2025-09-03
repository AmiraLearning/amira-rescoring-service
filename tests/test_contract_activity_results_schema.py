import jsonschema

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://amira.example/schema/activity-results-v1.json",
    "title": "ActivityResultsV1",
    "type": "object",
    "required": [
        "activity_id",
        "status",
        "pipeline_results",
        "processing_time",
        "timestamp",
        "processor",
        "schema_version",
    ],
    "properties": {
        "activity_id": {"type": "string"},
        "status": {"type": "string"},
        "pipeline_results": {"type": "object"},
        "processing_time": {"type": "number"},
        "timestamp": {"type": "number"},
        "processor": {"type": "string"},
        "schema_version": {"const": "activity-results-v1"},
        "correlation_id": {"type": ["string", "null"]},
    },
    "additionalProperties": True,
}


def test_activity_results_contract_shape() -> None:
    sample = {
        "activity_id": "a-1",
        "status": "processed",
        "pipeline_results": {"data": {"updateActivity": {"activityId": "a-1"}}},
        "processing_time": 1.23,
        "timestamp": 1736036400,
        "processor": "existing-pipeline-arm64",
        "schema_version": "activity-results-v1",
        "correlation_id": "abc123",
    }

    jsonschema.validate(sample, SCHEMA)
