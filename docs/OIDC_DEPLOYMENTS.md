# OIDC Deployments Checklist

## Prereqs
- GitHub Actions OIDC provider configured in AWS IAM
- IAM role with trust policy for repo:ref
- CDK bootstrap done in target accounts/regions

## Workflow
- Use `aws-actions/configure-aws-credentials@v4` with `role-to-assume`
- Set `concurrency` group in workflow to avoid overlapping deploys
- Use `cdk diff` then `cdk deploy --require-approval never`

## Example Snippet
```yaml
concurrency:
  group: cdk-deploy-${{ github.ref }}
  cancel-in-progress: false

jobs:
  deploy:
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::<account>:role/GitHubCdkDeployRole
          aws-region: us-east-2
      - run: npm ci --prefix cdk
      - run: npx cdk diff --app "npx ts-node cdk/bin/amira-letter-scoring.ts"
      - run: npx cdk deploy --app "npx ts-node cdk/bin/amira-letter-scoring.ts" --require-approval never
```
