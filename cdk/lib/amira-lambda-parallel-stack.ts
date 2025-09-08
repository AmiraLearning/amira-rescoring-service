import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { Annotations } from 'aws-cdk-lib';

// Import our new constructs
import { StorageConstruct } from './constructs/storage';
import { QueueingConstruct } from './constructs/queueing';
import { ParallelProcessingLambdaConstruct } from './constructs/parallel-processing-lambda';
import { ObservabilityConstruct } from './constructs/observability';
import { CrossStackLinkingConstruct } from './constructs/cross-stack-linking';
import { SecurityStandardsAspect } from './aspects/security-standards';
import { CdkNagIntegrationAspect } from './aspects/cdk-nag-integration';
import { StageConfigs, StageConfig } from './config/stages';

export interface AmiraLambdaParallelStackProps extends cdk.StackProps {
  readonly stage?: string;
  readonly stageConfig?: StageConfig;
}

export class AmiraLambdaParallelStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: AmiraLambdaParallelStackProps = {}) {
    super(scope, id, props);

    // Get stage configuration
    const stage = props.stage || process.env.STAGE || 'dev';
    const stageConfig = props.stageConfig || StageConfigs.getConfig(stage);

    // Parameters for deployment-time configuration (much reduced)
    const modelPathParam = new cdk.CfnParameter(this, 'ModelPath', {
      type: 'String',
      default: 'facebook/wav2vec2-base-960h',
      description: 'HF model path for Wav2Vec2'
    });

    const resultsPrefixParam = new cdk.CfnParameter(this, 'ResultsPrefix', {
      type: 'String',
      default: 'results/',
      description: 'S3 key prefix for results writes'
    });

    // Audio bucket configuration moved to stage config to fix token branching

    const slackWebhookParam = new cdk.CfnParameter(this, 'SlackWebhookUrl', {
      type: 'String',
      default: '',
      description: 'Slack webhook URL for job completion and error notifications',
      noEcho: true
    });

    // Triton cluster URL auto-resolved from SSM parameter /amira/triton_alb_url (removed parameter to fix token branching)

    // Storage (KMS key, buckets with security policies)
    const storage = new StorageConstruct(this, 'Storage', {
      kmsKeyAlias: `alias/amira-lambda-parallel-${stage}`,
      enableVersioning: false, // Cost optimization for Lambda stack
      lifecycleRules: [
        {
          id: 'IntelligentTieringNow',
          enabled: true,
          transitions: [
            { storageClass: s3.StorageClass.INTELLIGENT_TIERING, transitionAfter: cdk.Duration.days(0) }
          ]
        },
        {
          id: 'TransitionToIA30d',
          enabled: true,
          transitions: [
            { storageClass: s3.StorageClass.INFREQUENT_ACCESS, transitionAfter: cdk.Duration.days(30) }
          ]
        },
        {
          id: 'TransitionToGlacier120d',
          enabled: true,
          transitions: [
            { storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL, transitionAfter: cdk.Duration.days(120) }
          ]
        }
      ]
    });

    // Queueing (SQS with DLQ, 2-hour visibility timeout for processing)
    const queueing = new QueueingConstruct(this, 'Queueing', {
      kmsKey: storage.kmsKey,
      visibilityTimeout: cdk.Duration.hours(2),
      receiveMessageWaitTimeSeconds: stageConfig.queueing.maxBatchingWindowSeconds
    });

    // Get VPC configuration from cross-stack linking if Triton is enabled
    let vpcConfig: { vpc: ec2.IVpc; subnets: ec2.ISubnet[]; securityGroup?: ec2.ISecurityGroup } | undefined;

    if (stageConfig.features.enableTriton) {
      try {
        const crossStackConfig = CrossStackLinkingConstruct.getVpcConfigFromSsm(this);
        const vpc = ec2.Vpc.fromLookup(this, 'ImportedVpc', { vpcId: crossStackConfig.vpcId });
        const subnets = crossStackConfig.privateSubnetIds.map((id, index) =>
          ec2.Subnet.fromSubnetId(this, `ImportedSubnet${index}`, id)
        );
        const securityGroup = ec2.SecurityGroup.fromSecurityGroupId(
          this,
          'ImportedSecurityGroup',
          crossStackConfig.albSecurityGroupId
        );

        vpcConfig = { vpc, subnets, securityGroup };
      } catch (error) {
        const errorMessage = `Failed to resolve VPC configuration from SSM: ${error}`;

        // For production stages, VPC attachment failure should be treated as an error
        if (stage === 'prod' || stage === 'production') {
          throw new Error(`CRITICAL: ${errorMessage}. Production Lambda functions must be VPC-attached.`);
        }

        // For non-production, log detailed warning
        Annotations.of(this).addWarning(`${errorMessage}. Lambda will run without VPC attachment in ${stage} environment.`);

        // Add metadata for debugging
        this.node.addMetadata('vpc-resolution-error', {
          error: String(error),
          stage,
          timestamp: new Date().toISOString()
        });
      }
    }

