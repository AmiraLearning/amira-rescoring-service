import asyncio
import json
import os
import time
from typing import Any

import boto3
import torch
from pydantic import BaseModel

from src.pipeline.inference.models import W2VConfig
from src.pipeline.pipeline import run_activity_pipeline
from utils.config import PipelineConfig
from utils.logging import setup_logging


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
_config_cache: PipelineConfig | None = None
_cloudwatch_client: Any | None = None
_s3_client: Any | None = None


def apply_lambda_optimizations() -> None:
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
    from utils.config import AwsConfig

    aws_config = AwsConfig(
        aws_profile=os.environ.get("AWS_PROFILE", "default"),
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
    )
    use_triton = os.environ.get("USE_TRITON", "false").lower() == "true"
    include_confidence = os.environ.get("INCLUDE_CONFIDENCE", "true").lower() == "true"
    batch_all_phrases = os.environ.get("BATCH_ALL_PHRASES", "true").lower() == "true"
    model_path = os.environ.get("MODEL_PATH", "")
    triton_url = os.environ.get("TRITON_URL", "")
    triton_model = os.environ.get("TRITON_MODEL", "w2v2")

    w2v2_config = W2VConfig(
        model_path=model_path,
        include_confidence=include_confidence,
        use_triton=use_triton,
        triton_url=triton_url or "",
        triton_model=triton_model,
        batch_all_phrases=batch_all_phrases,
    )
    return PipelineConfig(aws=aws_config, w2v2=w2v2_config)


def _get_cloudwatch_client() -> Any:
    global _cloudwatch_client
    if _cloudwatch_client is None:
        _cloudwatch_client = boto3.client("cloudwatch")
    return _cloudwatch_client


def publish_job_metrics(result: LambdaProcessingResult) -> None:
    try:
        cw_client = _get_cloudwatch_client()
        cw_client.put_metric_data(
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


def _get_s3_client() -> Any:
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def write_results_to_s3(result: LambdaProcessingResult) -> None:
    s3_client = _get_s3_client()
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
    global _config_cache
    if _config_cache is None:
        _config_cache = create_lambda_config()
    config = _config_cache
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


def publish_batch_metrics(successes: int, failures: int, total_time: float) -> None:
    try:
        cw_client = _get_cloudwatch_client()

        metrics = []
        if failures > 0:
            metrics.append(
                {
                    "MetricName": "JobsFailed",
                    "Value": failures,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Processor", "Value": "existing-pipeline-arm64"}],
                }
            )

        if successes > 0:
            metrics.append(
                {
                    "MetricName": "BatchThroughput",
                    "Value": successes / (total_time / 60) if total_time > 0 else 0,
                    "Unit": "Count/Minute",
                    "Dimensions": [{"Name": "Processor", "Value": "existing-pipeline-arm64"}],
                }
            )

        if metrics:
            cw_client.put_metric_data(Namespace="Amira/Jobs", MetricData=metrics)
    except Exception:
        pass


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    setup_logging(service="lambda-parallel-processor")
    handler_start = time.time()

    try:
        records = event.get("Records", [])
        successes = 0
        failures = 0
        batch_item_failures: list[dict[str, Any]] = []
        total_processing_time = 0.0

        max_concurrency_env = os.environ.get("MAX_CONCURRENCY")
        try:
            max_concurrency = int(max_concurrency_env) if max_concurrency_env else 8
        except ValueError:
            max_concurrency = 8

        async def _process_record(
            record: dict[str, Any], sem: asyncio.Semaphore
        ) -> tuple[bool, float, str | None]:
            async with sem:
                try:
                    body_text = record.get("body", "{}")
                    body = json.loads(body_text) if isinstance(body_text, str) else body_text
                    activity_id = body.get("activityId")
                    if not activity_id:
                        raise ValueError("Missing activityId")
                    result = await process_activity(activity_id)
                    return True, result.processing_time, None
                except Exception:
                    return False, 0.0, record.get("messageId")

        async def _run(
            records_list: list[dict[str, Any]],
        ) -> tuple[int, int, float, list[dict[str, Any]]]:
            sem = asyncio.Semaphore(max_concurrency)
            tasks = [asyncio.create_task(_process_record(r, sem)) for r in records_list]
            done = await asyncio.gather(*tasks, return_exceptions=False)
            ok = sum(1 for s, _, _ in done if s)
            fail = sum(1 for s, _, _ in done if not s)
            total_time = sum(t for s, t, _ in done if s)
            failures_list = [{"itemIdentifier": mid} for s, _, mid in done if not s and mid]
            return ok, fail, total_time, failures_list

        ok, fail, total_time_success, failures_list = asyncio.run(_run(records))
        successes += ok
        failures += fail
        total_processing_time += total_time_success
        batch_item_failures.extend(failures_list)

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
            body=json.dumps({"error": str(e), "handlerTime": time.time() - handler_start}),
        ).model_dump()
