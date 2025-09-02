#!/usr/bin/env python3
"""
Benchmark script to test Lambda cold start performance.
Tests different configurations to find blazing fast setup.
"""

import json
import time
import aioboto3
import asyncio
from typing import Dict, List, Any

async def test_lambda_cold_start(function_name: str, num_tests: int = 10) -> Dict[str, Any]:
    """Test Lambda cold start performance"""
    session = aioboto3.Session()
    async with session.client('lambda', region_name='us-east-1') as lambda_client:
        cold_start_times = []
        warm_times = []
        
        print(f"🧪 Testing {function_name} cold start performance...")
        
        # Force cold start by updating environment
        for i in range(num_tests):
            # Update env to force cold start
            env_update = {'TEST_RUN': str(int(time.time()))}
            await lambda_client.update_function_configuration(
                FunctionName=function_name,
                Environment={'Variables': env_update}
            )
            
            # Wait for update to complete
            await asyncio.sleep(2)
            
            # Test cold start
            test_payload = {
                'Records': [{
                    'body': json.dumps({'activityId': f'test-{i}'}),
                    'messageId': f'msg-{i}'
                }]
            }
            
            start_time = time.time()
            response = await lambda_client.invoke(
                FunctionName=function_name,
                Payload=json.dumps(test_payload)
            )
            cold_start_time = time.time() - start_time
            cold_start_times.append(cold_start_time)
            
            print(f"  Cold start {i+1}: {cold_start_time:.3f}s")
            
            # Test warm execution
            start_time = time.time()
            await lambda_client.invoke(
                FunctionName=function_name,
                Payload=json.dumps(test_payload)
            )
            warm_time = time.time() - start_time
            warm_times.append(warm_time)
            
            await asyncio.sleep(1)  # Brief pause between tests
        
        results = {
            'avg_cold_start': sum(cold_start_times) / len(cold_start_times),
            'min_cold_start': min(cold_start_times),
            'max_cold_start': max(cold_start_times),
            'avg_warm': sum(warm_times) / len(warm_times),
            'cold_starts': cold_start_times,
            'warm_times': warm_times
        }
        
        print(f"\n📊 Results for {function_name}:")
        print(f"   Average cold start: {results['avg_cold_start']:.3f}s")
        print(f"   Min cold start: {results['min_cold_start']:.3f}s")
        print(f"   Max cold start: {results['max_cold_start']:.3f}s")
        print(f"   Average warm: {results['avg_warm']:.3f}s")
        
        return results

async def benchmark_concurrent_cold_starts(function_name: str, concurrency: int = 100):
    """Test concurrent cold start performance"""
    session = aioboto3.Session()
    async with session.client('lambda', region_name='us-east-1') as lambda_client:
        print(f"🚀 Testing {concurrency} concurrent cold starts...")
        
        # Force cold start
        await lambda_client.update_function_configuration(
            FunctionName=function_name,
            Environment={'Variables': {'CONCURRENT_TEST': str(int(time.time()))}}
        )
        await asyncio.sleep(3)
        
        # Create concurrent invocations
        async def invoke_lambda(i: int):
            test_payload = {
                'Records': [{
                    'body': json.dumps({'activityId': f'concurrent-test-{i}'}),
                    'messageId': f'concurrent-msg-{i}'
                }]
            }
            
            start_time = time.time()
            response = await lambda_client.invoke(
                FunctionName=function_name,
                Payload=json.dumps(test_payload)
            )
            return time.time() - start_time
        
        # Run concurrent tests
        start_time = time.time()
        tasks = [invoke_lambda(i) for i in range(concurrency)]
        execution_times = await asyncio.gather(*tasks)
        total_time = time.time() - start_time
        
        print(f"\n⚡ Concurrent Results:")
        print(f"   {concurrency} invocations in {total_time:.3f}s")
        print(f"   Average execution time: {sum(execution_times) / len(execution_times):.3f}s")
        print(f"   Throughput: {concurrency / total_time:.1f} invocations/second")
        
        return {
            'concurrency': concurrency,
            'total_time': total_time,
            'avg_execution_time': sum(execution_times) / len(execution_times),
            'throughput': concurrency / total_time,
            'execution_times': execution_times
        }

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python benchmark_cold_start.py <function-name>")
        print("Example: python benchmark_cold_start.py AmiraLambdaParallelStack-ProcessingFunction")
        sys.exit(1)
    
    function_name = sys.argv[1]
    
    print("🧪 Lambda Cold Start Benchmark")
    print("=" * 50)
    
    # Test sequential cold starts
    sequential_results = asyncio.run(test_lambda_cold_start(function_name, num_tests=5))
    
    # Test concurrent cold starts
    print("\n" + "=" * 50)
    concurrent_results = asyncio.run(
        benchmark_concurrent_cold_starts(function_name, concurrency=50)
    )
    
    print("\n📈 Summary:")
    print(f"   Best cold start: {sequential_results['min_cold_start']:.3f}s")
    print(f"   Concurrent throughput: {concurrent_results['throughput']:.1f} invocations/second")
    print(f"   45,000 concurrent potential: {45000 / concurrent_results['avg_execution_time']:.0f} activities/minute")