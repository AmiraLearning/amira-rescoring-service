import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as sources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';
import { LambdaDeploymentConstruct } from './lambda-deployment';

export interface ParallelProcessingLambdaConstructProps {
  readonly resultsBucket: s3.IBucket;
  readonly kmsKey: kms.IKey;
  readonly tasksQueue: sqs.IQueue;
  readonly dlq: sqs.IQueue;
  readonly vpc?: {
    vpc: ec2.IVpc;
    subnets: ec2.ISubnet[];
    securityGroup?: ec2.ISecurityGroup;
  };
  readonly features: {
    enableTriton: boolean;
    enableWarming: boolean;
    tritonUrl?: string;
    athenaCleanup?: {
      enabled: boolean;
      bucket: string;
      prefix: string;
      ageDays: number;
    };
  };
  readonly config: {
    modelPath: string;
    resultsPrefix: string;
    audioBucket?: {
      name: string;
      prefix?: string;
    };
    slackWebhookUrl?: string;
    maxConcurrency: number;
    maxEventSourceConcurrency: number;
    maxBatchingWindowSeconds: number;
    athena?: {
      database: string;
      output: string;
      query: string;
    };
    warmRateMinutes?: number;
    lambda?: {
      memorySize?: number;
      timeout?: number;
    };
  };
}

export class ParallelProcessingLambdaConstruct extends Construct {
  public readonly processingLambda: lambda.DockerImageFunction;
  public readonly processingLambdaDeployment: LambdaDeploymentConstruct;
  public readonly enqueueLambda: lambda.Function;
  public readonly manualTriggerLambda: lambda.Function;
  public readonly slackNotifierLambda?: lambda.Function;
  public readonly cleanupLambda?: lambda.Function;

  constructor(scope: Construct, id: string, props: ParallelProcessingLambdaConstructProps) {
    super(scope, id);

    // Create log group
    const logGroup = new logs.LogGroup(this, 'ProcessingLogGroup', {
      logGroupName: '/aws/lambda/amira-parallel-processor',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryptionKey: props.kmsKey
    });

    // Create Slack notifier if webhook URL provided
    if (props.config.slackWebhookUrl) {
      this.slackNotifierLambda = this.createSlackNotifier(props.config.slackWebhookUrl);
    }

    // Create processing Lambda
    this.processingLambda = this.createProcessingLambda(props, logGroup);

    // Setup canary deployment for processing Lambda
    this.processingLambdaDeployment = new LambdaDeploymentConstruct(this, 'ProcessingDeployment', {
      lambdaFunction: this.processingLambda,
      enableCanaryDeployment: true,
      canaryConfig: {
        deploymentConfig: cdk.aws_codedeploy.LambdaDeploymentConfig.CANARY_10PERCENT_5MINUTES
      }
    });

    // Create enqueue Lambda
    this.enqueueLambda = this.createEnqueueLambda(props);

    // Create manual trigger Lambda
    this.manualTriggerLambda = this.createManualTriggerLambda(props);

    // Setup scheduled trigger
    this.setupScheduledTrigger();

    // Setup optional warming
    if (props.features.enableWarming) {
      this.setupWarming(props.config.warmRateMinutes ?? 15);
    }

    // Setup optional Athena cleanup
    if (props.features.athenaCleanup?.enabled) {
      this.cleanupLambda = this.setupAthenaCleanup(props.features.athenaCleanup);
    }

    // Configure permissions
    this.configurePermissions(props);
  }

  private createProcessingLambda(
    props: ParallelProcessingLambdaConstructProps,
    _logGroup: logs.ILogGroup
  ): lambda.DockerImageFunction {
    const processingLambda = new lambda.DockerImageFunction(this, 'ProcessingFunction', {
      functionName: 'amira-parallel-processor',
      code: lambda.DockerImageCode.fromImageAsset('../lambda/parallel_processor'),
      timeout: cdk.Duration.minutes(props.config.lambda?.timeout || 15),
      memorySize: this.calculateOptimalMemorySize(props),
      deadLetterQueue: props.dlq,
      tracing: lambda.Tracing.ACTIVE,
      environment: this.buildProcessingEnvironment(props)
    });

    // Configure VPC if provided
    if (props.vpc) {
      let securityGroup = props.vpc.securityGroup;

      // Create default security group if none provided
      if (!securityGroup) {
        securityGroup = new ec2.SecurityGroup(this, 'LambdaSecurityGroup', {
          vpc: props.vpc.vpc,
          description: 'Security group for Lambda function in VPC',
          allowAllOutbound: true // Lambda needs outbound access for AWS services
        });
      }

      const cfnFunc = processingLambda.node.defaultChild as lambda.CfnFunction;
      cfnFunc.vpcConfig = {
        securityGroupIds: [securityGroup.securityGroupId],
        subnetIds: props.vpc.subnets.map(s => s.subnetId)
      };
    }

    // Add SQS event source
    const eventSource = new sources.SqsEventSource(props.tasksQueue, {
      batchSize: 1,
      maxConcurrency: props.config.maxEventSourceConcurrency,
      reportBatchItemFailures: true,
      maxBatchingWindow: cdk.Duration.seconds(props.config.maxBatchingWindowSeconds),
    });
    processingLambda.addEventSource(eventSource);

    return processingLambda;
  }

