# Deployment

## Stacks
- amira-letter-scoring-stack: ECS/Triton GPU cluster, ALB 443 → sidecar 8443, dashboards & alarms
- amira-lambda-parallel-stack: Image-based Lambda, SQS pipelines, results bucket, dashboards

## Critical Parameters
- TritonCertificateArn: ACM cert for ALB 443
- TritonTargetCertSecretArn: Optional sidecar TLS cert/key secret
- UseTriton / TritonClusterUrl: Enable remote inference from Lambda
 - EnableTargetHttp2 / TargetSslCiphers: TLS tuning options for sidecar

## Steps
1. Ensure ECR images are published (app, triton, cw-agent, dcgm)
2. Deploy ECS stack with TLS params
3. Deploy Lambda parallel stack with queue/bucket params
4. Verify dashboards and alarms are active
 5. (Optional) Provide `TritonTargetCertSecretArn` with {cert,key} to use managed certs; tasks will hot-load on restart

## Rollback
- Use CloudFormation stack rollback
- For ECS issues: scale desired count to 0, fix image/params, redeploy
- For Lambda issues: revert image tag or environment variables via stack update
