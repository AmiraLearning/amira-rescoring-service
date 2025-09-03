import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as sources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sns_subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as cwactions from 'aws-cdk-lib/aws-cloudwatch-actions';
import { Construct } from 'constructs';

export class AmiraLambdaParallelStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Parameters for configuration
    const athenaDbParam = new cdk.CfnParameter(this, 'AthenaDatabase', {
      type: 'String',
      default: 'default',
      description: 'Athena database name'
    });
    const athenaOutputParam = new cdk.CfnParameter(this, 'AthenaOutput', {
      type: 'String',
      default: 's3://athena-query-results/',
      description: 'Athena query output S3 path'
    });
    const athenaQueryParam = new cdk.CfnParameter(this, 'AthenaQuery', {
      type: 'String',
      default: 'SELECT activity_id FROM activities WHERE process_flag = 1',
      description: 'Athena SQL to produce activity IDs'
    });
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
    const audioBucketNameParam = new cdk.CfnParameter(this, 'AudioBucketName', {
      type: 'String',
      default: '',
      description: 'Optional S3 bucket name for input audio (read-only). Leave blank to skip.'
    });
    const slackWebhookParam = new cdk.CfnParameter(this, 'SlackWebhookUrl', {
      type: 'String',
      default: '',
      description: 'Slack webhook URL for job completion and error notifications',
      noEcho: true
    });
    const tritonClusterUrlParam = new cdk.CfnParameter(this, 'TritonClusterUrl', {
      type: 'String',
      default: '',
      description: 'Optional Triton GPU cluster URL for remote inference. Leave blank for CPU-only mode.'
    });

    // KMS key for encryption
    const kmsKey = new kms.Key(this, 'AmiraParallelKey', {
      enableKeyRotation: true,
      alias: 'alias/amira-lambda-parallel'
    });

    // Results bucket
    const resultsBucket = new s3.Bucket(this, 'ResultsBucket', {
      versioned: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: kmsKey,
      bucketKeyEnabled: true,
      enforceSSL: true,
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
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN
    });

    // SQS Dead Letter Queue
    const dlq = new sqs.Queue(this, 'ProcessingDLQ', {
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: kmsKey,
      enforceSSL: true
    });

    // Main SQS queue for tasks
    const tasksQueue = new sqs.Queue(this, 'TasksQueue', {
      visibilityTimeout: cdk.Duration.minutes(15),
      deadLetterQueue: { queue: dlq, maxReceiveCount: 3 },
      encryption: sqs.QueueEncryption.KMS,
      encryptionMasterKey: kmsKey,
      enforceSSL: true,
      receiveMessageWaitTime: cdk.Duration.seconds(0),
    });

    // CloudWatch Log Group for Lambda
    const logGroup = new logs.LogGroup(this, 'ProcessingLogGroup', {
      logGroupName: '/aws/lambda/amira-parallel-processor',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryptionKey: kmsKey
    });

    // Conditions
    const audioProvided = new cdk.CfnCondition(this, 'AudioBucketProvided', {
      expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(audioBucketNameParam.valueAsString, ''))
    });
    const tritonUrlProvided = new cdk.CfnCondition(this, 'TritonUrlProvided', {
      expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(tritonClusterUrlParam.valueAsString, ''))
    });

    // Processing Lambda function as Docker image (pre-cached model)
    const processingLambda = new lambda.DockerImageFunction(this, 'ProcessingFunction', {
      functionName: 'amira-parallel-processor',
      code: lambda.DockerImageCode.fromImageAsset('../lambda/parallel_processor'),
      timeout: cdk.Duration.minutes(15),
      memorySize: 10240,
      reservedConcurrentExecutions: 200,
      deadLetterQueue: dlq,
      tracing: lambda.Tracing.ACTIVE,
      environment: {
        RESULTS_BUCKET: resultsBucket.bucketName,
        RESULTS_PREFIX: resultsPrefixParam.valueAsString,
        MODEL_PATH: '/opt/models/wav2vec2-optimized',
        AUDIO_BUCKET: audioBucketNameParam.valueAsString,
        KMS_KEY_ID: kmsKey.keyId,
        SLACK_WEBHOOK_URL: slackWebhookParam.valueAsString,
        MAX_CONCURRENCY: '10', // Tuned for initial alignment; adjust via env if needed
        BATCH_ALL_PHRASES: 'true',
        USE_FLOAT16: 'true',
        INCLUDE_CONFIDENCE: 'true',
        TEST_MODE: 'false',
        USE_TRITON: cdk.Token.asString(cdk.Fn.conditionIf(tritonUrlProvided.logicalId, 'true', 'false')),
        TRITON_URL: tritonClusterUrlParam.valueAsString,
        TRITON_MODEL: 'w2v2',
        PYTHONOPTIMIZE: '2',
        TORCH_NUM_THREADS: '6',
        OMP_NUM_THREADS: '6',
        TRANSFORMERS_CACHE: '/tmp/models',
        HF_HUB_CACHE: '/tmp/hf_cache'
      }
    });

    // SQS Event Source for Lambda
    const eventSource = new sources.SqsEventSource(tasksQueue, {
      batchSize: 1,
      maxConcurrency: 10,
      reportBatchItemFailures: true,
      maxBatchingWindow: cdk.Duration.seconds(0),
    });
    processingLambda.addEventSource(eventSource);

    // Optional warming rule
    const enableWarmingParam = new cdk.CfnParameter(this, 'EnableWarming', {
      type: 'String',
      default: 'false',
      allowedValues: ['true', 'false'],
      description: 'Enable periodic warm invocation to reduce cold starts'
    });
    const warmRateMinutesParam = new cdk.CfnParameter(this, 'WarmRateMinutes', {
      type: 'Number',
      default: 15,
      description: 'Warm ping rate in minutes when EnableWarming=true'
    });
    const warmEnabled = new cdk.CfnCondition(this, 'WarmEnabled', {
      expression: cdk.Fn.conditionEquals(enableWarmingParam.valueAsString, 'true')
    });
    const warmingRule = new events.CfnRule(this, 'ProcessingWarmRule', {
      scheduleExpression: cdk.Fn.sub('rate(${Minutes} minutes)', { Minutes: warmRateMinutesParam.valueAsString }),
      state: 'ENABLED',
      targets: [
        {
          id: 'Target0',
          arn: processingLambda.functionArn,
          input: JSON.stringify({ warm: true })
        }
      ]
    });
    warmingRule.cfnOptions.condition = warmEnabled;
    const warmPermission = new lambda.CfnPermission(this, 'AllowEventBridgeInvokeWarm', {
      action: 'lambda:InvokeFunction',
      functionName: processingLambda.functionName,
      principal: 'events.amazonaws.com',
      sourceArn: warmingRule.attrArn
    });
    warmPermission.cfnOptions.condition = warmEnabled;

    const audioPolicyDoc = new iam.PolicyDocument({
      statements: [
        new iam.PolicyStatement({
          actions: ['s3:ListBucket'],
          resources: [cdk.Fn.sub('arn:aws:s3:::${BucketName}', { BucketName: audioBucketNameParam.valueAsString })]
        }),
        new iam.PolicyStatement({
          actions: ['s3:GetObject'],
          resources: [cdk.Fn.sub('arn:aws:s3:::${BucketName}/*', { BucketName: audioBucketNameParam.valueAsString })]
        })
      ]
    });

    const audioCfnPolicy = new iam.CfnPolicy(this, 'ProcessingLambdaAudioPolicy', {
      policyDocument: audioPolicyDoc,
      roles: [processingLambda.role!.roleName],
      policyName: `ProcessingLambdaAudioPolicy-${cdk.Stack.of(this).stackName}`
    });
    audioCfnPolicy.cfnOptions.condition = audioProvided;

    // Results bucket and KMS permissions
    resultsBucket.grantWrite(processingLambda);
    kmsKey.grantEncryptDecrypt(processingLambda);

    // CloudWatch metrics permissions for job tracking
    processingLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cloudwatch:PutMetricData'
      ],
      resources: ['*']
    }));

    // Enqueue Lambda function
    const enqueueLambda = new lambda.Function(this, 'EnqueueFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('../lambda/enqueue_jobs'),
      timeout: cdk.Duration.minutes(5),
      tracing: lambda.Tracing.ACTIVE,
      environment: {
        JOBS_QUEUE_URL: tasksQueue.queueUrl,
        ATHENA_DATABASE: athenaDbParam.valueAsString,
        ATHENA_OUTPUT: athenaOutputParam.valueAsString,
        ATHENA_QUERY: athenaQueryParam.valueAsString
      }
    });

    // IAM permissions for enqueue Lambda
    const athenaWorkgroupArn = cdk.Arn.format({
      service: 'athena',
      resource: 'workgroup',
      resourceName: 'primary'
    }, this);
    const glueDbArn = cdk.Arn.format({
      service: 'glue',
      resource: 'database',
      resourceName: athenaDbParam.valueAsString
    }, this);
    const glueTableWildcardArn = cdk.Arn.format({
      service: 'glue',
      resource: 'table',
      resourceName: `${athenaDbParam.valueAsString}/*`
    }, this);

    enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['athena:StartQueryExecution', 'athena:GetQueryExecution', 'athena:GetQueryResults'],
      resources: [athenaWorkgroupArn]
    }));
    enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['glue:GetDatabase'],
      resources: [glueDbArn]
    }));
    enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['glue:GetTable'],
      resources: [glueTableWildcardArn]
    }));

    // Athena output bucket permissions
    const athenaOutputParsed = cdk.Fn.split('/', athenaOutputParam.valueAsString);
    const athenaOutputBucket = cdk.Fn.select(2, athenaOutputParsed);
    enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket'],
      resources: [cdk.Arn.format({ service: 's3', resource: athenaOutputBucket }, this)]
    }));
    enqueueLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:PutObject'],
      resources: [cdk.Arn.format({ service: 's3', resource: `${athenaOutputBucket}/*` }, this)]
    }));

    tasksQueue.grantSendMessages(enqueueLambda);

    // Schedule for automatic enqueueing
    const scheduleRule = new events.Rule(this, 'ScheduleRule', {
      description: 'Trigger parallel processing pipeline',
      schedule: events.Schedule.cron({ minute: '0', hour: '2' })
    });
    scheduleRule.addTarget(new targets.LambdaFunction(enqueueLambda));

    // Manual trigger Lambda
    const manualTriggerLambda = new lambda.Function(this, 'ManualTriggerFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('../lambda/manual_enqueue'),
      timeout: cdk.Duration.minutes(1),
      tracing: lambda.Tracing.ACTIVE,
      environment: { JOBS_QUEUE_URL: tasksQueue.queueUrl }
    });
    tasksQueue.grantSendMessages(manualTriggerLambda);

    // SNS topic for alerts
    const alertsTopic = new sns.Topic(this, 'AlertsTopic', {
      displayName: 'Amira Lambda Parallel Alerts'
    });

    // Slack notification Lambda
    const slackNotifierLambda = new lambda.Function(this, 'SlackNotifierFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.lambda_handler',
      code: lambda.Code.fromAsset('../lambda/slack_notifier'),
      timeout: cdk.Duration.seconds(30),
      tracing: lambda.Tracing.ACTIVE,
      environment: {
        SLACK_WEBHOOK_URL: slackWebhookParam.valueAsString
      }
    });

    // Subscribe Slack notifier to SNS alerts
    const slackWebhookProvided = new cdk.CfnCondition(this, 'SlackWebhookProvided', {
      expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(slackWebhookParam.valueAsString, ''))
    });

    // Lambda permission for SNS to invoke Slack notifier
    slackNotifierLambda.addPermission('AllowSNSInvoke', {
      principal: new iam.ServicePrincipal('sns.amazonaws.com'),
      sourceArn: alertsTopic.topicArn
    });

    // Conditional SNS subscription to Slack notifier
    const slackSubscription = new sns.CfnSubscription(this, 'SlackNotifierSubscription', {
      topicArn: alertsTopic.topicArn,
      protocol: 'lambda',
      endpoint: slackNotifierLambda.functionArn
    });
    slackSubscription.cfnOptions.condition = slackWebhookProvided;

    // CloudWatch Alarms
    const dlqDepthAlarm = new cw.Alarm(this, 'DLQDepthAlarm', {
      metric: dlq.metricApproximateNumberOfMessagesVisible(),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
    });

    const processingErrorsAlarm = new cw.Alarm(this, 'ProcessingErrorsAlarm', {
      metric: processingLambda.metricErrors(),
      threshold: 10,
      evaluationPeriods: 2,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD
    });

    const queueDepthAlarm = new cw.Alarm(this, 'QueueDepthAlarm', {
      metric: tasksQueue.metricApproximateNumberOfMessagesVisible(),
      threshold: 1000,
      evaluationPeriods: 3,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD
    });

    const queueAgeAlarm = new cw.Alarm(this, 'QueueAgeAlarm', {
      metric: tasksQueue.metricApproximateAgeOfOldestMessage(),
      threshold: 300,
      evaluationPeriods: 3,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD
    });

    // Job completion detection - queue empty AND no active Lambda executions
    const concurrentExecutionsMetric = new cw.Metric({
      namespace: 'AWS/Lambda',
      metricName: 'ConcurrentExecutions',
      dimensionsMap: { FunctionName: processingLambda.functionName },
      statistic: 'Average',
      period: cdk.Duration.minutes(2)
    });
    const jobCompletionExpr = new cw.MathExpression({
      expression: 'IF(queue < 1 AND concurrent < 1, 1, 0)',
      usingMetrics: {
        queue: tasksQueue.metricApproximateNumberOfMessagesVisible({
          statistic: 'Average',
          period: cdk.Duration.minutes(2)
        }),
        concurrent: concurrentExecutionsMetric
      }
    });
    const jobCompletionAlarm = new cw.Alarm(this, 'JobCompletionAlarm', {
      alarmName: 'JobCompletionDetected',
      alarmDescription: 'Triggered when all jobs are processed (queue empty and no active executions)',
      metric: jobCompletionExpr,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING
    });

    const alertAction = new cwactions.SnsAction(alertsTopic);

    dlqDepthAlarm.addAlarmAction(alertAction);
    processingErrorsAlarm.addAlarmAction(alertAction);
    queueDepthAlarm.addAlarmAction(alertAction);
    jobCompletionAlarm.addAlarmAction(alertAction);
    queueAgeAlarm.addAlarmAction(alertAction);

    // CloudWatch Dashboard
    const dashboard = new cw.Dashboard(this, 'ParallelProcessingDashboard', {
      dashboardName: 'AmiraLambdaParallel'
    });

    dashboard.addWidgets(
      new cw.GraphWidget({
        title: 'SQS Queue Metrics',
        left: [tasksQueue.metricApproximateNumberOfMessagesVisible()],
        right: [tasksQueue.metricApproximateAgeOfOldestMessage()],
        width: 12
      }),
      new cw.GraphWidget({
        title: 'Lambda Processing Metrics',
        left: [processingLambda.metricInvocations(), processingLambda.metricDuration()],
        right: [processingLambda.metricErrors(), processingLambda.metricThrottles()],
        width: 12
      }),
      new cw.GraphWidget({
        title: 'Lambda Concurrency',
        left: [concurrentExecutionsMetric],
        width: 24
      }),
      new cw.GraphWidget({
        title: 'DLQ Depth',
        left: [dlq.metricApproximateNumberOfMessagesVisible()],
        width: 12
      }),
      new cw.GraphWidget({
        title: 'Job Completion Tracking',
        left: [
          new cw.Metric({
            namespace: 'Amira/Jobs',
            metricName: 'JobsCompleted',
            statistic: 'Sum'
          }),
          new cw.Metric({
            namespace: 'Amira/Jobs',
            metricName: 'JobsFailed',
            statistic: 'Sum'
          })
        ],
        width: 12
      }),
      new cw.GraphWidget({
        title: 'ProcessingTime (ms)',
        left: [
          new cw.Metric({ namespace: 'Amira/Jobs', metricName: 'ProcessingTime', statistic: 'Average' })
        ],
        right: [
          new cw.Metric({ namespace: 'Amira/Jobs', metricName: 'ProcessingTime', statistic: 'p95' })
        ],
        width: 12
      }),
      new cw.GraphWidget({
        title: 'Inference Total (p95 ms)',
        left: [
          new cw.Metric({ namespace: 'Amira/Inference', metricName: 'InferenceTotalMs', statistic: 'p95' })
        ],
        width: 12
      }),
      new cw.GraphWidget({
        title: 'Activity Total (p95 ms)',
        left: [
          new cw.Metric({ namespace: 'Amira/Activity', metricName: 'ActivityTotalMs', statistic: 'p95' })
        ],
        width: 12
      })
    );

    // Outputs
    new cdk.CfnOutput(this, 'TasksQueueUrl', {
      value: tasksQueue.queueUrl,
      description: 'SQS Tasks Queue URL'
    });

    new cdk.CfnOutput(this, 'ProcessingLambdaArn', {
      value: processingLambda.functionArn,
      description: 'Processing Lambda Function ARN'
    });

    new cdk.CfnOutput(this, 'ResultsBucketName', {
      value: resultsBucket.bucketName,
      description: 'S3 Results Bucket Name'
    });

    new cdk.CfnOutput(this, 'ManualTriggerFunctionName', {
      value: manualTriggerLambda.functionName,
      description: 'Manual trigger Lambda function name (use with AWS CLI)'
    });

    new cdk.CfnOutput(this, 'DashboardUrl', {
      value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=${dashboard.dashboardName}`,
      description: 'CloudWatch Dashboard URL'
    });
  }
}
