#!/usr/bin/env python3
"""Script to test Slack notifications for the pipeline."""

import os
import sys

# Mock the lambda modules for testing
sys.path.insert(0, "../lambda/slack_notifier")


def test_kickoff_notification() -> None:
    """Test pipeline kickoff notification."""
    from index import SlackNotification

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("⚠️  SLACK_WEBHOOK_URL not set, skipping test")
        return

    notifier = SlackNotification(webhook_url)

    # Test kickoff message
    message = notifier.format_pipeline_kickoff_message(jobs_enqueued=250, environment="dev2")

    print("📤 Sending kickoff notification...")
    success = notifier.send_message(message)

    if success:
        print("✅ Kickoff notification sent successfully!")
    else:
        print("❌ Failed to send kickoff notification")


def test_completion_notification() -> None:
    """Test pipeline completion notification."""
    from index import JobSummary, SlackNotification

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("⚠️  SLACK_WEBHOOK_URL not set, skipping test")
        return

    notifier = SlackNotification(webhook_url)

    # Test successful completion
    summary = JobSummary(
        total_enqueued=250,
        total_completed=245,
        total_failed=5,
        duration_minutes=65.5,
        avg_processing_time=12.3,
        throughput_per_hour=230,
        dlq_count=0,
    )

    message = notifier.format_job_completion_message(summary=summary, environment="dev2")

    print("📤 Sending completion notification...")
    success = notifier.send_message(message)

    if success:
        print("✅ Completion notification sent successfully!")
    else:
        print("❌ Failed to send completion notification")


def test_error_notification() -> None:
    """Test error alert notification."""
    from index import SlackNotification

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("⚠️  SLACK_WEBHOOK_URL not set, skipping test")
        return

    notifier = SlackNotification(webhook_url)

    # Test error alert
    message = notifier.format_error_alert(
        alarm_name="ProcessingErrorsAlarm",
        alarm_description="High error rate detected in processing Lambda",
        metric_value=15.0,
    )

    print("📤 Sending error notification...")
    success = notifier.send_message(message)

    if success:
        print("✅ Error notification sent successfully!")
    else:
        print("❌ Failed to send error notification")


def main() -> None:
    """Main function."""
    print("🧪 Testing Slack notifications...")
    print("=" * 50)

    # Test all notification types
    test_kickoff_notification()
    print()
    test_completion_notification()
    print()
    test_error_notification()

    print()
    print("🎉 All tests completed!")
    print()
    print("💡 To test with real Slack:")
    print("   export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...")
    print("   python test_slack_notifications.py")


if __name__ == "__main__":
    main()