  private buildProcessingEnvironment(props: ParallelProcessingLambdaConstructProps): Record<string, string> {
    const env: Record<string, string> = {
      RESULTS_BUCKET: props.resultsBucket.bucketName,
      RESULTS_PREFIX: props.config.resultsPrefix,
      MODEL_PATH: '/opt/models/wav2vec2-optimized',
      KMS_KEY_ID: props.kmsKey.keyId,
      MAX_CONCURRENCY: props.config.maxConcurrency.toString(),
      BATCH_ALL_PHRASES: 'true',
      USE_FLOAT16: 'true',
      INCLUDE_CONFIDENCE: 'true',
      TEST_MODE: 'false',
      USE_TRITON: props.features.enableTriton.toString(),
      TRITON_MODEL: 'w2v2',
      PYTHONOPTIMIZE: '2',
      TORCH_NUM_THREADS: '6',
      OMP_NUM_THREADS: '6',
      TRANSFORMERS_CACHE: '/tmp/models',
      HF_HUB_CACHE: '/tmp/hf_cache'
    };

    if (props.config.audioBucket) {
      env.AUDIO_BUCKET = props.config.audioBucket.name;
    }

    if (props.config.slackWebhookUrl) {
      env.SLACK_WEBHOOK_URL = props.config.slackWebhookUrl;
    }

    if (props.features.tritonUrl) {
      env.TRITON_URL = props.features.tritonUrl;
    }

    return env;
  }

