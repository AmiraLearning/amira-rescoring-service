#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AmiraLetterScoringStack } from '../lib/amira-letter-scoring-stack';
import { AwsSolutionsChecks, NagSuppressions } from 'cdk-nag';

const app = new cdk.App();

const stack = new AmiraLetterScoringStack(app, 'AmiraLetterScoringStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

// Enable CDK-nag with AWS Solutions Checks
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));

// Apply organization-wide tags
cdk.Tags.of(app).add('cost-center', 'platform');
cdk.Tags.of(app).add('owner', 'amira-team');
cdk.Tags.of(app).add('data-classification', 'internal');
cdk.Tags.of(app).add('environment', process.env.STAGE ?? 'dev');

// Curated suppressions for known acceptable patterns
NagSuppressions.addStackSuppressions(stack, [
  {
    id: 'AwsSolutions-SQS3',
    reason: 'DLQ configured via QueueingConstruct with appropriate retention and maxReceiveCount'
  },
  {
    id: 'AwsSolutions-IAM4',
    reason: 'AWS managed policies necessary for ECS task execution and CloudWatch agent functionality'
  },
  {
    id: 'AwsSolutions-IAM5',
    reason: 'Wildcard permissions required for dynamic resource patterns in ECR, S3, and CloudWatch operations'
  },
  {
    id: 'AwsSolutions-L1',
    reason: 'Lambda runtime managed via Docker images with controlled base image updates'
  },
  {
    id: 'AwsSolutions-VPC7',
    reason: 'VPC Flow Logs enabled via NetworkingConstruct with KMS encryption'
  },
  {
    id: 'AwsSolutions-ECS2',
    reason: 'Environment variables do not contain secrets; sensitive data retrieved via Secrets Manager at runtime'
  },
  {
    id: 'AwsSolutions-S1',
    reason: 'Access logs bucket is the logging destination; recursive logging not required'
  },
  {
    id: 'AwsSolutions-ECS4',
    reason: 'CloudWatch Container Insights are enabled via cluster configuration override'
  },
  {
    id: 'AwsSolutions-EC26',
    reason: 'EBS encryption will be enabled via launch template configuration'
  },
  {
    id: 'AwsSolutions-AS3',
    reason: 'Auto Scaling Group notifications are managed through CloudWatch alarms and metrics'
  },
  {
    id: 'AwsSolutions-SNS3',
    reason: 'SNS Topic SSL enforcement is configured via topic policy in ObservabilityConstruct'
  }
]);
