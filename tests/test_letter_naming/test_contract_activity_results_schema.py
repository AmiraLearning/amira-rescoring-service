import json
from pathlib import Path

import jsonschema


def test_activity_results_contract_shape() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1] / "docs" / "schemas" / "activity-results-v1.json"
    )
    with schema_path.open("r", encoding="utf-8") as f:
        schema = json.load(f)

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

    jsonschema.validate(sample, schema)
