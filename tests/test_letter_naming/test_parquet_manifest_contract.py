import pyarrow as pa


def test_parquet_manifest_schema() -> None:
    schema = pa.schema(
        [
            pa.field("activityId", pa.string()),
            pa.field("status", pa.string()),
        ]
    )

    table = pa.Table.from_pylist(
        [
            {"activityId": "a-1", "status": "processed"},
            {"activityId": "a-2", "status": "processed"},
        ],
        schema=schema,
    )

    assert table.schema == schema
    assert table.num_rows == 2
