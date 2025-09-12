import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
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

    def format_pipeline_kickoff_message(
        self, jobs_enqueued: int, environment: str = "unknown"
    ) -> dict[str, Any]:
        """Format pipeline kickoff notification."""
        return {
            "text": "Amira Letter Scoring Pipeline Started",
            "attachments": [
                {
                    "color": "good",
                    "fields": [
                        {
                            "title": "Pipeline Details",
                            "value": f"• Environment: {environment}\n• Jobs Enqueued: {jobs_enqueued:,}\n• Status: Processing started",
                            "short": True,
                        },
                        {
                            "title": "Expected Duration",
                            "value": f"• Est. Time: {self._estimate_completion_time(jobs_enqueued)}\n• Concurrency: 10 parallel workers\n• Avg per job: ~45s",
                            "short": True,
                        },
                    ],
                    "footer": "Amira Lambda Parallel - Pipeline Started",
                    "ts": int(time.time()),
                }
            ],
        }

    def _estimate_completion_time(self, jobs_count: int) -> str | None:
        """Estimate completion time for parallel pipeline based on job count."""
        # THIS ESTIMATE IS BOGUS
        # if jobs_count == 0:
        #     return "0 minutes"
        # estimated_minutes = max(
        #     1, (jobs_count * 45) // (10 * 60)
        # )
        # if estimated_minutes < 60:
        #     return f"~{estimated_minutes} minutes"
        # else:
        #     hours = estimated_minutes // 60
        #     minutes = estimated_minutes % 60
        #     return f"~{hours}h {minutes}m"

    def format_job_completion_message(
        self, summary: JobSummary, environment: str = "unknown"
    ) -> dict[str, Any]:
        success_rate = (
            (summary.total_completed / summary.total_enqueued * 100)
            if summary.total_enqueued > 0
            else 0
        )

        if success_rate >= 95 and summary.dlq_count == 0:
            color = "good"
            status_text = "Excellent"
        elif success_rate >= 80 and summary.dlq_count < 5:
            color = "good"
            status_text = "Success"
        elif success_rate >= 50:
            color = "warning"
            status_text = "Completed with Issues"
        else:
            color = "danger"
            status_text = "Failed"

        return {
            "text": f"Amira Letter Scoring Pipeline Complete - {status_text}",
            "attachments": [
                {
                    "color": color,
                    "fields": [
                        {
                            "title": "Processing Summary",
                            "value": f"• Environment: {environment}\n• Total Jobs: {summary.total_enqueued:,}\n• Completed: {summary.total_completed:,}\n• Failed: {summary.total_failed:,}\n• Success Rate: {success_rate:.1f}%",
                            "short": True,
                        },
                        {
                            "title": "Performance Metrics",
                            "value": f"• Duration: {summary.duration_minutes:.1f}m\n• Avg Processing: {summary.avg_processing_time:.1f}s\n• Throughput: {summary.throughput_per_hour:.0f}/hour\n• DLQ Items: {summary.dlq_count}",
                            "short": True,
                        },
                    ],
                    "footer": "Amira Lambda Parallel - Pipeline Complete",
                    "ts": int(time.time()),
                }
            ],
        }

    def format_error_alert(
        self, alarm_name: str, alarm_description: str, metric_value: float
    ) -> dict[str, Any]:
        """Format error alert notification.

        Args:
            alarm_name: Name of the alarm.
            alarm_description: Description of the alarm.
            metric_value: Value of the metric.

        Returns:
            Dictionary containing the error alert notification.
        """
        return {
            "text": f"Amira Processing Alert: {alarm_name}",
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
        """Send message to Slack.

        Args:
            message: Dictionary containing the message to send.

        Returns:
            Boolean indicating whether the message was sent successfully.
        """
        try:
            data: bytes = json.dumps(message).encode("utf-8")
            req: urllib.request.Request = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status == 200

        except urllib.error.URLError as e:
            print(f"Network error sending Slack message: {e}")
            return False
        except Exception as e:
            print(f"Unexpected error sending Slack message: {e}")
            return False


def extract_job_metrics_from_cloudwatch(event: dict[str, Any]) -> JobSummary:
    """Extract job metrics from CloudWatch event data.

    Args:
        event: CloudWatch alarm event containing metric data

    Returns:
        JobSummary: Processed metrics summary
    """
    from datetime import datetime, timedelta

    import boto3

    cloudwatch = boto3.client("cloudwatch")

    event.get("AlarmData", {})
    alarm_name: str = event.get("AlarmName", "Unknown")

    end_time: datetime = datetime.utcnow()
    start_time = end_time - timedelta(hours=1)

    try:
        completed_response: dict[str, Any] = cloudwatch.get_metric_statistics(
            Namespace="Amira/Jobs",
            MetricName="JobsCompleted",
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=["Sum"],
        )

        failed_response: dict[str, Any] = cloudwatch.get_metric_statistics(
            Namespace="Amira/Jobs",
            MetricName="JobsFailed",
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=["Sum"],
        )

        processing_time_response: dict[str, Any] = cloudwatch.get_metric_statistics(
            Namespace="Amira/Jobs",
            MetricName="ProcessingTime",
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=["Average"],
        )

        completed_count: int = 0
        failed_count: int = 0
        avg_processing_time: float = 0.0

        if completed_response["Datapoints"]:
            completed_count = int(completed_response["Datapoints"][0].get("Sum", 0))

        if failed_response["Datapoints"]:
            failed_count = int(failed_response["Datapoints"][0].get("Sum", 0))

        if processing_time_response["Datapoints"]:
            avg_processing_time = float(processing_time_response["Datapoints"][0].get("Average", 0))

        total_jobs: int = completed_count + failed_count
        duration_minutes: float = 60.0  # 1 hour window
        throughput_per_hour: int = int(total_jobs) if total_jobs > 0 else 0

        return JobSummary(
            total_enqueued=total_jobs,  # Approximation
            total_completed=completed_count,
            total_failed=failed_count,
            duration_minutes=duration_minutes,
            avg_processing_time=avg_processing_time,
            throughput_per_hour=throughput_per_hour,
            dlq_count=0,
        )

    except Exception:
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
    environment = os.environ.get("AUDIO_ENV", "unknown")

    try:
        if event.get("source") == "pipeline_kickoff":
            jobs_enqueued = event.get("jobs_enqueued", 0)
            message = notifier.format_pipeline_kickoff_message(jobs_enqueued, environment)
        else:
            sns_data = parse_sns_message(event)

            if sns_data.get("AlarmName") == "JobCompletionDetected":
                summary = extract_job_metrics_from_cloudwatch(sns_data)
                message = notifier.format_job_completion_message(summary, environment)
            else:
                alarm_name: str = sns_data.get("AlarmName", "Unknown")
                alarm_description: str = sns_data.get("AlarmDescription", "No description")
                metric_value: float = sns_data.get("NewStateValue", 0)

                message = notifier.format_error_alert(alarm_name, alarm_description, metric_value)

        success = notifier.send_message(message)

        return {
            "statusCode": 200 if success else 500,
            "body": json.dumps({"sent": success, "timestamp": datetime.now(UTC).isoformat()}),
        }

    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