  private createSlackNotifier(webhookUrl: string): lambda.Function {
    return new lambda.Function(this, 'SlackNotifierFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('../lambda/slack_notifier'),
      timeout: cdk.Duration.seconds(30),
      tracing: lambda.Tracing.ACTIVE,
      environment: {
        SLACK_WEBHOOK_URL: webhookUrl,
        AUDIO_ENV: 'unknown' // Will be set at runtime
      }
    });
  }

  private createEnqueueLambda(props: ParallelProcessingLambdaConstructProps): lambda.Function {
    if (!props.config.athena) {
      throw new Error('Athena configuration is required for enqueue Lambda');
    }

    return new lambda.Function(this, 'EnqueueFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('../lambda/enqueue_jobs'),
      timeout: cdk.Duration.minutes(5),
      tracing: lambda.Tracing.ACTIVE,
      environment: {
        JOBS_QUEUE_URL: props.tasksQueue.queueUrl,
        ATHENA_DATABASE: props.config.athena.database,
        ATHENA_OUTPUT: props.config.athena.output,
        ATHENA_QUERY: props.config.athena.query,
        SLACK_NOTIFIER_FUNCTION_NAME: this.slackNotifierLambda?.functionName || '',
        AUDIO_ENV: 'unknown' // Will be set at runtime
      }
    });
  }

  private createManualTriggerLambda(props: ParallelProcessingLambdaConstructProps): lambda.Function {
    return new lambda.Function(this, 'ManualTriggerFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('../lambda/manual_enqueue'),
      timeout: cdk.Duration.minutes(1),
      tracing: lambda.Tracing.ACTIVE,
      environment: { JOBS_QUEUE_URL: props.tasksQueue.queueUrl }
    });
  }

  private setupScheduledTrigger(): void {
    const scheduleRule = new events.Rule(this, 'ScheduleRule', {
      description: 'Trigger parallel processing pipeline',
      schedule: events.Schedule.cron({ minute: '0', hour: '2' })
    });
    scheduleRule.addTarget(new targets.LambdaFunction(this.enqueueLambda));
  }

  private setupWarming(rateMinutes: number): void {
    const warmingRule = new events.Rule(this, 'WarmingRule', {
      description: 'Keep processing Lambda warm',
      schedule: events.Schedule.rate(cdk.Duration.minutes(rateMinutes))
    });
    warmingRule.addTarget(new targets.LambdaFunction(this.processingLambda, {
      event: events.RuleTargetInput.fromObject({ warm: true })
    }));
  }

  private setupAthenaCleanup(config: { bucket: string; prefix: string; ageDays: number }): lambda.Function {
    const cleanupLambda = new lambda.Function(this, 'AthenaCleanupFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'athena_staging_cleanup.main',
      code: lambda.Code.fromAsset('../scripts'),
      timeout: cdk.Duration.minutes(5)
    });

    // Schedule cleanup
    const cleanupRule = new events.Rule(this, 'AthenaCleanupSchedule', {
      description: 'Scheduled Athena staging cleanup',
      schedule: events.Schedule.cron({ minute: '0', hour: '3' })
    });
    cleanupRule.addTarget(new targets.LambdaFunction(cleanupLambda, {
      event: events.RuleTargetInput.fromObject({
        bucket: config.bucket,
        prefix: config.prefix,
        age_days: config.ageDays,
      })
    }));

    // Grant permissions
    cleanupLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket', 's3:DeleteObject', 's3:DeleteObjectVersion'],
      resources: [
        `arn:aws:s3:::${config.bucket}`,
        `arn:aws:s3:::${config.bucket}/*`
      ]
    }));

    return cleanupLambda;
  }

  private configurePermissions(props: ParallelProcessingLambdaConstructProps): void {
    // Results bucket and KMS permissions
    props.resultsBucket.grantWrite(this.processingLambda);
    props.kmsKey.grantEncryptDecrypt(this.processingLambda);

    // CloudWatch metrics permissions
    this.processingLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*']
    }));

    // Secrets Manager permissions for AppSync
    this.processingLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'],
      resources: [
        cdk.Arn.format({
          service: 'secretsmanager',
          resource: 'secret',
          resourceName: 'amira/appsync/*'
        }, cdk.Stack.of(this))
      ]
    }));

    // Audio bucket permissions (conditional)
    if (props.config.audioBucket) {
      this.processingLambda.addToRolePolicy(new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [`arn:aws:s3:::${props.config.audioBucket.name}`]
      }));
      this.processingLambda.addToRolePolicy(new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [`arn:aws:s3:::${props.config.audioBucket.name}/*`]
      }));
    }

    // Queue permissions for enqueue and manual trigger
    props.tasksQueue.grantSendMessages(this.enqueueLambda);
    props.tasksQueue.grantSendMessages(this.manualTriggerLambda);

    // Athena permissions for enqueue Lambda
    if (props.config.athena) {
      this.configureAthenaPermissions(props.config.athena);
    }

    // Slack notifier permissions
    if (this.slackNotifierLambda) {
      this.enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
        actions: ['lambda:InvokeFunction'],
        resources: [this.slackNotifierLambda.functionArn]
      }));
    }
  }

  private configureAthenaPermissions(athena: { database: string; output: string }): void {
    const athenaWorkgroupArn = cdk.Arn.format({
      service: 'athena',
      resource: 'workgroup',
      resourceName: 'primary'
    }, cdk.Stack.of(this));

    const glueDbArn = cdk.Arn.format({
      service: 'glue',
      resource: 'database',
      resourceName: athena.database
    }, cdk.Stack.of(this));

    const glueTableWildcardArn = cdk.Arn.format({
      service: 'glue',
      resource: 'table',
      resourceName: `${athena.database}/*`
    }, cdk.Stack.of(this));

    this.enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults'],
      resources: [athenaWorkgroupArn]
    }));

    this.enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['glue:GetDatabase'],
      resources: [glueDbArn]
    }));

    this.enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['glue:GetTable'],
      resources: [glueTableWildcardArn]
    }));

    // Athena output bucket permissions
    const athenaOutputParts = athena.output.split('/');
    const athenaOutputBucket = athenaOutputParts[2];

    this.enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket'],
      resources: [`arn:aws:s3:::${athenaOutputBucket}`]
    }));

    this.enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:PutObject'],
      resources: [`arn:aws:s3:::${athenaOutputBucket}/*`]
    }));
  }

  private calculateOptimalMemorySize(props: ParallelProcessingLambdaConstructProps): number {
    // Use explicit config if provided
    if (props.config.lambda?.memorySize) {
      return props.config.lambda.memorySize;
    }

    // Calculate based on workload characteristics
    const baseMemory = 1024; // 1GB minimum
    const tritonMultiplier = props.features.enableTriton ? 1.5 : 1; // Triton needs more memory
    const concurrencyPenalty = Math.min(props.config.maxConcurrency, 8) * 512; // Scale with concurrency

    let recommendedMemory = Math.round((baseMemory * tritonMultiplier) + concurrencyPenalty);

    // Ensure memory is within Lambda limits and multiples of 64MB above 1GB
    recommendedMemory = Math.max(1024, Math.min(10240, recommendedMemory));
    recommendedMemory = Math.ceil(recommendedMemory / 64) * 64;

    // Add metadata for cost tracking
    this.node.addMetadata('memory-calculation', {
      baseMemory,
      tritonMultiplier,
      concurrencyPenalty,
      finalMemory: recommendedMemory,
      reasoning: 'Dynamic calculation based on Triton usage and concurrency requirements'
    });

    return recommendedMemory;
  }
}
