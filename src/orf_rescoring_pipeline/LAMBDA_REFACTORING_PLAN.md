# ORF Rescoring Pipeline Lambda Refactoring Plan

## Current Architecture Issues

The current ORF rescoring pipeline (`pipeline.py`) is designed for batch processing and contains several anti-patterns that are incompatible with Lambda-based serverless architecture:

### 1. Batch Operations
- `bulk_load_activity_story_data()` - loads multiple activities at once
- `bulk_load_model_features()` - loads model features for multiple activities
- Batch error tracking and reporting
- Batch file uploads and notifications

### 2. Concurrency Management
- `asyncio.Semaphore` for controlling concurrent processing
- Complex async task coordination with `asyncio.gather()`
- Progress bars and batch completion tracking

### 3. Orchestration Logic
- Pipeline start/completion notifications
- Batch statistics collection and reporting
- Complex error aggregation across activities
- File system operations (logging, debug output, CSV generation)

### 4. State Management
- Global accuracy statistics accumulator
- Batch success/failure tracking
- Cross-activity debugging and reporting

## Target Lambda Architecture

Each Lambda invocation should:
1. **Process exactly one activity** independently
2. **Initialize only required resources** (no pre-warming pools)
3. **Handle its own errors** without cross-activity coordination
4. **Return simple success/failure status**
5. **Clean up resources** before termination

## Refactoring Tasks

### Phase 1: Create Lambda Entry Point ✅
- [x] Create `lambda_handler.py` with async entry point
- [x] Single activity processing logic
- [x] Proper error handling and resource cleanup
- [x] Standard Lambda event/response format

### Phase 2: Simplify Core Processor
The `process_single_activity()` function is already well-designed but needs minor updates:

- [ ] Remove dependency on `model_features_cache` (already handles None case)
- [ ] Simplify error handling (no batch context needed)
- [ ] Remove file system operations for Lambda environment
- [ ] Streamline debug output for individual activities

### Phase 3: Eliminate Batch Operations
Files to refactor/remove:

#### `pipeline.py` - NEEDS MAJOR REFACTORING
- [ ] Remove batch loading functions
- [ ] Remove async coordination logic
- [ ] Remove progress tracking and notifications
- [ ] Remove batch file upload logic
- [ ] Keep only as legacy/local testing script

#### `core/processor.py` - MINOR UPDATES NEEDED
- [ ] Make `bulk_load_*` functions optional/deprecated
- [ ] Ensure all functions work with single activity scope
- [ ] Remove batch caching optimizations

### Phase 4: Update Utilities
Most utilities are already activity-scoped, but some need updates:

#### `utils/file_operations.py` - OK AS-IS
- Individual activity file operations are fine
- Upload functions work per-activity

#### `utils/debug.py` - MINOR UPDATES
- [ ] Remove batch accuracy accumulator or make optional
- [ ] Individual activity debugging is fine

#### `utils/slack.py` - REFACTOR FOR LAMBDA
- [ ] Remove batch pipeline notifications
- [ ] Add individual activity completion/error notifications if needed

### Phase 5: Lambda Infrastructure
- [ ] Create Lambda deployment configuration
- [ ] Set up SQS/EventBridge triggers for activity processing
- [ ] Configure proper IAM roles and permissions
- [ ] Set up monitoring and error handling

## Migration Strategy

### Step 1: Parallel Development
- Keep existing `pipeline.py` for current batch processing
- Develop Lambda handler alongside existing code
- Test Lambda handler with same activities

### Step 2: Gradual Migration
- Start with subset of activities through Lambda
- Compare results between batch and Lambda processing
- Gradually increase Lambda traffic

### Step 3: Complete Migration
- Decommission batch pipeline
- Move all activity processing to Lambda
- Update downstream systems to trigger individual Lambdas

## Benefits of Lambda Architecture

1. **Scalability**: Automatic scaling per activity
2. **Fault Isolation**: One activity failure doesn't affect others
3. **Resource Efficiency**: Pay only for actual processing time
4. **Simpler Debugging**: Individual activity logs and traces
5. **Better Monitoring**: Per-activity metrics and alerting
6. **Faster Recovery**: Retry failed activities individually

## Proposed File Structure

```
src/orf_rescoring_pipeline/
├── lambda_handler.py          # Main Lambda entry point ✅
├── core/
│   ├── processor.py           # Single activity processor (minor updates needed)
│   └── single_activity_loader.py  # Individual activity data loading
├── utils/                     # Mostly unchanged
├── models/                    # Unchanged
└── legacy/
    └── batch_pipeline.py      # Renamed from pipeline.py
```

## Implementation Priority

1. **High Priority**: Lambda handler and core processing
2. **Medium Priority**: Remove batch operations and simplify utilities
3. **Low Priority**: Legacy pipeline cleanup and infrastructure updates

This refactoring will transform the ORF rescoring pipeline from a monolithic batch processor into a scalable, serverless, event-driven architecture.
