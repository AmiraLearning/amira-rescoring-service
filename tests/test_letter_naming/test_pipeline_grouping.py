import polars as pl

from src.pipeline.pipeline import ActivityFields, group_activities_by_id


def test_group_activities_by_id_basic() -> None:
    df = pl.DataFrame(
        {
            "activityId": ["a1", "a1", "a2"],
            "phraseIndex": [0, 1, 0],
            "expected_text": [["cat"], ["sat"], ["dog"]],
            "reference_phonemes": [["k", "æ", "t"], ["s", "æ", "t"], ["d", "ɔ", "g"]],
        }
    )

    grouped = group_activities_by_id(phrase_df=df)

    ids = grouped[ActivityFields.ACTIVITY_ID]
    records = grouped[ActivityFields.RECORDS]

    assert set(ids) == {"a1", "a2"}
    assert len(records) == len(ids)
    idx_a1 = ids.index("a1")
    idx_a2 = ids.index("a2")
    assert len(records[idx_a1]) == 2
    assert len(records[idx_a2]) == 1
