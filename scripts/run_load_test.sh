#!/bin/bash
# Load test runner script for Amira Letter Scoring Platform

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${SCRIPT_DIR}/../results/load-tests"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

# Default values
QUEUE_URL=""
MESSAGES=1000
RPS=50
THREADS=10
BATCH_SIZE=10
REGION="us-east-1"
STAGE="dev"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --queue-url)
            QUEUE_URL="$2"
            shift 2
            ;;
        --messages)
            MESSAGES="$2"
            shift 2
            ;;
        --rps)
            RPS="$2"
            shift 2
            ;;
        --threads)
            THREADS="$2"
            shift 2
            ;;
        --batch-size)
            BATCH_SIZE="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --stage)
            STAGE="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --queue-url URL     SQS Queue URL (required)"
            echo "  --messages N        Total messages to send (default: 1000)"
            echo "  --rps N             Target requests per second (default: 50)"
            echo "  --threads N         Maximum concurrent threads (default: 10)"
            echo "  --batch-size N      Messages per thread batch (default: 10)"
            echo "  --region REGION     AWS region (default: us-east-1)"
            echo "  --stage STAGE       Deployment stage (default: dev)"
            echo "  --help              Show this help"
            echo ""
            echo "Examples:"
            echo "  $0 --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/amira-tasks --messages 5000 --rps 100"
            echo "  $0 --queue-url https://sqs.us-east-1.amazonaws.com/123456789012/amira-tasks --stage prod --threads 20"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate required parameters
if [[ -z "$QUEUE_URL" ]]; then
    echo "Error: --queue-url is required"
    echo "Run with --help for usage information"
    exit 1
fi

# Create results directory
mkdir -p "$RESULTS_DIR"

# Result files
RESULTS_FILE="${RESULTS_DIR}/load-test-${TIMESTAMP}.json"
LOG_FILE="${RESULTS_DIR}/load-test-${TIMESTAMP}.log"

echo "Starting load test..."
echo "Queue URL: $QUEUE_URL"
echo "Messages: $MESSAGES"
echo "Target RPS: $RPS"
echo "Stage: $STAGE"
echo "Results will be saved to: $RESULTS_FILE"
echo ""

# Ensure Python dependencies are available
if ! python3 -c "import boto3, statistics" 2>/dev/null; then
    echo "Installing required Python dependencies..."
    pip3 install boto3 --quiet
fi

# Pre-test queue state
echo "Getting pre-test queue state..."
PRE_TEST_DEPTH=$(aws sqs get-queue-attributes \
    --queue-url "$QUEUE_URL" \
    --attribute-names ApproximateNumberOfMessages \
    --region "$REGION" \
    --query 'Attributes.ApproximateNumberOfMessages' \
    --output text 2>/dev/null || echo "0")

echo "Pre-test queue depth: $PRE_TEST_DEPTH messages"

# Run the load test
echo "Running load test..."
START_TIME=$(date +%s)

python3 "$SCRIPT_DIR/load_driver.py" \
    --queue-url "$QUEUE_URL" \
    --messages "$MESSAGES" \
    --rps "$RPS" \
    --threads "$THREADS" \
    --batch-size "$BATCH_SIZE" \
    --region "$REGION" \
    --output "$RESULTS_FILE" \
    2>&1 | tee "$LOG_FILE"

END_TIME=$(date +%s)
TOTAL_DURATION=$((END_TIME - START_TIME))

# Post-test queue state
echo ""
echo "Getting post-test queue state..."
POST_TEST_DEPTH=$(aws sqs get-queue-attributes \
    --queue-url "$QUEUE_URL" \
    --attribute-names ApproximateNumberOfMessages \
    --region "$REGION" \
    --query 'Attributes.ApproximateNumberOfMessages' \
    --output text 2>/dev/null || echo "unknown")

# Get Lambda metrics if available
echo "Collecting Lambda metrics..."
FUNCTION_NAME="amira-parallel-processor"

# Get Lambda invocations in the last hour
LAMBDA_INVOCATIONS=$(aws cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name Invocations \
    --dimensions Name=FunctionName,Value="$FUNCTION_NAME" \
    --start-time "$(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%S)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
    --period 3600 \
    --statistics Sum \
    --region "$REGION" \
    --query 'Datapoints[0].Sum' \
    --output text 2>/dev/null || echo "unavailable")

LAMBDA_ERRORS=$(aws cloudwatch get-metric-statistics \
    --namespace AWS/Lambda \
    --metric-name Errors \
    --dimensions Name=FunctionName,Value="$FUNCTION_NAME" \
    --start-time "$(date -d '1 hour ago' -u +%Y-%m-%dT%H:%M:%S)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
    --period 3600 \
    --statistics Sum \
    --region "$REGION" \
    --query 'Datapoints[0].Sum' \
    --output text 2>/dev/null || echo "unavailable")

# Summary
echo ""
echo "========================================="
echo "LOAD TEST SUMMARY"
echo "========================================="
echo "Test completed at: $(date)"
echo "Total duration: ${TOTAL_DURATION}s"
echo "Stage: $STAGE"
echo "Region: $REGION"
echo ""
echo "Test Parameters:"
echo "  Messages sent: $MESSAGES"
echo "  Target RPS: $RPS"
echo "  Threads used: $THREADS"
echo "  Batch size: $BATCH_SIZE"
echo ""
echo "Queue State:"
echo "  Pre-test depth: $PRE_TEST_DEPTH messages"
echo "  Post-test depth: $POST_TEST_DEPTH messages"
echo ""
echo "Lambda Metrics (last hour):"
echo "  Invocations: $LAMBDA_INVOCATIONS"
echo "  Errors: $LAMBDA_ERRORS"
echo ""
echo "Results saved to: $RESULTS_FILE"
echo "Logs saved to: $LOG_FILE"
echo ""

# Generate quick ROI snippet if results file exists
if [[ -f "$RESULTS_FILE" ]]; then
    echo "Generating ROI snippet..."
    SUCCESSFUL_SENDS=$(cat "$RESULTS_FILE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('successful_sends', 'N/A'))")
    ACTUAL_RPS=$(cat "$RESULTS_FILE" | python3 -c "import sys, json; print(f\"{json.load(sys.stdin).get('actual_rps', 0):.2f}\")")
    AVG_SEND_TIME=$(cat "$RESULTS_FILE" | python3 -c "import sys, json; print(f\"{json.load(sys.stdin).get('avg_send_time_ms', 0):.2f}\")")

    echo "Quick ROI Metrics:"
    echo "  Successful sends: $SUCCESSFUL_SENDS/$MESSAGES"
    echo "  Actual RPS achieved: $ACTUAL_RPS (target: $RPS)"
    echo "  Average send time: ${AVG_SEND_TIME}ms"
    echo ""
fi

echo "To run another test with different parameters:"
echo "$0 --queue-url \"$QUEUE_URL\" --messages 5000 --rps 100 --stage prod"
echo ""
echo "To analyze results:"
echo "cat $RESULTS_FILE | jq '.'"
