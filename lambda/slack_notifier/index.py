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
        success_rate = (summary.total_completed / summary.total_enqueued * 100) if summary.total_enqueued > 0 else 0
        
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
                            "short": True
                        },
                        {
                            "title": "Performance",
                            "value": f"• Duration: {summary.duration_minutes:.1f}m\n• Avg Time: {summary.avg_processing_time:.1f}s\n• Throughput: {summary.throughput_per_hour:.0f}/hour",
                            "short": True
                        }
                    ],
                    "footer": "Amira Lambda Parallel",
                    "ts": int(time.time())
                }
            ]
        }
    
    def format_error_alert(self, alarm_name: str, alarm_description: str, metric_value: float) -> dict[str, Any]:
        return {
            "text": f"🚨 Amira Processing Alert: {alarm_name}",
            "attachments": [
                {
                    "color": "danger",
                    "fields": [
                        {
                            "title": "Alert Details",
                            "value": f"• Alarm: {alarm_name}\n• Description: {alarm_description}\n• Current Value: {metric_value}",
                            "short": False
                        }
                    ],
                    "footer": "Amira Lambda Parallel",
                    "ts": int(time.time())
                }
            ]
        }
    
    def send_message(self, message: dict[str, Any]) -> bool:
        try:
            data = json.dumps(message).encode('utf-8')
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={'Content-Type': 'application/json'}
            )
            
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status == 200
                
        except Exception:
            return False


def extract_job_metrics_from_cloudwatch(event: dict[str, Any]) -> JobSummary:
    # This would normally query CloudWatch metrics
    # For now, return sample data structure
    # TODO: Implement actual logic to extract metrics from CloudWatch
    return JobSummary(
        total_enqueued=1000,
        total_completed=950,
        total_failed=50,
        duration_minutes=45.5,
        avg_processing_time=15.2,
        throughput_per_hour=1316,
        dlq_count=0
    )


def parse_sns_message(event: dict[str, Any]) -> dict[str, Any]:
    records: list[dict[str, Any]] = event.get('Records', [])
    if not records:
        return {}
    
    sns_message: dict[str, Any] = records[0].get('Sns', {})
    message_text: str = sns_message.get('Message', '{}')
    
    try:
        return json.loads(message_text)
    except json.JSONDecodeError:
        return {'RawMessage': message_text}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    webhook_url = os.environ.get('SLACK_WEBHOOK_URL')
    if not webhook_url:
        return {'statusCode': 400, 'body': 'SLACK_WEBHOOK_URL not configured'}
    
    notifier = SlackNotification(webhook_url)
    
    try:
        sns_data = parse_sns_message(event)
        

        if sns_data.get('AlarmName') == 'JobCompletionDetected':
            summary = extract_job_metrics_from_cloudwatch(event)
            message = notifier.format_job_completion_message(summary)
        else:
            alarm_name: str = sns_data.get('AlarmName', 'Unknown')
            alarm_description: str = sns_data.get('AlarmDescription', 'No description')
            metric_value: float = sns_data.get('NewStateValue', 0)
            
            message = notifier.format_error_alert(alarm_name, alarm_description, metric_value)
        
        success = notifier.send_message(message)
        
        return {
            'statusCode': 200 if success else 500,
            'body': json.dumps({
                'sent': success,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
        }
        
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }