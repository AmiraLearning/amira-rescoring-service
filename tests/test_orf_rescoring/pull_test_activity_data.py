import json
from pathlib import Path
from typing import Any

from fire import Fire
from tqdm import tqdm

from amira_pyutils.environment import Environment, Environments
from amira_pyutils.appsync import AppSync
from src.orf_rescoring_pipeline.alignment.phrase_manifest import (
    PhraseBuilder,
    PhraseManifest,
)


def pull_srs_data(env_name: str, activity_ids: list[str]) -> None:
    """
    This util pulls the SRS data for the given activity IDs so that we can add integration tests with real data.

    Note: This util only pulls the SRS data.

    We still need to get:
    - re-transcribed phrases from kaldi and w2v
    - retouched errors
    - timing files with page and phrase alignment information
    For those, we can run the pipeline in debug mode and update this json file manually with the correct data.
    """
    env = Environments.find(env_name)
    appsync = AppSync(env=env)

    save_path = Path("tests/input_data")
    save_path.mkdir(parents=True, exist_ok=True)

    data = appsync.get_activity_details(
        activity_ids=activity_ids,
        fields=[
            "activityId",
            "storyId",
            "story.chapters.phrases",
            "story.tags",
            "errors",
        ],
        show_progress=True,
    )
    for activity in tqdm(data):
        features = appsync.get_custom_activity_features(
            activity_id=activity["activityId"],
            feature_list=[
                "model",
                "phraseIndex",
                "Kaldi_match",
                "W2V_match",
                "we_dist",
                "correct_confidences",
            ],
        )
        features = sorted(features, key=lambda x: x["phraseIndex"])
        activity["model_features"] = features
        with open(save_path / f"{activity['activityId']}.json", "w") as f:
            json.dump(activity, f)


def pull_phrase_manifest(activity_id: str) -> list[Any]:
    """
    Util to export the phrase manifest for a given activity ID.
    This util is helpful for creating the test manifest files for the integration tests.

    Required environment variable: AWS_PROFILE (else it will use default profile)

    Example command:
    AWS_PROFILE=prod2 python pull_test_activity_data.py pull_phrase_manifest <ACTIVITY_ID>
    """
    import os

    import amira_pyutils.s3 as s3_utils
    from amira_pyutils.environment import Environment

    # Since this code is borrowed from the slicing service, we do not test it in this repo
    # Rather we will use it to create mocks for the integration tests
    # So that we do not need to make real S3 calls during the tests

    # Copy the returned value from this file into the test_manifest.py file

    env_name = os.environ["AWS_PROFILE"]
    env = Environments.find(env_name)
    builder = PhraseBuilder(s3_client=s3_utils.S3Service())
    manifest = PhraseManifest(builder=builder)
    phrases = manifest.generate(bucket=env.audio_bucket, activity_id=activity_id)
    return [phrase.to_dict() for phrase in phrases]


if __name__ == "__main__":
    Fire({"pull_srs_data": pull_srs_data, "pull_phrase_manifest": pull_phrase_manifest})
