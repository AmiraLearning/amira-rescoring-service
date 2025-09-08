#!/usr/bin/env python3
"""
Load test driver for Amira Letter Scoring Platform
Generates controlled load to measure platform performance and capacity
"""

import argparse
import json
import logging
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import boto3

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class LoadDriver:
    def __init__(self, queue_url: str, region: str = "us-east-1"):
        """Initialize load driver with SQS configuration"""
        self.queue_url = queue_url
        self.sqs = boto3.client("sqs", region_name=region)
        self.cloudwatch = boto3.client("cloudwatch", region_name=region)

    def generate_test_message(self, test_id: str, message_id: int) -> dict[str, Any]:
        """Generate a realistic test message for letter scoring"""
        return {
            "id": str(uuid.uuid4()),
            "test_id": test_id,
            "message_id": message_id,
            "activity_id": f"test-activity-{message_id:06d}",
            "audio_bucket": "amira-test-audio",
            "audio_key": f"test-audio/{test_id}/audio-{message_id:06d}.wav",
            "model_path": "facebook/wav2vec2-base-960h",
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": {
                "test_run": True,
                "duration_ms": 5000 + (message_id % 10000),  # Simulate varying audio lengths
                "sample_rate": 16000,
                "channels": 1,
            },
        }

    def send_message_batch(
        self, messages: list[dict[str, Any]], delay_between_sends: float = 0.0
    ) -> list[dict[str, Any]]:
        """Send a batch of messages with optional delay"""
        results = []

        for i, message in enumerate(messages):
            try:
                start_time = time.time()
                response = self.sqs.send_message(
                    QueueUrl=self.queue_url,
                    MessageBody=json.dumps(message),
                    MessageAttributes={
                        "TestId": {"StringValue": message["test_id"], "DataType": "String"},
                        "MessageId": {
                            "StringValue": str(message["message_id"]),
                            "DataType": "Number",
                        },
                    },
                )

                send_time = time.time() - start_time
                results.append(
                    {
                        "message_id": message["message_id"],
                        "sqs_message_id": response["MessageId"],
                        "send_time": send_time,
                        "status": "success",
                    }
                )

                if delay_between_sends > 0 and i < len(messages) - 1:
                    time.sleep(delay_between_sends)

            except Exception as e:
                logger.error(f"Failed to send message {message['message_id']}: {e}")
                results.append(
                    {"message_id": message["message_id"], "error": str(e), "status": "error"}
                )

        return results

    def run_load_test(
        self, total_messages: int, target_rps: float, max_threads: int = 10, batch_size: int = 10
    ) -> dict[str, Any]:
        """
        Run load test with specified parameters

        Args:
            total_messages: Total number of messages to send
            target_rps: Target requests per second
            max_threads: Maximum concurrent threads
            batch_size: Messages per thread batch
        """
        test_id = f"load-test-{int(time.time())}"
        logger.info(f"Starting load test {test_id}: {total_messages} messages at {target_rps} RPS")

        # Calculate timing parameters
        messages_per_thread = max(1, batch_size)
        delay_between_sends = (1.0 / target_rps) if target_rps > 0 else 0.0
        threads_needed = min(
            max_threads, (total_messages + messages_per_thread - 1) // messages_per_thread
        )

        # Generate test messages
        logger.info("Generating test messages...")
        all_messages = [self.generate_test_message(test_id, i) for i in range(total_messages)]

        # Split messages into batches for threads
        message_batches = [
            all_messages[i : i + messages_per_thread]
            for i in range(0, len(all_messages), messages_per_thread)
        ]

        # Execute load test
        start_time = time.time()
        all_results = []

        logger.info(f"Sending {len(message_batches)} batches using {threads_needed} threads...")

        with ThreadPoolExecutor(max_workers=threads_needed) as executor:
            # Submit all batches
            future_to_batch = {
                executor.submit(self.send_message_batch, batch, delay_between_sends): i
                for i, batch in enumerate(message_batches)
            }

            # Collect results
            for future in as_completed(future_to_batch):
                batch_id = future_to_batch[future]
                try:
                    batch_results = future.result()
                    all_results.extend(batch_results)
                    logger.info(f"Completed batch {batch_id + 1}/{len(message_batches)}")
                except Exception as e:
                    logger.error(f"Batch {batch_id} failed: {e}")

        end_time = time.time()
        duration = end_time - start_time

        # Calculate statistics
        successful_sends = [r for r in all_results if r["status"] == "success"]
        failed_sends = [r for r in all_results if r["status"] == "error"]

        if successful_sends:
            send_times = [r["send_time"] for r in successful_sends]
            avg_send_time = statistics.mean(send_times)
            p95_send_time = statistics.quantiles(send_times, n=20)[18]  # 95th percentile
        else:
            avg_send_time = p95_send_time = 0

        actual_rps = len(successful_sends) / duration if duration > 0 else 0

        results = {
            "test_id": test_id,
            "start_time": datetime.fromtimestamp(start_time).isoformat(),
            "end_time": datetime.fromtimestamp(end_time).isoformat(),
            "duration_seconds": duration,
            "total_messages": total_messages,
            "successful_sends": len(successful_sends),
            "failed_sends": len(failed_sends),
            "target_rps": target_rps,
            "actual_rps": actual_rps,
            "avg_send_time_ms": avg_send_time * 1000,
            "p95_send_time_ms": p95_send_time * 1000,
            "threads_used": threads_needed,
            "batch_size": messages_per_thread,
        }

        logger.info(
            f"Load test completed: {results['successful_sends']}/{total_messages} messages sent "
            f"at {actual_rps:.2f} RPS (target: {target_rps} RPS)"
        )

        return results

    def get_queue_metrics(self, start_time: datetime, end_time: datetime) -> dict[str, Any]:
        """Get CloudWatch metrics for the queue during test period"""
        try:
            # Get queue attributes for approximate metrics
            queue_attrs = self.sqs.get_queue_attributes(
                QueueUrl=self.queue_url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )

            return {
                "messages_visible": int(
                    queue_attrs["Attributes"].get("ApproximateNumberOfMessages", 0)
                ),
                "messages_in_flight": int(
                    queue_attrs["Attributes"].get("ApproximateNumberOfMessagesNotVisible", 0)
                ),
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.error(f"Failed to get queue metrics: {e}")
            return {}


def main():
    parser = argparse.ArgumentParser(description="Amira Letter Scoring Load Test Driver")
    parser.add_argument("--queue-url", required=True, help="SQS Queue URL")
    parser.add_argument("--messages", type=int, default=1000, help="Total messages to send")
    parser.add_argument("--rps", type=float, default=50.0, help="Target requests per second")
    parser.add_argument("--threads", type=int, default=10, help="Maximum concurrent threads")
    parser.add_argument("--batch-size", type=int, default=10, help="Messages per thread batch")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("--output", help="JSON output file for results")

    args = parser.parse_args()

    # Create load driver
    driver = LoadDriver(args.queue_url, args.region)

    # Run load test
    results = driver.run_load_test(
        total_messages=args.messages,
        target_rps=args.rps,
        max_threads=args.threads,
        batch_size=args.batch_size,
    )

    # Get final queue state
    queue_metrics = driver.get_queue_metrics(
        datetime.fromisoformat(results["start_time"]), datetime.fromisoformat(results["end_time"])
    )
    results["final_queue_state"] = queue_metrics

    # Output results
    print(json.dumps(results, indent=2))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        logger.info(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()