    // Resolve Triton URL from SSM (parameters removed to fix token branching)
    let tritonUrl: string | undefined;
    if (stageConfig.features.enableTriton) {
      tritonUrl = CrossStackLinkingConstruct.getTritonUrlFromSsm(this);
    }

    // Parallel Processing Lambda construct
    const parallelProcessing = new ParallelProcessingLambdaConstruct(this, 'ParallelProcessing', {
      resultsBucket: storage.resultsBucket,
      kmsKey: storage.kmsKey,
      tasksQueue: queueing.queue,
      dlq: queueing.dlq,
      vpc: vpcConfig,
      features: {
        enableTriton: stageConfig.features.enableTriton,
        enableWarming: stageConfig.features.enableWarming,
        tritonUrl,
        athenaCleanup: stageConfig.features.enableAthenaCleanup && stageConfig.athena?.cleanup ? {
          enabled: true,
          ...stageConfig.athena.cleanup
        } : undefined
      },
      config: {
        modelPath: modelPathParam.valueAsString,
        resultsPrefix: resultsPrefixParam.valueAsString,
        audioBucket: stageConfig.storage?.audioBucket,
        slackWebhookUrl: slackWebhookParam.valueAsString || undefined,
        maxConcurrency: stageConfig.compute.lambda.maxConcurrency,
        maxEventSourceConcurrency: stageConfig.compute.lambda.maxEventSourceConcurrency,
        maxBatchingWindowSeconds: stageConfig.queueing.maxBatchingWindowSeconds,
        athena: stageConfig.athena ? {
          database: stageConfig.athena.database,
          output: stageConfig.athena.output,
          query: stageConfig.athena.query
        } : undefined,
        warmRateMinutes: stageConfig.observability.warmRateMinutes
      }
    });

    // Observability (alarms, dashboards)
    const observability = new ObservabilityConstruct(this, 'Observability', {
      alarmEmail: stageConfig.observability.alarmEmail,
      dashboardName: `AmiraLambdaParallel-${stage}`,
      metricSources: {
        processingLambda: parallelProcessing.processingLambda,
        enqueueLambda: parallelProcessing.enqueueLambda,
        manualTriggerLambda: parallelProcessing.manualTriggerLambda,
        slackNotifierLambda: parallelProcessing.slackNotifierLambda,
        queue: queueing.queue,
        dlq: queueing.dlq
      }
    });

    // Cross-stack linking validation (if Triton is enabled but URL not provided via stage config)
    if (stageConfig.features.enableTriton && !tritonUrl) {
      new CrossStackLinkingConstruct(this, 'CrossStackLinking', {
        enableTritonUrlValidation: true
      });
    }

    // Apply security standards and compliance aspects
    cdk.Aspects.of(this).add(new SecurityStandardsAspect());
    cdk.Aspects.of(this).add(new CdkNagIntegrationAspect());

    // Outputs
    new cdk.CfnOutput(this, 'TasksQueueUrl', {
      value: queueing.queue.queueUrl,
      description: 'SQS Tasks Queue URL'
    });

    new cdk.CfnOutput(this, 'ProcessingLambdaArn', {
      value: parallelProcessing.processingLambda.functionArn,
      description: 'Processing Lambda Function ARN'
    });

    new cdk.CfnOutput(this, 'ResultsBucketName', {
      value: storage.resultsBucket.bucketName,
      description: 'S3 Results Bucket Name'
    });

    new cdk.CfnOutput(this, 'ManualTriggerFunctionName', {
      value: parallelProcessing.manualTriggerLambda.functionName,
      description: 'Manual trigger Lambda function name (use with AWS CLI)'
    });

    new cdk.CfnOutput(this, 'DashboardUrl', {
      value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=${observability.dashboard.dashboardName}`,
      description: 'CloudWatch Dashboard URL'
    });
  }
}
