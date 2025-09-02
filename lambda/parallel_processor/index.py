import json
import os
import time
import asyncio
from typing import Any

from pydantic import BaseModel
import torch
import boto3

from src.pipeline.pipeline import run_activity_pipeline
from utils.config import PipelineConfig


class LambdaProcessingResult(BaseModel):
    activity_id: str
    status: str
    pipeline_results: dict[str, Any]
    processing_time: float
    timestamp: int
    processor: str


class LambdaResponse(BaseModel):
    status_code: int
    body: str
    batch_item_failures: list[dict[str, Any]] = []


_optimizations_applied = False


def apply_lambda_optimizations():
    global _optimizations_applied
    if _optimizations_applied:
        return

    try:
        cpu_count = max(1, (os.cpu_count() or 6))
        torch.set_num_threads(max(2, cpu_count // 2))
        torch.set_num_interop_threads(2)
        torch.backends.cudnn.benchmark = False

        if hasattr(torch.backends, "openmp") and torch.backends.openmp.is_available():
            torch.backends.openmp.enabled = True

        _optimizations_applied = True

    except Exception:
        pass


def create_lambda_config() -> PipelineConfig:
    from utils.config import AwsConfig, W2VConfig

    aws_config = AwsConfig(
        aws_profile=os.environ.get("AWS_PROFILE", "default"),
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
    )
    w2v2_config = W2VConfig(
        model_path=os.environ["MODEL_PATH"],
        include_confidence=os.environ.get("INCLUDE_CONFIDENCE", "true").lower()
        == "true",
    )
    return PipelineConfig(aws=aws_config, w2v2=w2v2_config)


def publish_job_metrics(result: LambdaProcessingResult):
    try:
        cloudwatch = boto3.client("cloudwatch")

        cloudwatch.put_metric_data(
            Namespace="Amira/Jobs",
            MetricData=[
                {
                    "MetricName": "JobsCompleted",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "Status", "Value": result.status},
                        {"Name": "Processor", "Value": result.processor},
                    ],
                    "Timestamp": result.timestamp,
                },
                {
                    "MetricName": "ProcessingTime",
                    "Value": result.processing_time,
                    "Unit": "Seconds",
                    "Dimensions": [{"Name": "Processor", "Value": result.processor}],
                    "Timestamp": result.timestamp,
                },
            ],
        )
    except Exception:
        pass


def write_results_to_s3(result: LambdaProcessingResult):
    s3_client = boto3.client("s3")
    bucket = os.environ["RESULTS_BUCKET"]
    prefix = os.environ.get("RESULTS_PREFIX", "results/")

    dt_prefix = time.strftime("dt=%Y-%m-%d")
    base_key = f"{prefix.rstrip('/')}/{dt_prefix}/activity_id={result.activity_id}"

    put_kwargs = {"Bucket": bucket, "ContentType": "application/json"}
    if os.environ.get("KMS_KEY_ID"):
        put_kwargs.update(
            {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": os.environ["KMS_KEY_ID"]}
        )

    s3_client.put_object(
        Key=f"{base_key}/results.json", Body=result.model_dump_json(), **put_kwargs
    )

    s3_client.put_object(Key=f"{base_key}/_SUCCESS", Body=b"", **put_kwargs)


async def process_activity(activity_id: str) -> LambdaProcessingResult:
    start_time = time.time()
    apply_lambda_optimizations()

    config = create_lambda_config()
    config.metadata.activity_id = activity_id

    results = await run_activity_pipeline(config=config)

    if not results:
        raise ValueError("Pipeline returned no results")

    result = LambdaProcessingResult(
        activity_id=activity_id,
        status="processed",
        pipeline_results=results[0],
        processing_time=time.time() - start_time,
        timestamp=int(time.time()),
        processor="existing-pipeline-arm64",
    )

    write_results_to_s3(result)
    publish_job_metrics(result)
    return result


def publish_batch_metrics(successes: int, failures: int, total_time: float):
    try:
        cloudwatch = boto3.client("cloudwatch")

        metrics = []
        if failures > 0:
            metrics.append(
                {
                    "MetricName": "JobsFailed",
                    "Value": failures,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "Processor", "Value": "existing-pipeline-arm64"}
                    ],
                }
            )

        if successes > 0:
            metrics.append(
                {
                    "MetricName": "BatchThroughput",
                    "Value": successes / (total_time / 60) if total_time > 0 else 0,
                    "Unit": "Count/Minute",
                    "Dimensions": [
                        {"Name": "Processor", "Value": "existing-pipeline-arm64"}
                    ],
                }
            )

        if metrics:
            cloudwatch.put_metric_data(Namespace="Amira/Jobs", MetricData=metrics)
    except Exception:
        pass


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    handler_start = time.time()

    try:
        records = event.get("Records", [])
        successes = 0
        failures = 0
        batch_item_failures = []
        total_processing_time = 0.0

        for record in records:
            try:
                body = json.loads(record.get("body", "{}"))
                activity_id = body.get("activityId")

                if not activity_id:
                    raise ValueError("Missing activityId")

                result = asyncio.run(process_activity(activity_id))
                total_processing_time += result.processing_time
                successes += 1

            except Exception:
                batch_item_failures.append({"itemIdentifier": record.get("messageId")})
                failures += 1

        handler_time = time.time() - handler_start
        publish_batch_metrics(successes, failures, handler_time)

        response = LambdaResponse(
            status_code=200,
            body=json.dumps(
                {
                    "processed": successes,
                    "failed": failures,
                    "total": len(records),
                    "handlerTime": handler_time,
                    "avgProcessingTime": total_processing_time / successes
                    if successes > 0
                    else 0.0,
                    "runtime": "existing-pipeline-arm64",
                }
            ),
            batch_item_failures=batch_item_failures,
        )

        return response.model_dump()

    except Exception as e:
        return LambdaResponse(
            status_code=500,
            body=json.dumps(
                {"error": str(e), "handlerTime": time.time() - handler_start}
            ),
        ).model_dump()
