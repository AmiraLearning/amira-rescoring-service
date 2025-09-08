import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { StageConfig } from './config/stages';
export interface AmiraLambdaParallelStackProps extends cdk.StackProps {
    readonly stage?: string;
    readonly stageConfig?: StageConfig;
}
export declare class AmiraLambdaParallelStack extends cdk.Stack {
    constructor(scope: Construct, id: string, props?: AmiraLambdaParallelStackProps);
}
