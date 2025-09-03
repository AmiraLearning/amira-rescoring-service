#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AmiraLetterScoringStack } from '../lib/amira-letter-scoring-stack';

const app = new cdk.App();
new AmiraLetterScoringStack(app, 'AmiraLetterScoringStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});
