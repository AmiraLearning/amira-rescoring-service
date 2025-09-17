import json
from pathlib import Path

import jsonschema


def test_segment_manifest_contract_shape() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1] / "docs" / "schemas" / "segment-manifest-v1.json"
    )
    with schema_path.open("r", encoding="utf-8") as f:
        schema = json.load(f)

    sample = {
        "activityId": "a-1",
        "phraseIndex": 0,
        "start_ms": 0.0,
        "end_ms": 1234.5,
        "s3_uri": "s3://bucket/key.wav",
    }

    jsonschema.validate(sample, schema)
