# Rollback & Change Review

## Change Review Checklist
- Risk assessment and blast radius
- Rollout plan and rollback plan included
- Alarms/dashboards reviewed
- Concurrency guards enabled (no overlapping deploys)
- Secrets and TLS cert validity confirmed

## Rollback Procedure
- CDK: redeploy last-known-good stack version (tagged commit)
- Lambda: revert image tag/environment parameters
- ECS: scale desired count to 0, then redeploy stable task definition
- Verify: health checks green, dashboards back to baseline

## Post-Mortem
- Capture timeline, contributing factors, and action items
- Update runbooks and alarms if gaps identified
