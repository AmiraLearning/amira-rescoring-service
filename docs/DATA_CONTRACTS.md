# Data Contracts

## Activity Results (v1)
- Schema: `schemas/activity-results-v1.json`
- Validated in CI via `tests/test_contract_activity_results_schema.py`
- Fields:
  - activity_id (string)
  - status (string)
  - pipeline_results (object)
  - processing_time (number)
  - timestamp (number)
  - processor (string)
  - schema_version (const: activity-results-v1)
  - correlation_id (string|null)

## Evolution Policy
- Backward compatible changes only in patch/minor versions
- Additive fields permitted; removals/renames require new schema version
- CI gates: contract tests must pass for all supported versions
