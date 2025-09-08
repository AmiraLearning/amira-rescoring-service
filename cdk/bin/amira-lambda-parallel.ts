#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AmiraLambdaParallelStack } from '../lib/amira-lambda-parallel-stack';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';

const app = new cdk.App();

const stack = new AmiraLambdaParallelStack(app, 'AmiraLambdaParallelStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

// Enable CDK-nag with AWS Solutions Checks
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));

// Curated suppressions for Lambda parallel processing stack
NagSuppressions.addStackSuppressions(stack, [
  {
    id: 'AwsSolutions-SQS3',
    reason: 'DLQ configured with appropriate retention; messages sent to DLQ after maxReceiveCount attempts'
  },
  {
    id: 'AwsSolutions-IAM4',
    reason: 'AWS managed policies required for Lambda execution, Athena queries, and S3 operations'
  },
  {
    id: 'AwsSolutions-IAM5',
    reason: 'Wildcard permissions needed for dynamic S3 paths, Athena result locations, and CloudWatch metrics'
  },
  {
    id: 'AwsSolutions-L1',
    reason: 'Lambda functions use Docker images with controlled runtime versions and security updates'
  },
  {
    id: 'AwsSolutions-COD4',
    reason: 'CodeDeploy canary deployment configured via LambdaDeploymentConstruct for safe rollouts'
  }
]);

// The stack handles parameter defaults and SSM lookups internally
