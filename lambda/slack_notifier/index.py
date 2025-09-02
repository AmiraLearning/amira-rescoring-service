import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel


class JobSummary(BaseModel):
    total_enqueued: int
    total_completed: int
    total_failed: int
    duration_minutes: float
    avg_processing_time: float
    throughput_per_hour: float
    dlq_count: int


class SlackNotification:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def format_job_completion_message(self, summary: JobSummary) -> dict[str, Any]:
        success_rate = (
            (summary.total_completed / summary.total_enqueued * 100)
            if summary.total_enqueued > 0
            else 0
        )

        status_emoji = "✅" if summary.dlq_count == 0 else "⚠️"
        color = "good" if summary.dlq_count == 0 else "warning"

        return {
            "text": f"{status_emoji} Amira Letter Scoring Job Complete",
            "attachments": [
                {
                    "color": color,
                    "fields": [
                        {
                            "title": "Processing Summary",
                            "value": f"• Total Jobs: {summary.total_enqueued:,}\n• Completed: {summary.total_completed:,}\n• Failed: {summary.total_failed:,}\n• Success Rate: {success_rate:.1f}%",
                            "short": True,
                        },
                        {
                            "title": "Performance",
                            "value": f"• Duration: {summary.duration_minutes:.1f}m\n• Avg Time: {summary.avg_processing_time:.1f}s\n• Throughput: {summary.throughput_per_hour:.0f}/hour",
                            "short": True,
                        },
                    ],
                    "footer": "Amira Lambda Parallel",
                    "ts": int(time.time()),
                }
            ],
        }

    def format_error_alert(
        self, alarm_name: str, alarm_description: str, metric_value: float
    ) -> dict[str, Any]:
        return {
            "text": f"🚨 Amira Processing Alert: {alarm_name}",
            "attachments": [
                {
                    "color": "danger",
                    "fields": [
                        {
                            "title": "Alert Details",
                            "value": f"• Alarm: {alarm_name}\n• Description: {alarm_description}\n• Current Value: {metric_value}",
                            "short": False,
                        }
                    ],
                    "footer": "Amira Lambda Parallel",
                    "ts": int(time.time()),
                }
            ],
        }

    def send_message(self, message: dict[str, Any]) -> bool:
        try:
            data = json.dumps(message).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status == 200

        except Exception:
            return False


def extract_job_metrics_from_cloudwatch(event: dict[str, Any]) -> JobSummary:
    """Extract job metrics from CloudWatch event data.

    Args:
        event: CloudWatch alarm event containing metric data

    Returns:
        JobSummary: Processed metrics summary
    """
    import boto3
    from datetime import datetime, timedelta

    cloudwatch = boto3.client("cloudwatch")

    # Extract alarm details
    alarm_data = event.get("AlarmData", {})
    alarm_name = event.get("AlarmName", "Unknown")

    # Set time range for metrics query (last hour)
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=1)

    try:
        # Query job completion metrics
        completed_response = cloudwatch.get_metric_statistics(
            Namespace="Amira/Jobs",
            MetricName="JobsCompleted",
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,  # 1 hour
            Statistics=["Sum"],
        )

        failed_response = cloudwatch.get_metric_statistics(
            Namespace="Amira/Jobs",
            MetricName="JobsFailed",
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=["Sum"],
        )

        processing_time_response = cloudwatch.get_metric_statistics(
            Namespace="Amira/Jobs",
            MetricName="ProcessingTime",
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=["Average"],
        )

        # Extract metrics or use defaults
        completed_count = 0
        failed_count = 0
        avg_processing_time = 0.0

        if completed_response["Datapoints"]:
            completed_count = int(completed_response["Datapoints"][0].get("Sum", 0))

        if failed_response["Datapoints"]:
            failed_count = int(failed_response["Datapoints"][0].get("Sum", 0))

        if processing_time_response["Datapoints"]:
            avg_processing_time = float(
                processing_time_response["Datapoints"][0].get("Average", 0)
            )

        total_jobs = completed_count + failed_count
        duration_minutes = 60.0  # 1 hour window
        throughput_per_hour = int(total_jobs) if total_jobs > 0 else 0

        return JobSummary(
            total_enqueued=total_jobs,  # Approximation
            total_completed=completed_count,
            total_failed=failed_count,
            duration_minutes=duration_minutes,
            avg_processing_time=avg_processing_time,
            throughput_per_hour=throughput_per_hour,
            dlq_count=0,  # Would need separate DLQ query
        )

    except Exception as e:
        # Fallback to alarm event data if CloudWatch query fails
        return JobSummary(
            total_enqueued=1,
            total_completed=0 if "fail" in alarm_name.lower() else 1,
            total_failed=1 if "fail" in alarm_name.lower() else 0,
            duration_minutes=1.0,
            avg_processing_time=60.0,
            throughput_per_hour=1,
            dlq_count=1 if "dlq" in alarm_name.lower() else 0,
        )


def parse_sns_message(event: dict[str, Any]) -> dict[str, Any]:
    records: list[dict[str, Any]] = event.get("Records", [])
    if not records:
        return {}

    sns_message: dict[str, Any] = records[0].get("Sns", {})
    message_text: str = sns_message.get("Message", "{}")

    try:
        return json.loads(message_text)
    except json.JSONDecodeError:
        return {"RawMessage": message_text}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return {"statusCode": 400, "body": "SLACK_WEBHOOK_URL not configured"}

    notifier = SlackNotification(webhook_url)

    try:
        sns_data = parse_sns_message(event)

        if sns_data.get("AlarmName") == "JobCompletionDetected":
            summary = extract_job_metrics_from_cloudwatch(event)
            message = notifier.format_job_completion_message(summary)
        else:
            alarm_name: str = sns_data.get("AlarmName", "Unknown")
            alarm_description: str = sns_data.get("AlarmDescription", "No description")
            metric_value: float = sns_data.get("NewStateValue", 0)

            message = notifier.format_error_alert(
                alarm_name, alarm_description, metric_value
            )

        success = notifier.send_message(message)

        return {
            "statusCode": 200 if success else 500,
            "body": json.dumps(
                {"sent": success, "timestamp": datetime.now(timezone.utc).isoformat()}
            ),
        }

    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
