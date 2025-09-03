#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AmiraLambdaParallelStack } from '../lib/amira-lambda-parallel-stack';
import * as ssm from 'aws-cdk-lib/aws-ssm';

const app = new cdk.App();
const stack = new AmiraLambdaParallelStack(app, 'AmiraLambdaParallelStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});

// The stack handles parameter defaults and SSM lookups internally
