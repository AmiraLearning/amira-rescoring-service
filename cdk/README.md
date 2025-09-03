CDK deployment notes

Build outputs
- TypeScript compiles to `dist/`. The `lib/` folder contains only sources.
- Use `npm run build` before `cdk synth/deploy` or rely on CI to run it.

Stacks
- GPU: `AmiraLetterScoringStack` (internal ALB + ECS/Triton)
- Lambda: `AmiraLambdaParallelStack` (CPU-only by default)

Lambda modes
- CPU-only: set `EnableTriton=false` (default). No VPC/ALB requirements.
- Triton mode: set `EnableTriton=true` and either:
  - pass `TritonClusterUrl`, or
  - leave blank and ensure SSM `/amira/triton_alb_url` exists (published by the GPU stack).
  - provide `VpcId`, `PrivateSubnetIdsCsv`, and `LambdaSecurityGroupId` for internal ALB access.

Parameters summary
- `EnableTriton`: 'true' | 'false'
- `TritonClusterUrl`: Triton URL (leave blank to use SSM)
- `VpcId`, `PrivateSubnetIdsCsv`, `LambdaSecurityGroupId`: required when `EnableTriton=true`
- `MaxEventSourceConcurrency`: tune SQS consumer parallelism
