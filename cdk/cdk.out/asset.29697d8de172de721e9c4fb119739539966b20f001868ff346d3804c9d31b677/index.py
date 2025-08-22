import json
import os
import time
import typing as t

import boto3

sqs = boto3.client("sqs")
QUEUE_URL = os.environ["JOBS_QUEUE_URL"]


def _chunk(iterable: t.List[str], size: int) -> t.Iterable[t.List[str]]:
    for i in range(0, len(iterable), size):
        yield iterable[i : i + size]


def handler(event, context):
    try:
        if "body" in event and isinstance(event["body"], str):
            payload = json.loads(event["body"])
        else:
            payload = event
        activity_ids = payload.get("activity_ids")
        if not isinstance(activity_ids, list) or not all(
            isinstance(x, str) for x in activity_ids
        ):
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "activity_ids must be a list[str]"}),
            }
        dataset = payload.get("dataset")
        replay_suffix = payload.get("replaySuffix")
        source = payload.get("source", "manual")
        timestamp = int(time.time())

        sent = 0
        for batch in _chunk(activity_ids, 10):
            entries = []
            for idx, aid in enumerate(batch):
                body = {
                    "activityId": aid,
                    "dataset": dataset,
                    "replaySuffix": replay_suffix,
                    "timestamp": timestamp,
                    "source": source,
                }
                entries.append(
                    {
                        "Id": f"{timestamp}-{idx}",
                        "MessageBody": json.dumps(body),
                    }
                )
            resp = sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=entries)
            sent += len(resp.get("Successful", []))
            if resp.get("Failed"):
                return {
                    "statusCode": 207,
                    "body": json.dumps({"sent": sent, "failed": resp["Failed"]}),
                }
        return {"statusCode": 200, "body": json.dumps({"sent": sent})}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
