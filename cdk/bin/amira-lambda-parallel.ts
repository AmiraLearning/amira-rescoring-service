#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AmiraLambdaParallelStack } from '../lib/amira-lambda-parallel-stack';

const app = new cdk.App();
new AmiraLambdaParallelStack(app, 'AmiraLambdaParallelStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});
