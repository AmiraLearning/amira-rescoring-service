#!/usr/bin/env python3
"""
Benchmark script to test Lambda cold start performance.
Tests different configurations to find blazing fast setup.
"""

import asyncio
import json
import time
from typing import Any

import aioboto3


async def test_lambda_cold_start(function_name: str, num_tests: int) -> dict[str, Any]:
    """Test Lambda cold start performance

    Args:
        function_name: Name of the Lambda function to test.
        num_tests: Number of tests to run.

    Returns:
        Dictionary containing cold start times.
    """
    session: aioboto3.Session = aioboto3.Session()
    async with session.client("lambda", region_name="us-east-1") as lambda_client:
        cold_start_times: list[float] = []
        warm_times: list[float] = []

        print(f"Testing {function_name} cold start performance...")

        for idx in range(num_tests):
            env_update: dict[str, str] = {"TEST_RUN": str(int(time.time()))}
            await lambda_client.update_function_configuration(
                FunctionName=function_name, Environment={"Variables": env_update}
            )

            await asyncio.sleep(2)

            test_payload: dict[str, Any] = {
                "Records": [
                    {
                        "body": json.dumps({"activityId": f"test-{idx}"}),
                        "messageId": f"msg-{idx}",
                    }
                ]
            }

            start_time: float = time.time()
            await lambda_client.invoke(FunctionName=function_name, Payload=json.dumps(test_payload))
            cold_start_time = time.time() - start_time
            cold_start_times.append(cold_start_time)

            print(f"  Cold start {idx + 1}: {cold_start_time:.3f}s")

            start_time: float = time.time()
            await lambda_client.invoke(FunctionName=function_name, Payload=json.dumps(test_payload))
            warm_time: float = time.time() - start_time
            warm_times.append(warm_time)

            await asyncio.sleep(1)

        results: dict[str, Any] = {
            "avg_cold_start": sum(cold_start_times) / len(cold_start_times),
            "min_cold_start": min(cold_start_times),
            "max_cold_start": max(cold_start_times),
            "avg_warm": sum(warm_times) / len(warm_times),
            "cold_starts": cold_start_times,
            "warm_times": warm_times,
        }

        print(f"\nResults for {function_name}:")
        print(f"   Average cold start: {results['avg_cold_start']:.3f}s")
        print(f"   Min cold start: {results['min_cold_start']:.3f}s")
        print(f"   Max cold start: {results['max_cold_start']:.3f}s")
        print(f"   Average warm: {results['avg_warm']:.3f}s")

        return results


async def benchmark_concurrent_cold_starts(
    function_name: str, concurrency: int = 100
) -> dict[str, Any]:
    """Test concurrent cold start performance

    Args:
        function_name: Name of the Lambda function to test.
        concurrency: Number of concurrent tests to run.

    Returns:
        Dictionary containing concurrent cold start times.
    """
    session: aioboto3.Session = aioboto3.Session()
    async with session.client("lambda", region_name="us-east-1") as lambda_client:
        print(f"Testing {concurrency} concurrent cold starts...")

        await lambda_client.update_function_configuration(
            FunctionName=function_name,
            Environment={"Variables": {"CONCURRENT_TEST": str(int(time.time()))}},
        )
        await asyncio.sleep(3)

        async def invoke_lambda(idx: int) -> float:
            test_payload: dict[str, Any] = {
                "Records": [
                    {
                        "body": json.dumps({"activityId": f"concurrent-test-{idx}"}),
                        "messageId": f"concurrent-msg-{idx}",
                    }
                ]
            }

            start_time: float = time.time()
            await lambda_client.invoke(FunctionName=function_name, Payload=json.dumps(test_payload))
            return time.time() - start_time

        start_time: float = time.time()
        tasks: list[float] = [invoke_lambda(idx) for idx in range(concurrency)]
        execution_times: list[float] = await asyncio.gather(*tasks)
        total_time: float = time.time() - start_time

        print("\nConcurrent Results:")
        print(f"   {concurrency} invocations in {total_time:.3f}s")
        print(f"   Average execution time: {sum(execution_times) / len(execution_times):.3f}s")
        print(f"   Throughput: {concurrency / total_time:.1f} invocations/second")

        return {
            "concurrency": concurrency,
            "total_time": total_time,
            "avg_execution_time": sum(execution_times) / len(execution_times),
            "throughput": concurrency / total_time,
            "execution_times": execution_times,
        }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python benchmark_cold_start.py <function-name> <num-tests> <concurrency>")
        print("Example: python benchmark_cold_start.py AmiraLambdaParallelStack-ProcessingFunction")
        sys.exit(1)

    function_name: str = sys.argv[1]

    print("Lambda Cold Start Benchmark")
    print("=" * 50)

    sequential_results: dict[str, Any] = asyncio.run(
        test_lambda_cold_start(function_name, num_tests=5)
    )

    print("\n" + "=" * 50)
    concurrent_results: dict[str, Any] = asyncio.run(
        benchmark_concurrent_cold_starts(function_name, concurrency=50)
    )

    print("\nSummary:")
    print(f"   Best cold start: {sequential_results['min_cold_start']:.3f}s")
    print(f"   Concurrent throughput: {concurrent_results['throughput']:.1f} invocations/second")
    print(
        f"   45,000 concurrent potential: {45000 / concurrent_results['avg_execution_time']:.0f} activities/minute"
    )
