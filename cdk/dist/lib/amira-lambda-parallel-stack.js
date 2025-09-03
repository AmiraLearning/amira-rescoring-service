"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.AmiraLambdaParallelStack = void 0;
const cdk = require("aws-cdk-lib");
const lambda = require("aws-cdk-lib/aws-lambda");
const sqs = require("aws-cdk-lib/aws-sqs");
const s3 = require("aws-cdk-lib/aws-s3");
const iam = require("aws-cdk-lib/aws-iam");
const events = require("aws-cdk-lib/aws-events");
const targets = require("aws-cdk-lib/aws-events-targets");
const sources = require("aws-cdk-lib/aws-lambda-event-sources");
const kms = require("aws-cdk-lib/aws-kms");
const logs = require("aws-cdk-lib/aws-logs");
const ssm = require("aws-cdk-lib/aws-ssm");
const cw = require("aws-cdk-lib/aws-cloudwatch");
const sns = require("aws-cdk-lib/aws-sns");
const cwactions = require("aws-cdk-lib/aws-cloudwatch-actions");
const cr = require("aws-cdk-lib/custom-resources");
class AmiraLambdaParallelStack extends cdk.Stack {
    constructor(scope, id, props) {
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
            description: 'Optional Triton GPU cluster URL for remote inference. Leave blank to auto-resolve from SSM parameter /amira/triton_alb_url.'
        });
        const enableTritonParam = new cdk.CfnParameter(this, 'EnableTriton', {
            type: 'String',
            default: 'false',
            allowedValues: ['true', 'false'],
            description: 'Enable calling a Triton cluster (internal ALB) from the processing Lambda'
        });
        // Optional VPC attachment for calling internal ALB directly
        const vpcIdParam = new cdk.CfnParameter(this, 'VpcId', {
            type: 'AWS::EC2::VPC::Id',
            default: cdk.Aws.NO_VALUE,
            description: 'VPC ID to attach the processing Lambda to (for internal ALB access)'
        });
        const privateSubnetIdsCsvParam = new cdk.CfnParameter(this, 'PrivateSubnetIdsCsv', {
            type: 'List<AWS::EC2::Subnet::Id>',
            description: 'Private subnet IDs for the Lambda VPC config'
        });
        const lambdaSecurityGroupIdParam = new cdk.CfnParameter(this, 'LambdaSecurityGroupId', {
            type: 'String',
            default: cdk.Aws.NO_VALUE,
            description: 'Optional existing Security Group ID for the processing Lambda'
        });
        const vpcProvided = new cdk.CfnCondition(this, 'VpcProvided', {
            expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(vpcIdParam.valueAsString, ''))
        });
        const lambdaSgProvided = new cdk.CfnCondition(this, 'LambdaSgProvided', {
            expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(lambdaSecurityGroupIdParam.valueAsString, ''))
        });
        // KMS key for encryption
        const kmsKey = new kms.Key(this, 'AmiraParallelKey', {
            enableKeyRotation: true,
            alias: 'alias/amira-lambda-parallel'
        });
        // Results bucket
        const accessLogsBucket = new s3.Bucket(this, 'LambdaAccessLogsBucket', {
            versioned: false,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            encryption: s3.BucketEncryption.S3_MANAGED,
            enforceSSL: true,
            removalPolicy: cdk.RemovalPolicy.RETAIN
        });
        const resultsBucket = new s3.Bucket(this, 'ResultsBucket', {
            versioned: false,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            encryption: s3.BucketEncryption.KMS,
            encryptionKey: kmsKey,
            bucketKeyEnabled: true,
            serverAccessLogsBucket: accessLogsBucket,
            serverAccessLogsPrefix: 's3-access-logs/',
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
        resultsBucket.addToResourcePolicy(new iam.PolicyStatement({
            sid: 'DenyInsecureTransport',
            effect: iam.Effect.DENY,
            principals: [new iam.AnyPrincipal()],
            actions: ['s3:*'],
            resources: [resultsBucket.bucketArn, `${resultsBucket.bucketArn}/*`],
            conditions: { Bool: { 'aws:SecureTransport': 'false' } }
        }));
        resultsBucket.addToResourcePolicy(new iam.PolicyStatement({
            sid: 'DenyUnEncryptedObjectUploads',
            effect: iam.Effect.DENY,
            principals: [new iam.AnyPrincipal()],
            actions: ['s3:PutObject'],
            resources: [`${resultsBucket.bucketArn}/*`],
            conditions: { StringNotEquals: { 's3:x-amz-server-side-encryption': 'aws:kms' } }
        }));
        // SQS Dead Letter Queue
        const dlq = new sqs.Queue(this, 'ProcessingDLQ', {
            retentionPeriod: cdk.Duration.days(14),
            encryption: sqs.QueueEncryption.KMS,
            encryptionMasterKey: kmsKey,
            enforceSSL: true
        });
        // Main SQS queue for tasks
        const tasksQueue = new sqs.Queue(this, 'TasksQueue', {
            visibilityTimeout: cdk.Duration.hours(2),
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
        const useTritonCond = new cdk.CfnCondition(this, 'UseTritonCond', {
            expression: cdk.Fn.conditionEquals(enableTritonParam.valueAsString, 'true')
        });
        // Custom resource to fail fast if SSM /amira/triton_alb_url is missing when Triton URL param is blank
        const ssmParamName = '/amira/triton_alb_url';
        const ssmParamArn = cdk.Arn.format({ service: 'ssm', resource: 'parameter', resourceName: 'amira/triton_alb_url' }, this);
        const tritonUrlSsmCheck = new cr.AwsCustomResource(this, 'TritonUrlSsmCheck', {
            onCreate: {
                service: 'SSM',
                action: 'getParameter',
                parameters: { Name: ssmParamName },
                physicalResourceId: cr.PhysicalResourceId.of('TritonUrlSsmCheck')
            },
            onUpdate: {
                service: 'SSM',
                action: 'getParameter',
                parameters: { Name: ssmParamName },
                physicalResourceId: cr.PhysicalResourceId.of('TritonUrlSsmCheck')
            },
            policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: [ssmParamArn] })
        });
        const ssmCheckCond = new cdk.CfnCondition(this, 'SsmCheckWhenUsingTritonAndNoUrl', {
            expression: cdk.Fn.conditionAnd(cdk.Fn.conditionEquals(enableTritonParam.valueAsString, 'true'), cdk.Fn.conditionEquals(tritonClusterUrlParam.valueAsString, ''))
        });
        const customResourceCfn = tritonUrlSsmCheck.node.defaultChild;
        if (customResourceCfn && customResourceCfn.cfnOptions) {
            customResourceCfn.cfnOptions.condition = ssmCheckCond;
        }
        // Processing Lambda function as Docker image (pre-cached model)
        const processingLambda = new lambda.DockerImageFunction(this, 'ProcessingFunction', {
            functionName: 'amira-parallel-processor',
            code: lambda.DockerImageCode.fromImageAsset('../lambda/parallel_processor'),
            timeout: cdk.Duration.minutes(15),
            memorySize: 10240,
            // Removed reserved concurrency to avoid unintended throttling during tests
            deadLetterQueue: dlq,
            tracing: lambda.Tracing.ACTIVE,
            environment: {
                RESULTS_BUCKET: resultsBucket.bucketName,
                RESULTS_PREFIX: resultsPrefixParam.valueAsString,
                MODEL_PATH: '/opt/models/wav2vec2-optimized',
                AUDIO_BUCKET: audioBucketNameParam.valueAsString,
                KMS_KEY_ID: kmsKey.keyId,
                SLACK_WEBHOOK_URL: slackWebhookParam.valueAsString,
                MAX_CONCURRENCY: '10',
                BATCH_ALL_PHRASES: 'true',
                USE_FLOAT16: 'true',
                INCLUDE_CONFIDENCE: 'true',
                TEST_MODE: 'false',
                USE_TRITON: enableTritonParam.valueAsString,
                TRITON_URL: cdk.Token.asString(cdk.Fn.conditionIf(useTritonCond.logicalId, cdk.Fn.conditionIf(tritonUrlProvided.logicalId, tritonClusterUrlParam.valueAsString, ssm.StringParameter.valueForStringParameter(this, '/amira/triton_alb_url')), '')),
                TRITON_MODEL: 'w2v2',
                PYTHONOPTIMIZE: '2',
                TORCH_NUM_THREADS: '6',
                OMP_NUM_THREADS: '6',
                TRANSFORMERS_CACHE: '/tmp/models',
                HF_HUB_CACHE: '/tmp/hf_cache'
            }
        });
        // VPC configuration is handled directly in CFN since CDK validation conflicts with conditional parameters
        const subnetsList = privateSubnetIdsCsvParam.valueAsList;
        const cfnFunc = processingLambda.node.defaultChild;
        cfnFunc.vpcConfig = cdk.Token.asAny(cdk.Fn.conditionIf(vpcProvided.logicalId, {
            SecurityGroupIds: cdk.Token.asList(cdk.Fn.conditionIf(lambdaSgProvided.logicalId, [lambdaSecurityGroupIdParam.valueAsString], cdk.Aws.NO_VALUE)),
            SubnetIds: subnetsList
        }, cdk.Aws.NO_VALUE));
        // Enforce: if VpcId is provided, LambdaSecurityGroupId must be provided
        new cdk.CfnRule(this, 'VpcRequiresLambdaSg', {
            ruleCondition: cdk.Fn.conditionAnd(cdk.Fn.conditionNot(cdk.Fn.conditionEquals(vpcIdParam.valueAsString, '')), cdk.Fn.conditionEquals(enableTritonParam.valueAsString, 'true')),
            assertions: [
                {
                    assert: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(lambdaSecurityGroupIdParam.valueAsString, '')),
                    assertDescription: 'When VpcId is provided, LambdaSecurityGroupId must also be provided.'
                }
            ]
        });
        // SQS Event Source for Lambda
        const maxEventSourceConcurrencyParam = new cdk.CfnParameter(this, 'MaxEventSourceConcurrency', {
            type: 'Number',
            default: 10,
            description: 'SQS event source max concurrency for the processing Lambda'
        });
        const eventSource = new sources.SqsEventSource(tasksQueue, {
            batchSize: 1,
            maxConcurrency: maxEventSourceConcurrencyParam.valueAsNumber,
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
            roles: [processingLambda.role.roleName],
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
        // Optional Athena staging cleanup Lambda + schedule
        const enableAthenaCleanupParam = new cdk.CfnParameter(this, 'EnableAthenaCleanup', {
            type: 'String',
            default: 'false',
            allowedValues: ['true', 'false'],
            description: 'Enable scheduled Athena staging cleanup (optional)'
        });
        const athenaCleanupBucketParam = new cdk.CfnParameter(this, 'AthenaCleanupBucket', {
            type: 'String',
            default: '',
            description: 'S3 bucket for Athena staging results'
        });
        const athenaCleanupPrefixParam = new cdk.CfnParameter(this, 'AthenaCleanupPrefix', {
            type: 'String',
            default: 'athena_staging',
            description: 'S3 prefix for Athena staging results'
        });
        const athenaCleanupAgeDaysParam = new cdk.CfnParameter(this, 'AthenaCleanupAgeDays', {
            type: 'Number',
            default: 7,
            description: 'Delete staging objects older than N days'
        });
        const cleanupEnabled = new cdk.CfnCondition(this, 'AthenaCleanupEnabled', {
            expression: cdk.Fn.conditionEquals(enableAthenaCleanupParam.valueAsString, 'true')
        });
        const cleanupLambda = new lambda.Function(this, 'AthenaStagingCleanup', {
            runtime: lambda.Runtime.PYTHON_3_12,
            handler: 'athena_staging_cleanup.main',
            code: lambda.Code.fromAsset('../scripts'),
            timeout: cdk.Duration.minutes(5)
        });
        cleanupLambda.node.defaultChild.cfnOptions.condition = cleanupEnabled;
        cleanupLambda.addToRolePolicy(new iam.PolicyStatement({
            actions: ['s3:ListBucket', 's3:DeleteObject', 's3:DeleteObjectVersion'],
            resources: [
                cdk.Arn.format({ service: 's3', resource: athenaCleanupBucketParam.valueAsString }, this),
                cdk.Arn.format({ service: 's3', resource: `${athenaCleanupBucketParam.valueAsString}/*` }, this)
            ]
        }));
        const cleanupRule = new events.Rule(this, 'AthenaCleanupSchedule', {
            description: 'Scheduled Athena staging cleanup',
            schedule: events.Schedule.cron({ minute: '0', hour: '3' })
        });
        cleanupRule.node.defaultChild.cfnOptions.condition = cleanupEnabled;
        cleanupRule.addTarget(new targets.LambdaFunction(cleanupLambda, {
            event: events.RuleTargetInput.fromObject({
                bucket: athenaCleanupBucketParam.valueAsString,
                prefix: athenaCleanupPrefixParam.valueAsString,
                age_days: athenaCleanupAgeDaysParam.valueAsNumber,
            })
        }));
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
        dashboard.addWidgets(new cw.GraphWidget({
            title: 'SQS Queue Metrics',
            left: [tasksQueue.metricApproximateNumberOfMessagesVisible()],
            right: [tasksQueue.metricApproximateAgeOfOldestMessage()],
            width: 12
        }), new cw.GraphWidget({
            title: 'Lambda Processing Metrics',
            left: [processingLambda.metricInvocations(), processingLambda.metricDuration()],
            right: [processingLambda.metricErrors(), processingLambda.metricThrottles()],
            width: 12
        }), new cw.GraphWidget({
            title: 'Lambda Concurrency',
            left: [concurrentExecutionsMetric],
            width: 24
        }), new cw.GraphWidget({
            title: 'DLQ Depth',
            left: [dlq.metricApproximateNumberOfMessagesVisible()],
            width: 12
        }), new cw.GraphWidget({
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
        }), new cw.GraphWidget({
            title: 'ProcessingTime (ms)',
            left: [
                new cw.Metric({ namespace: 'Amira/Jobs', metricName: 'ProcessingTime', statistic: 'Average' })
            ],
            right: [
                new cw.Metric({ namespace: 'Amira/Jobs', metricName: 'ProcessingTime', statistic: 'p95' })
            ],
            width: 12
        }), new cw.GraphWidget({
            title: 'Inference Total (p95 ms)',
            left: [
                new cw.Metric({ namespace: 'Amira/Inference', metricName: 'InferenceTotalMs', statistic: 'p95' })
            ],
            width: 12
        }), new cw.GraphWidget({
            title: 'Activity Total (p95 ms)',
            left: [
                new cw.Metric({ namespace: 'Amira/Activity', metricName: 'ActivityTotalMs', statistic: 'p95' })
            ],
            width: 12
        }));
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
exports.AmiraLambdaParallelStack = AmiraLambdaParallelStack;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiYW1pcmEtbGFtYmRhLXBhcmFsbGVsLXN0YWNrLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsiLi4vLi4vbGliL2FtaXJhLWxhbWJkYS1wYXJhbGxlbC1zdGFjay50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOzs7QUFBQSxtQ0FBbUM7QUFDbkMsaURBQWlEO0FBRWpELDJDQUEyQztBQUMzQyx5Q0FBeUM7QUFDekMsMkNBQTJDO0FBQzNDLGlEQUFpRDtBQUNqRCwwREFBMEQ7QUFDMUQsZ0VBQWdFO0FBQ2hFLDJDQUEyQztBQUMzQyw2Q0FBNkM7QUFDN0MsMkNBQTJDO0FBQzNDLGlEQUFpRDtBQUNqRCwyQ0FBMkM7QUFFM0MsZ0VBQWdFO0FBQ2hFLG1EQUFtRDtBQUduRCxNQUFhLHdCQUF5QixTQUFRLEdBQUcsQ0FBQyxLQUFLO0lBQ3JELFlBQVksS0FBZ0IsRUFBRSxFQUFVLEVBQUUsS0FBc0I7UUFDOUQsS0FBSyxDQUFDLEtBQUssRUFBRSxFQUFFLEVBQUUsS0FBSyxDQUFDLENBQUM7UUFFeEIsK0JBQStCO1FBQy9CLE1BQU0sYUFBYSxHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsZ0JBQWdCLEVBQUU7WUFDakUsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsU0FBUztZQUNsQixXQUFXLEVBQUUsc0JBQXNCO1NBQ3BDLENBQUMsQ0FBQztRQUNILE1BQU0saUJBQWlCLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxjQUFjLEVBQUU7WUFDbkUsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsNEJBQTRCO1lBQ3JDLFdBQVcsRUFBRSw2QkFBNkI7U0FDM0MsQ0FBQyxDQUFDO1FBQ0gsTUFBTSxnQkFBZ0IsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLGFBQWEsRUFBRTtZQUNqRSxJQUFJLEVBQUUsUUFBUTtZQUNkLE9BQU8sRUFBRSwyREFBMkQ7WUFDcEUsV0FB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QyxDQUFDO1FBRUgsSUFBSSxHQUFHLENBQUMsU0FBUyxDQUFDLElBQUksRUFBRSxxQkFBcUIsRUFBRTtZQUM3QyxLQUFLLEVBQUUsZ0JBQWdCLENBQUMsV0FBVztZQUNuQyxXQUFXLEVBQUUsZ0NBQWdDO1NBQzlDLENBQUMsQ0FBQztRQUVILElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsbUJBQW1CLEVBQUU7WUFDM0MsS0FBSyxFQUFFLGFBQWEsQ0FBQyxVQUFVO1lBQy9CLFdBQVcsRUFBRSx3QkFBd0I7U0FDdEMsQ0FBQyxDQUFDO1FBRUgsSUFBSSxHQUFHLENBQUMsU0FBUyxDQUFDLElBQUksRUFBRSwyQkFBMkIsRUFBRTtZQUNuRCxLQUFLLEVBQUUsbUJBQW1CLENBQUMsWUFBWTtZQUN2QyxXQUFXLEVBQUUsd0RBQXdEO1NBQ3RFLENBQUMsQ0FBQztRQUVILElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsY0FBYyxFQUFFO1lBQ3RDLEtBQUssRUFBRSxXQUFXLElBQUksQ0FBQyxNQUFNLGtEQUFrRCxJQUFJLENBQUMsTUFBTSxvQkFBb0IsU0FBUyxDQUFDLGFBQWEsRUFBRTtZQUN2SSxXQUFXLEVBQUUsMEJBQTBCO1NBQ3hDLENBQUMsQ0FBQztJQUNMLENBQUM7Q0FDRjtBQXpxQkQsNERBeXFCQyIsInNvdXJjZXNDb250ZW50IjpbImltcG9ydCAqIGFzIGNkayBmcm9tICdhd3MtY2RrLWxpYic7XG5pbXBvcnQgKiBhcyBsYW1iZGEgZnJvbSAnYXdzLWNkay1saWIvYXdzLWxhbWJkYSc7XG5pbXBvcnQgKiBhcyBlYzIgZnJvbSAnYXdzLWNkay1saWIvYXdzLWVjMic7XG5pbXBvcnQgKiBhcyBzcXMgZnJvbSAnYXdzLWNkay1saWIvYXdzLXNxcyc7XG5pbXBvcnQgKiBhcyBzMyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtczMnO1xuaW1wb3J0ICogYXMgaWFtIGZyb20gJ2F3cy1jZGstbGliL2F3cy1pYW0nO1xuaW1wb3J0ICogYXMgZXZlbnRzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1ldmVudHMnO1xuaW1wb3J0ICogYXMgdGFyZ2V0cyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZXZlbnRzLXRhcmdldHMnO1xuaW1wb3J0ICogYXMgc291cmNlcyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtbGFtYmRhLWV2ZW50LXNvdXJjZXMnO1xuaW1wb3J0ICogYXMga21zIGZyb20gJ2F3cy1jZGstbGliL2F3cy1rbXMnO1xuaW1wb3J0ICogYXMgbG9ncyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtbG9ncyc7XG5pbXBvcnQgKiBhcyBzc20gZnJvbSAnYXdzLWNkay1saWIvYXdzLXNzbSc7XG5pbXBvcnQgKiBhcyBjdyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtY2xvdWR3YXRjaCc7XG5pbXBvcnQgKiBhcyBzbnMgZnJvbSAnYXdzLWNkay1saWIvYXdzLXNucyc7XG5pbXBvcnQgKiBhcyBzbnNfc3Vic2NyaXB0aW9ucyBmcm9tICdhd3MtY2RrLWxpYi9hd3Mtc25zLXN1YnNjcmlwdGlvbnMnO1xuaW1wb3J0ICogYXMgY3dhY3Rpb25zIGZyb20gJ2F3cy1jZGstbGliL2F3cy1jbG91ZHdhdGNoLWFjdGlvbnMnO1xuaW1wb3J0ICogYXMgY3IgZnJvbSAnYXdzLWNkay1saWIvY3VzdG9tLXJlc291cmNlcyc7XG5pbXBvcnQgeyBDb25zdHJ1Y3QgfSBmcm9tICdjb25zdHJ1Y3RzJztcblxuZXhwb3J0IGNsYXNzIEFtaXJhTGFtYmRhUGFyYWxsZWxTdGFjayBleHRlbmRzIGNkay5TdGFjayB7XG4gIGNvbnN0cnVjdG9yKHNjb3BlOiBDb25zdHJ1Y3QsIGlkOiBzdHJpbmcsIHByb3BzPzogY2RrLlN0YWNrUHJvcHMpIHtcbiAgICBzdXBlcihzY29wZSwgaWQsIHByb3BzKTtcblxuICAgIC8vIFBhcmFtZXRlcnMgZm9yIGNvbmZpZ3VyYXRpb25cbiAgICBjb25zdCBhdGhlbmFEYlBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYURhdGFiYXNlJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnZGVmYXVsdCcsXG4gICAgICBkZXNjcmlwdGlvbjogJ0F0aGVuYSBkYXRhYmFzZSBuYW1lJ1xuICAgIH0pO1xuICAgIGNvbnN0IGF0aGVuYU91dHB1dFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYU91dHB1dCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3MzOi8vYXRoZW5hLXF1ZXJ5LXJlc3VsdHMvJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnQXRoZW5hIHF1ZXJ5IG91dHB1dCBTMyBwYXRoJ1xuICAgIH0pO1xuICAgIGNvbnN0IGF0aGVuYVF1ZXJ5UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXRoZW5hUXVlcnknLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdTRUxFQ1QgYWN0aXZpdHlfaWQgRlJPTSBhY3Rpdml0aWVzIFdIRVJFIHByb2Nlc3NfZmxhZyA9IDEnLFxuICAgICAgZGVzY3JpcHRpb246ICdBdGhlbmEgU1FMIHRvIHByb2R1Y2UgYWN0aXZpdHkgSURzJ1xuICAgIH0pO1xuICAgIGNvbnN0IG1vZGVsUGF0aFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ01vZGVsUGF0aCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2ZhY2Vib29rL3dhdjJ2ZWMyLWJhc2UtOTYwaCcsXG4gICAgICBkZXNjcmlwdGlvbjogJ0hGIG1vZGVsIHBhdGggZm9yIFdhdjJWZWMyJ1xuICAgIH0pO1xuICAgIGNvbnN0IHJlc3VsdHNQcmVmaXhQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdSZXN1bHRzUHJlZml4Jywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAncmVzdWx0cy8nLFxuICAgICAgZGVzY3JpcHRpb246ICdTMyBrZXkgcHJlZml4IGZvciByZXN1bHRzIHdyaXRlcydcbiAgICB9KTtcbiAgICBjb25zdCBhdWRpb0J1Y2tldE5hbWVQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdWRpb0J1Y2tldE5hbWUnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBTMyBidWNrZXQgbmFtZSBmb3IgaW5wdXQgYXVkaW8gKHJlYWQtb25seSkuIExlYXZlIGJsYW5rIHRvIHNraXAuJ1xuICAgIH0pO1xuICAgIGNvbnN0IHNsYWNrV2ViaG9va1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1NsYWNrV2ViaG9va1VybCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ1NsYWNrIHdlYmhvb2sgVVJMIGZvciBqb2IgY29tcGxldGlvbiBhbmQgZXJyb3Igbm90aWZpY2F0aW9ucycsXG4gICAgICBub0VjaG86IHRydWVcbiAgICB9KTtcbiAgICBjb25zdCB0cml0b25DbHVzdGVyVXJsUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnVHJpdG9uQ2x1c3RlclVybCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIFRyaXRvbiBHUFUgY2x1c3RlciBVUkwgZm9yIHJlbW90ZSBpbmZlcmVuY2UuIExlYXZlIGJsYW5rIHRvIGF1dG8tcmVzb2x2ZSBmcm9tIFNTTSBwYXJhbWV0ZXIgL2FtaXJhL3RyaXRvbl9hbGJfdXJsLidcbiAgICB9KTtcblxuICAgIGNvbnN0IGVuYWJsZVRyaXRvblBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0VuYWJsZVRyaXRvbicsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2ZhbHNlJyxcbiAgICAgIGFsbG93ZWRWYWx1ZXM6IFsndHJ1ZScsICdmYWxzZSddLFxuICAgICAgZGVzY3JpcHRpb246ICdFbmFibGUgY2FsbGluZyBhIFRyaXRvbiBjbHVzdGVyIChpbnRlcm5hbCBBTEIpIGZyb20gdGhlIHByb2Nlc3NpbmcgTGFtYmRhJ1xuICAgIH0pO1xuXG4gICAgLy8gT3B0aW9uYWwgVlBDIGF0dGFjaG1lbnQgZm9yIGNhbGxpbmcgaW50ZXJuYWwgQUxCIGRpcmVjdGx5XG4gICAgY29uc3QgdnBjSWRQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdWcGNJZCcsIHtcbiAgICAgIHR5cGU6ICdBV1M6OkVDMjo6VlBDOjpJZCcsXG4gICAgICBkZWZhdWx0OiBjZGsuQXdzLk5PX1ZBTFVFIGFzIGFueSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnVlBDIElEIHRvIGF0dGFjaCB0aGUgcHJvY2Vzc2luZyBMYW1iZGEgdG8gKGZvciBpbnRlcm5hbCBBTEIgYWNjZXNzKSdcbiAgICB9KTtcbiAgICBjb25zdCBwcml2YXRlU3VibmV0SWRzQ3N2UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnUHJpdmF0ZVN1Ym5ldElkc0NzdicsIHtcbiAgICAgIHR5cGU6ICdMaXN0PEFXUzo6RUMyOjpTdWJuZXQ6OklkPicsXG4gICAgICBkZXNjcmlwdGlvbjogJ1ByaXZhdGUgc3VibmV0IElEcyBmb3IgdGhlIExhbWJkYSBWUEMgY29uZmlnJ1xuICAgIH0pO1xuICAgIGNvbnN0IGxhbWJkYVNlY3VyaXR5R3JvdXBJZFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0xhbWJkYVNlY3VyaXR5R3JvdXBJZCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogY2RrLkF3cy5OT19WQUxVRSBhcyBhbnksXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIGV4aXN0aW5nIFNlY3VyaXR5IEdyb3VwIElEIGZvciB0aGUgcHJvY2Vzc2luZyBMYW1iZGEnXG4gICAgfSk7XG4gICAgY29uc3QgdnBjUHJvdmlkZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnVnBjUHJvdmlkZWQnLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uTm90KGNkay5Gbi5jb25kaXRpb25FcXVhbHModnBjSWRQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpXG4gICAgfSk7XG4gICAgY29uc3QgbGFtYmRhU2dQcm92aWRlZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdMYW1iZGFTZ1Byb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGxhbWJkYVNlY3VyaXR5R3JvdXBJZFBhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSlcbiAgICB9KTtcblxuICAgIC8vIEtNUyBrZXkgZm9yIGVuY3J5cHRpb25cbiAgICBjb25zdCBrbXNLZXkgPSBuZXcga21zLktleSh0aGlzLCAnQW1pcmFQYXJhbGxlbEtleScsIHtcbiAgICAgIGVuYWJsZUtleVJvdGF0aW9uOiB0cnVlLFxuICAgICAgYWxpYXM6ICdhbGlhcy9hbWlyYS1sYW1iZGEtcGFyYWxsZWwnXG4gICAgfSk7XG5cbiAgICAvLyBSZXN1bHRzIGJ1Y2tldFxuICAgIGNvbnN0IGFjY2Vzc0xvZ3NCdWNrZXQgPSBuZXcgczMuQnVja2V0KHRoaXMsICdMYW1iZGFBY2Nlc3NMb2dzQnVja2V0Jywge1xuICAgICAgdmVyc2lvbmVkOiBmYWxzZSxcbiAgICAgIGJsb2NrUHVibGljQWNjZXNzOiBzMy5CbG9ja1B1YmxpY0FjY2Vzcy5CTE9DS19BTEwsXG4gICAgICBlbmNyeXB0aW9uOiBzMy5CdWNrZXRFbmNyeXB0aW9uLlMzX01BTkFHRUQsXG4gICAgICBlbmZvcmNlU1NMOiB0cnVlLFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuUkVUQUlOXG4gICAgfSk7XG5cbiAgICBjb25zdCByZXN1bHRzQnVja2V0ID0gbmV3IHMzLkJ1Y2tldCh0aGlzLCAnUmVzdWx0c0J1Y2tldCcsIHtcbiAgICAgIHZlcnNpb25lZDogZmFsc2UsXG4gICAgICBibG9ja1B1YmxpY0FjY2VzczogczMuQmxvY2tQdWJsaWNBY2Nlc3MuQkxPQ0tfQUxMLFxuICAgICAgZW5jcnlwdGlvbjogczMuQnVja2V0RW5jcnlwdGlvbi5LTVMsXG4gICAgICBlbmNyeXB0aW9uS2V5OiBrbXNLZXksXG4gICAgICBidWNrZXRLZXlFbmFibGVkOiB0cnVlLFxuICAgICAgc2VydmVyQWNjZXNzTG9nc0J1Y2tldDogYWNjZXNzTG9nc0J1Y2tldCxcbiAgICAgIHNlcnZlckFjY2Vzc0xvZ3NQcmVmaXg6ICdzMy1hY2Nlc3MtbG9ncy8nLFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZSxcbiAgICAgIGxpZmVjeWNsZVJ1bGVzOiBbXG4gICAgICAgIHtcbiAgICAgICAgICBpZDogJ0ludGVsbGlnZW50VGllcmluZ05vdycsXG4gICAgICAgICAgZW5hYmxlZDogdHJ1ZSxcbiAgICAgICAgICB0cmFuc2l0aW9uczogW1xuICAgICAgICAgICAgeyBzdG9yYWdlQ2xhc3M6IHMzLlN0b3JhZ2VDbGFzcy5JTlRFTExJR0VOVF9USUVSSU5HLCB0cmFuc2l0aW9uQWZ0ZXI6IGNkay5EdXJhdGlvbi5kYXlzKDApIH1cbiAgICAgICAgICBdXG4gICAgICAgIH0sXG4gICAgICAgIHtcbiAgICAgICAgICBpZDogJ1RyYW5zaXRpb25Ub0lBMzBkJyxcbiAgICAgICAgICBlbmFibGVkOiB0cnVlLFxuICAgICAgICAgIHRyYW5zaXRpb25zOiBbXG4gICAgICAgICAgICB7IHN0b3JhZ2VDbGFzczogczMuU3RvcmFnZUNsYXNzLklORlJFUVVFTlRfQUNDRVNTLCB0cmFuc2l0aW9uQWZ0ZXI6IGNkay5EdXJhdGlvbi5kYXlzKDMwKSB9XG4gICAgICAgICAgXVxuICAgICAgICB9LFxuICAgICAgICB7XG4gICAgICAgICAgaWQ6ICdUcmFuc2l0aW9uVG9HbGFjaWVyMTIwZCcsXG4gICAgICAgICAgZW5hYmxlZDogdHJ1ZSxcbiAgICAgICAgICB0cmFuc2l0aW9uczogW1xuICAgICAgICAgICAgeyBzdG9yYWdlQ2xhc3M6IHMzLlN0b3JhZ2VDbGFzcy5HTEFDSUVSX0lOU1RBTlRfUkVUUklFVkFMLCB0cmFuc2l0aW9uQWZ0ZXI6IGNkay5EdXJhdGlvbi5kYXlzKDEyMCkgfVxuICAgICAgICAgIF1cbiAgICAgICAgfVxuICAgICAgXSxcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNkay5SZW1vdmFsUG9saWN5LlJFVEFJTlxuICAgIH0pO1xuICAgIHJlc3VsdHNCdWNrZXQuYWRkVG9SZXNvdXJjZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBzaWQ6ICdEZW55SW5zZWN1cmVUcmFuc3BvcnQnLFxuICAgICAgZWZmZWN0OiBpYW0uRWZmZWN0LkRFTlksXG4gICAgICBwcmluY2lwYWxzOiBbbmV3IGlhbS5BbnlQcmluY2lwYWwoKV0sXG4gICAgICBhY3Rpb25zOiBbJ3MzOionXSxcbiAgICAgIHJlc291cmNlczogW3Jlc3VsdHNCdWNrZXQuYnVja2V0QXJuLCBgJHtyZXN1bHRzQnVja2V0LmJ1Y2tldEFybn0vKmBdLFxuICAgICAgY29uZGl0aW9uczogeyBCb29sOiB7ICdhd3M6U2VjdXJlVHJhbnNwb3J0JzogJ2ZhbHNlJyB9IH1cbiAgICB9KSk7XG4gICAgcmVzdWx0c0J1Y2tldC5hZGRUb1Jlc291cmNlUG9saWN5KG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgIHNpZDogJ0RlbnlVbkVuY3J5cHRlZE9iamVjdFVwbG9hZHMnLFxuICAgICAgZWZmZWN0OiBpYW0uRWZmZWN0LkRFTlksXG4gICAgICBwcmluY2lwYWxzOiBbbmV3IGlhbS5BbnlQcmluY2lwYWwoKV0sXG4gICAgICBhY3Rpb25zOiBbJ3MzOlB1dE9iamVjdCddLFxuICAgICAgcmVzb3VyY2VzOiBbYCR7cmVzdWx0c0J1Y2tldC5idWNrZXRBcm59LypgXSxcbiAgICAgIGNvbmRpdGlvbnM6IHsgU3RyaW5nTm90RXF1YWxzOiB7ICdzMzp4LWFtei1zZXJ2ZXItc2lkZS1lbmNyeXB0aW9uJzogJ2F3czprbXMnIH0gfVxuICAgIH0pKTtcblxuICAgIC8vIFNRUyBEZWFkIExldHRlciBRdWV1ZVxuICAgIGNvbnN0IGRscSA9IG5ldyBzcXMuUXVldWUodGhpcywgJ1Byb2Nlc3NpbmdETFEnLCB7XG4gICAgICByZXRlbnRpb25QZXJpb2Q6IGNkay5EdXJhdGlvbi5kYXlzKDE0KSxcbiAgICAgIGVuY3J5cHRpb246IHNxcy5RdWV1ZUVuY3J5cHRpb24uS01TLFxuICAgICAgZW5jcnlwdGlvbk1hc3RlcktleToga21zS2V5LFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZVxuICAgIH0pO1xuXG4gICAgLy8gTWFpbiBTUVMgcXVldWUgZm9yIHRhc2tzXG4gICAgY29uc3QgdGFza3NRdWV1ZSA9IG5ldyBzcXMuUXVldWUodGhpcywgJ1Rhc2tzUXVldWUnLCB7XG4gICAgICB2aXNpYmlsaXR5VGltZW91dDogY2RrLkR1cmF0aW9uLmhvdXJzKDIpLFxuICAgICAgZGVhZExldHRlclF1ZXVlOiB7IHF1ZXVlOiBkbHEsIG1heFJlY2VpdmVDb3VudDogMyB9LFxuICAgICAgZW5jcnlwdGlvbjogc3FzLlF1ZXVlRW5jcnlwdGlvbi5LTVMsXG4gICAgICBlbmNyeXB0aW9uTWFzdGVyS2V5OiBrbXNLZXksXG4gICAgICBlbmZvcmNlU1NMOiB0cnVlLFxuICAgICAgcmVjZWl2ZU1lc3NhZ2VXYWl0VGltZTogY2RrLkR1cmF0aW9uLnNlY29uZHMoMCksXG4gICAgfSk7XG5cbiAgICAvLyBDbG91ZFdhdGNoIExvZyBHcm91cCBmb3IgTGFtYmRhXG4gICAgY29uc3QgbG9nR3JvdXAgPSBuZXcgbG9ncy5Mb2dHcm91cCh0aGlzLCAnUHJvY2Vzc2luZ0xvZ0dyb3VwJywge1xuICAgICAgbG9nR3JvdXBOYW1lOiAnL2F3cy9sYW1iZGEvYW1pcmEtcGFyYWxsZWwtcHJvY2Vzc29yJyxcbiAgICAgIHJldGVudGlvbjogbG9ncy5SZXRlbnRpb25EYXlzLk9ORV9NT05USCxcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNkay5SZW1vdmFsUG9saWN5LkRFU1RST1ksXG4gICAgICBlbmNyeXB0aW9uS2V5OiBrbXNLZXlcbiAgICB9KTtcblxuICAgIC8vIENvbmRpdGlvbnNcbiAgICBjb25zdCBhdWRpb1Byb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ0F1ZGlvQnVja2V0UHJvdmlkZWQnLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uTm90KGNkay5Gbi5jb25kaXRpb25FcXVhbHMoYXVkaW9CdWNrZXROYW1lUGFyYW0udmFsdWVBc1N0cmluZywgJycpKVxuICAgIH0pO1xuICAgIGNvbnN0IHRyaXRvblVybFByb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ1RyaXRvblVybFByb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHRyaXRvbkNsdXN0ZXJVcmxQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpXG4gICAgfSk7XG4gICAgY29uc3QgdXNlVHJpdG9uQ29uZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdVc2VUcml0b25Db25kJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbkVxdWFscyhlbmFibGVUcml0b25QYXJhbS52YWx1ZUFzU3RyaW5nLCAndHJ1ZScpXG4gICAgfSk7XG5cbiAgICAvLyBDdXN0b20gcmVzb3VyY2UgdG8gZmFpbCBmYXN0IGlmIFNTTSAvYW1pcmEvdHJpdG9uX2FsYl91cmwgaXMgbWlzc2luZyB3aGVuIFRyaXRvbiBVUkwgcGFyYW0gaXMgYmxhbmtcbiAgICBjb25zdCBzc21QYXJhbU5hbWUgPSAnL2FtaXJhL3RyaXRvbl9hbGJfdXJsJztcbiAgICBjb25zdCBzc21QYXJhbUFybiA9IGNkay5Bcm4uZm9ybWF0KHsgc2VydmljZTogJ3NzbScsIHJlc291cmNlOiAncGFyYW1ldGVyJywgcmVzb3VyY2VOYW1lOiAnYW1pcmEvdHJpdG9uX2FsYl91cmwnIH0sIHRoaXMpO1xuICAgIGNvbnN0IHRyaXRvblVybFNzbUNoZWNrID0gbmV3IGNyLkF3c0N1c3RvbVJlc291cmNlKHRoaXMsICdUcml0b25VcmxTc21DaGVjaycsIHtcbiAgICAgIG9uQ3JlYXRlOiB7XG4gICAgICAgIHNlcnZpY2U6ICdTU00nLFxuICAgICAgICBhY3Rpb246ICdnZXRQYXJhbWV0ZXInLFxuICAgICAgICBwYXJhbWV0ZXJzOiB7IE5hbWU6IHNzbVBhcmFtTmFtZSB9LFxuICAgICAgICBwaHlzaWNhbFJlc291cmNlSWQ6IGNyLlBoeXNpY2FsUmVzb3VyY2VJZC5vZignVHJpdG9uVXJsU3NtQ2hlY2snKVxuICAgICAgfSxcbiAgICAgIG9uVXBkYXRlOiB7XG4gICAgICAgIHNlcnZpY2U6ICdTU00nLFxuICAgICAgICBhY3Rpb246ICdnZXRQYXJhbWV0ZXInLFxuICAgICAgICBwYXJhbWV0ZXJzOiB7IE5hbWU6IHNzbVBhcmFtTmFtZSB9LFxuICAgICAgICBwaHlzaWNhbFJlc291cmNlSWQ6IGNyLlBoeXNpY2FsUmVzb3VyY2VJZC5vZignVHJpdG9uVXJsU3NtQ2hlY2snKVxuICAgICAgfSxcbiAgICAgIHBvbGljeTogY3IuQXdzQ3VzdG9tUmVzb3VyY2VQb2xpY3kuZnJvbVNka0NhbGxzKHsgcmVzb3VyY2VzOiBbc3NtUGFyYW1Bcm5dIH0pXG4gICAgfSk7XG4gICAgY29uc3Qgc3NtQ2hlY2tDb25kID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ1NzbUNoZWNrV2hlblVzaW5nVHJpdG9uQW5kTm9VcmwnLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uQW5kKGNkay5Gbi5jb25kaXRpb25FcXVhbHMoZW5hYmxlVHJpdG9uUGFyYW0udmFsdWVBc1N0cmluZywgJ3RydWUnKSwgY2RrLkZuLmNvbmRpdGlvbkVxdWFscyh0cml0b25DbHVzdGVyVXJsUGFyYW0udmFsdWVBc1N0cmluZywgJycpKVxuICAgIH0pO1xuICAgIGNvbnN0IGN1c3RvbVJlc291cmNlQ2ZuID0gdHJpdG9uVXJsU3NtQ2hlY2subm9kZS5kZWZhdWx0Q2hpbGQgYXMgY2RrLkNmbkN1c3RvbVJlc291cmNlO1xuICAgIGlmIChjdXN0b21SZXNvdXJjZUNmbiAmJiBjdXN0b21SZXNvdXJjZUNmbi5jZm5PcHRpb25zKSB7XG4gICAgICBjdXN0b21SZXNvdXJjZUNmbi5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IHNzbUNoZWNrQ29uZDtcbiAgICB9XG5cbiAgICAvLyBQcm9jZXNzaW5nIExhbWJkYSBmdW5jdGlvbiBhcyBEb2NrZXIgaW1hZ2UgKHByZS1jYWNoZWQgbW9kZWwpXG4gICAgY29uc3QgcHJvY2Vzc2luZ0xhbWJkYSA9IG5ldyBsYW1iZGEuRG9ja2VySW1hZ2VGdW5jdGlvbih0aGlzLCAnUHJvY2Vzc2luZ0Z1bmN0aW9uJywge1xuICAgICAgZnVuY3Rpb25OYW1lOiAnYW1pcmEtcGFyYWxsZWwtcHJvY2Vzc29yJyxcbiAgICAgIGNvZGU6IGxhbWJkYS5Eb2NrZXJJbWFnZUNvZGUuZnJvbUltYWdlQXNzZXQoJy4uL2xhbWJkYS9wYXJhbGxlbF9wcm9jZXNzb3InKSxcbiAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5taW51dGVzKDE1KSxcbiAgICAgIG1lbW9yeVNpemU6IDEwMjQwLFxuICAgICAgLy8gUmVtb3ZlZCByZXNlcnZlZCBjb25jdXJyZW5jeSB0byBhdm9pZCB1bmludGVuZGVkIHRocm90dGxpbmcgZHVyaW5nIHRlc3RzXG4gICAgICBkZWFkTGV0dGVyUXVldWU6IGRscSxcbiAgICAgIHRyYWNpbmc6IGxhbWJkYS5UcmFjaW5nLkFDVElWRSxcbiAgICAgIGVudmlyb25tZW50OiB7XG4gICAgICAgIFJFU1VMVFNfQlVDS0VUOiByZXN1bHRzQnVja2V0LmJ1Y2tldE5hbWUsXG4gICAgICAgIFJFU1VMVFNfUFJFRklYOiByZXN1bHRzUHJlZml4UGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgTU9ERUxfUEFUSDogJy9vcHQvbW9kZWxzL3dhdjJ2ZWMyLW9wdGltaXplZCcsXG4gICAgICAgIEFVRElPX0JVQ0tFVDogYXVkaW9CdWNrZXROYW1lUGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgS01TX0tFWV9JRDoga21zS2V5LmtleUlkLFxuICAgICAgICBTTEFDS19XRUJIT09LX1VSTDogc2xhY2tXZWJob29rUGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgTUFYX0NPTkNVUlJFTkNZOiAnMTAnLCAvLyBUdW5lZCBmb3IgaW5pdGlhbCBhbGlnbm1lbnQ7IGFkanVzdCB2aWEgZW52IGlmIG5lZWRlZFxuICAgICAgICBCQVRDSF9BTExfUEhSQVNFUzogJ3RydWUnLFxuICAgICAgICBVU0VfRkxPQVQxNjogJ3RydWUnLFxuICAgICAgICBJTkNMVURFX0NPTkZJREVOQ0U6ICd0cnVlJyxcbiAgICAgICAgVEVTVF9NT0RFOiAnZmFsc2UnLFxuICAgICAgICBVU0VfVFJJVE9OOiBlbmFibGVUcml0b25QYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBUUklUT05fVVJMOiBjZGsuVG9rZW4uYXNTdHJpbmcoXG4gICAgICAgICAgY2RrLkZuLmNvbmRpdGlvbklmKFxuICAgICAgICAgICAgdXNlVHJpdG9uQ29uZC5sb2dpY2FsSWQsXG4gICAgICAgICAgICBjZGsuRm4uY29uZGl0aW9uSWYoXG4gICAgICAgICAgICAgIHRyaXRvblVybFByb3ZpZGVkLmxvZ2ljYWxJZCxcbiAgICAgICAgICAgICAgdHJpdG9uQ2x1c3RlclVybFBhcmFtLnZhbHVlQXNTdHJpbmcsXG4gICAgICAgICAgICAgIHNzbS5TdHJpbmdQYXJhbWV0ZXIudmFsdWVGb3JTdHJpbmdQYXJhbWV0ZXIodGhpcywgJy9hbWlyYS90cml0b25fYWxiX3VybCcpXG4gICAgICAgICAgICApLFxuICAgICAgICAgICAgJydcbiAgICAgICAgICApXG4gICAgICAgICksXG4gICAgICAgIFRSSVRPTl9NT0RFTDogJ3cydjInLFxuICAgICAgICBQWVRIT05PUFRJTUlaRTogJzInLFxuICAgICAgICBUT1JDSF9OVU1fVEhSRUFEUzogJzYnLFxuICAgICAgICBPTVBfTlVNX1RIUkVBRFM6ICc2JyxcbiAgICAgICAgVFJBTlNGT1JNRVJTX0NBQ0hFOiAnL3RtcC9tb2RlbHMnLFxuICAgICAgICBIRl9IVUJfQ0FDSEU6ICcvdG1wL2hmX2NhY2hlJ1xuICAgICAgfVxuICAgIH0pO1xuXG4gICAgLy8gVlBDIGNvbmZpZ3VyYXRpb24gaXMgaGFuZGxlZCBkaXJlY3RseSBpbiBDRk4gc2luY2UgQ0RLIHZhbGlkYXRpb24gY29uZmxpY3RzIHdpdGggY29uZGl0aW9uYWwgcGFyYW1ldGVyc1xuICAgIGNvbnN0IHN1Ym5ldHNMaXN0ID0gcHJpdmF0ZVN1Ym5ldElkc0NzdlBhcmFtLnZhbHVlQXNMaXN0O1xuICAgIGNvbnN0IGNmbkZ1bmMgPSBwcm9jZXNzaW5nTGFtYmRhLm5vZGUuZGVmYXVsdENoaWxkIGFzIGxhbWJkYS5DZm5GdW5jdGlvbjtcbiAgICBjZm5GdW5jLnZwY0NvbmZpZyA9IGNkay5Ub2tlbi5hc0FueShjZGsuRm4uY29uZGl0aW9uSWYoXG4gICAgICB2cGNQcm92aWRlZC5sb2dpY2FsSWQsXG4gICAgICB7XG4gICAgICAgIFNlY3VyaXR5R3JvdXBJZHM6IGNkay5Ub2tlbi5hc0xpc3QoY2RrLkZuLmNvbmRpdGlvbklmKGxhbWJkYVNnUHJvdmlkZWQubG9naWNhbElkLCBbbGFtYmRhU2VjdXJpdHlHcm91cElkUGFyYW0udmFsdWVBc1N0cmluZ10sIGNkay5Bd3MuTk9fVkFMVUUgYXMgYW55KSksXG4gICAgICAgIFN1Ym5ldElkczogc3VibmV0c0xpc3RcbiAgICAgIH0sXG4gICAgICBjZGsuQXdzLk5PX1ZBTFVFXG4gICAgKSk7XG5cbiAgICAvLyBFbmZvcmNlOiBpZiBWcGNJZCBpcyBwcm92aWRlZCwgTGFtYmRhU2VjdXJpdHlHcm91cElkIG11c3QgYmUgcHJvdmlkZWRcbiAgICBuZXcgY2RrLkNmblJ1bGUodGhpcywgJ1ZwY1JlcXVpcmVzTGFtYmRhU2cnLCB7XG4gICAgICBydWxlQ29uZGl0aW9uOiBjZGsuRm4uY29uZGl0aW9uQW5kKGNkay5Gbi5jb25kaXRpb25Ob3QoY2RrLkZuLmNvbmRpdGlvbkVxdWFscyh2cGNJZFBhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSksIGNkay5Gbi5jb25kaXRpb25FcXVhbHMoZW5hYmxlVHJpdG9uUGFyYW0udmFsdWVBc1N0cmluZywgJ3RydWUnKSksXG4gICAgICBhc3NlcnRpb25zOiBbXG4gICAgICAgIHtcbiAgICAgICAgICBhc3NlcnQ6IGNkay5Gbi5jb25kaXRpb25Ob3QoY2RrLkZuLmNvbmRpdGlvbkVxdWFscyhsYW1iZGFTZWN1cml0eUdyb3VwSWRQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpLFxuICAgICAgICAgIGFzc2VydERlc2NyaXB0aW9uOiAnV2hlbiBWcGNJZCBpcyBwcm92aWRlZCwgTGFtYmRhU2VjdXJpdHlHcm91cElkIG11c3QgYWxzbyBiZSBwcm92aWRlZC4nXG4gICAgICAgIH1cbiAgICAgIF1cbiAgICB9KTtcblxuICAgIC8vIFNRUyBFdmVudCBTb3VyY2UgZm9yIExhbWJkYVxuICAgIGNvbnN0IG1heEV2ZW50U291cmNlQ29uY3VycmVuY3lQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdNYXhFdmVudFNvdXJjZUNvbmN1cnJlbmN5Jywge1xuICAgICAgdHlwZTogJ051bWJlcicsXG4gICAgICBkZWZhdWx0OiAxMCxcbiAgICAgIGRlc2NyaXB0aW9uOiAnU1FTIGV2ZW50IHNvdXJjZSBtYXggY29uY3VycmVuY3kgZm9yIHRoZSBwcm9jZXNzaW5nIExhbWJkYSdcbiAgICB9KTtcblxuICAgIGNvbnN0IGV2ZW50U291cmNlID0gbmV3IHNvdXJjZXMuU3FzRXZlbnRTb3VyY2UodGFza3NRdWV1ZSwge1xuICAgICAgYmF0Y2hTaXplOiAxLFxuICAgICAgbWF4Q29uY3VycmVuY3k6IG1heEV2ZW50U291cmNlQ29uY3VycmVuY3lQYXJhbS52YWx1ZUFzTnVtYmVyLFxuICAgICAgcmVwb3J0QmF0Y2hJdGVtRmFpbHVyZXM6IHRydWUsXG4gICAgICBtYXhCYXRjaGluZ1dpbmRvdzogY2RrLkR1cmF0aW9uLnNlY29uZHMoMCksXG4gICAgfSk7XG4gICAgcHJvY2Vzc2luZ0xhbWJkYS5hZGRFdmVudFNvdXJjZShldmVudFNvdXJjZSk7XG5cbiAgICAvLyBPcHRpb25hbCB3YXJtaW5nIHJ1bGVcbiAgICBjb25zdCBlbmFibGVXYXJtaW5nUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnRW5hYmxlV2FybWluZycsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2ZhbHNlJyxcbiAgICAgIGFsbG93ZWRWYWx1ZXM6IFsndHJ1ZScsICdmYWxzZSddLFxuICAgICAgZGVzY3JpcHRpb246ICdFbmFibGUgcGVyaW9kaWMgd2FybSBpbnZvY2F0aW9uIHRvIHJlZHVjZSBjb2xkIHN0YXJ0cydcbiAgICB9KTtcbiAgICBjb25zdCB3YXJtUmF0ZU1pbnV0ZXNQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdXYXJtUmF0ZU1pbnV0ZXMnLCB7XG4gICAgICB0eXBlOiAnTnVtYmVyJyxcbiAgICAgIGRlZmF1bHQ6IDE1LFxuICAgICAgZGVzY3JpcHRpb246ICdXYXJtIHBpbmcgcmF0ZSBpbiBtaW51dGVzIHdoZW4gRW5hYmxlV2FybWluZz10cnVlJ1xuICAgIH0pO1xuICAgIGNvbnN0IHdhcm1FbmFibGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ1dhcm1FbmFibGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbkVxdWFscyhlbmFibGVXYXJtaW5nUGFyYW0udmFsdWVBc1N0cmluZywgJ3RydWUnKVxuICAgIH0pO1xuICAgIGNvbnN0IHdhcm1pbmdSdWxlID0gbmV3IGV2ZW50cy5DZm5SdWxlKHRoaXMsICdQcm9jZXNzaW5nV2FybVJ1bGUnLCB7XG4gICAgICBzY2hlZHVsZUV4cHJlc3Npb246IGNkay5Gbi5zdWIoJ3JhdGUoJHtNaW51dGVzfSBtaW51dGVzKScsIHsgTWludXRlczogd2FybVJhdGVNaW51dGVzUGFyYW0udmFsdWVBc1N0cmluZyB9KSxcbiAgICAgIHN0YXRlOiAnRU5BQkxFRCcsXG4gICAgICB0YXJnZXRzOiBbXG4gICAgICAgIHtcbiAgICAgICAgICBpZDogJ1RhcmdldDAnLFxuICAgICAgICAgIGFybjogcHJvY2Vzc2luZ0xhbWJkYS5mdW5jdGlvbkFybixcbiAgICAgICAgICBpbnB1dDogSlNPTi5zdHJpbmdpZnkoeyB3YXJtOiB0cnVlIH0pXG4gICAgICAgIH1cbiAgICAgIF1cbiAgICB9KTtcbiAgICB3YXJtaW5nUnVsZS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IHdhcm1FbmFibGVkO1xuICAgIGNvbnN0IHdhcm1QZXJtaXNzaW9uID0gbmV3IGxhbWJkYS5DZm5QZXJtaXNzaW9uKHRoaXMsICdBbGxvd0V2ZW50QnJpZGdlSW52b2tlV2FybScsIHtcbiAgICAgIGFjdGlvbjogJ2xhbWJkYTpJbnZva2VGdW5jdGlvbicsXG4gICAgICBmdW5jdGlvbk5hbWU6IHByb2Nlc3NpbmdMYW1iZGEuZnVuY3Rpb25OYW1lLFxuICAgICAgcHJpbmNpcGFsOiAnZXZlbnRzLmFtYXpvbmF3cy5jb20nLFxuICAgICAgc291cmNlQXJuOiB3YXJtaW5nUnVsZS5hdHRyQXJuXG4gICAgfSk7XG4gICAgd2FybVBlcm1pc3Npb24uY2ZuT3B0aW9ucy5jb25kaXRpb24gPSB3YXJtRW5hYmxlZDtcblxuICAgIGNvbnN0IGF1ZGlvUG9saWN5RG9jID0gbmV3IGlhbS5Qb2xpY3lEb2N1bWVudCh7XG4gICAgICBzdGF0ZW1lbnRzOiBbXG4gICAgICAgIG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgICAgICBhY3Rpb25zOiBbJ3MzOkxpc3RCdWNrZXQnXSxcbiAgICAgICAgICByZXNvdXJjZXM6IFtjZGsuRm4uc3ViKCdhcm46YXdzOnMzOjo6JHtCdWNrZXROYW1lfScsIHsgQnVja2V0TmFtZTogYXVkaW9CdWNrZXROYW1lUGFyYW0udmFsdWVBc1N0cmluZyB9KV1cbiAgICAgICAgfSksXG4gICAgICAgIG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgICAgICBhY3Rpb25zOiBbJ3MzOkdldE9iamVjdCddLFxuICAgICAgICAgIHJlc291cmNlczogW2Nkay5Gbi5zdWIoJ2Fybjphd3M6czM6Ojoke0J1Y2tldE5hbWV9LyonLCB7IEJ1Y2tldE5hbWU6IGF1ZGlvQnVja2V0TmFtZVBhcmFtLnZhbHVlQXNTdHJpbmcgfSldXG4gICAgICAgIH0pXG4gICAgICBdXG4gICAgfSk7XG5cbiAgICBjb25zdCBhdWRpb0NmblBvbGljeSA9IG5ldyBpYW0uQ2ZuUG9saWN5KHRoaXMsICdQcm9jZXNzaW5nTGFtYmRhQXVkaW9Qb2xpY3knLCB7XG4gICAgICBwb2xpY3lEb2N1bWVudDogYXVkaW9Qb2xpY3lEb2MsXG4gICAgICByb2xlczogW3Byb2Nlc3NpbmdMYW1iZGEucm9sZSEucm9sZU5hbWVdLFxuICAgICAgcG9saWN5TmFtZTogYFByb2Nlc3NpbmdMYW1iZGFBdWRpb1BvbGljeS0ke2Nkay5TdGFjay5vZih0aGlzKS5zdGFja05hbWV9YFxuICAgIH0pO1xuICAgIGF1ZGlvQ2ZuUG9saWN5LmNmbk9wdGlvbnMuY29uZGl0aW9uID0gYXVkaW9Qcm92aWRlZDtcblxuICAgIC8vIFJlc3VsdHMgYnVja2V0IGFuZCBLTVMgcGVybWlzc2lvbnNcbiAgICByZXN1bHRzQnVja2V0LmdyYW50V3JpdGUocHJvY2Vzc2luZ0xhbWJkYSk7XG4gICAga21zS2V5LmdyYW50RW5jcnlwdERlY3J5cHQocHJvY2Vzc2luZ0xhbWJkYSk7XG5cbiAgICAvLyBDbG91ZFdhdGNoIG1ldHJpY3MgcGVybWlzc2lvbnMgZm9yIGpvYiB0cmFja2luZ1xuICAgIHByb2Nlc3NpbmdMYW1iZGEuYWRkVG9Sb2xlUG9saWN5KG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgIGFjdGlvbnM6IFtcbiAgICAgICAgJ2Nsb3Vkd2F0Y2g6UHV0TWV0cmljRGF0YSdcbiAgICAgIF0sXG4gICAgICByZXNvdXJjZXM6IFsnKiddXG4gICAgfSkpO1xuXG4gICAgLy8gRW5xdWV1ZSBMYW1iZGEgZnVuY3Rpb25cbiAgICBjb25zdCBlbnF1ZXVlTGFtYmRhID0gbmV3IGxhbWJkYS5GdW5jdGlvbih0aGlzLCAnRW5xdWV1ZUZ1bmN0aW9uJywge1xuICAgICAgcnVudGltZTogbGFtYmRhLlJ1bnRpbWUuUFlUSE9OXzNfMTIsXG4gICAgICBoYW5kbGVyOiAnaW5kZXgubGFtYmRhX2hhbmRsZXInLFxuICAgICAgY29kZTogbGFtYmRhLkNvZGUuZnJvbUFzc2V0KCcuLi9sYW1iZGEvZW5xdWV1ZV9qb2JzJyksXG4gICAgICB0aW1lb3V0OiBjZGsuRHVyYXRpb24ubWludXRlcyg1KSxcbiAgICAgIHRyYWNpbmc6IGxhbWJkYS5UcmFjaW5nLkFDVElWRSxcbiAgICAgIGVudmlyb25tZW50OiB7XG4gICAgICAgIEpPQlNfUVVFVUVfVVJMOiB0YXNrc1F1ZXVlLnF1ZXVlVXJsLFxuICAgICAgICBBVEhFTkFfREFUQUJBU0U6IGF0aGVuYURiUGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgQVRIRU5BX09VVFBVVDogYXRoZW5hT3V0cHV0UGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgQVRIRU5BX1FVRVJZOiBhdGhlbmFRdWVyeVBhcmFtLnZhbHVlQXNTdHJpbmdcbiAgICAgIH1cbiAgICB9KTtcblxuICAgIC8vIElBTSBwZXJtaXNzaW9ucyBmb3IgZW5xdWV1ZSBMYW1iZGFcbiAgICBjb25zdCBhdGhlbmFXb3JrZ3JvdXBBcm4gPSBjZGsuQXJuLmZvcm1hdCh7XG4gICAgICBzZXJ2aWNlOiAnYXRoZW5hJyxcbiAgICAgIHJlc291cmNlOiAnd29ya2dyb3VwJyxcbiAgICAgIHJlc291cmNlTmFtZTogJ3ByaW1hcnknXG4gICAgfSwgdGhpcyk7XG4gICAgY29uc3QgZ2x1ZURiQXJuID0gY2RrLkFybi5mb3JtYXQoe1xuICAgICAgc2VydmljZTogJ2dsdWUnLFxuICAgICAgcmVzb3VyY2U6ICdkYXRhYmFzZScsXG4gICAgICByZXNvdXJjZU5hbWU6IGF0aGVuYURiUGFyYW0udmFsdWVBc1N0cmluZ1xuICAgIH0sIHRoaXMpO1xuICAgIGNvbnN0IGdsdWVUYWJsZVdpbGRjYXJkQXJuID0gY2RrLkFybi5mb3JtYXQoe1xuICAgICAgc2VydmljZTogJ2dsdWUnLFxuICAgICAgcmVzb3VyY2U6ICd0YWJsZScsXG4gICAgICByZXNvdXJjZU5hbWU6IGAke2F0aGVuYURiUGFyYW0udmFsdWVBc1N0cmluZ30vKmBcbiAgICB9LCB0aGlzKTtcblxuICAgIGVucXVldWVMYW1iZGEuYWRkVG9Sb2xlUG9saWN5KG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgIGFjdGlvbnM6IFsnYXRoZW5hOlN0YXJ0UXVlcnlFeGVjdXRpb24nLCAnYXRoZW5hOkdldFF1ZXJ5RXhlY3V0aW9uJywgJ2F0aGVuYTpHZXRRdWVyeVJlc3VsdHMnXSxcbiAgICAgIHJlc291cmNlczogW2F0aGVuYVdvcmtncm91cEFybl1cbiAgICB9KSk7XG4gICAgZW5xdWV1ZUxhbWJkYS5hZGRUb1JvbGVQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgYWN0aW9uczogWydnbHVlOkdldERhdGFiYXNlJ10sXG4gICAgICByZXNvdXJjZXM6IFtnbHVlRGJBcm5dXG4gICAgfSkpO1xuICAgIGVucXVldWVMYW1iZGEuYWRkVG9Sb2xlUG9saWN5KG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgIGFjdGlvbnM6IFsnZ2x1ZTpHZXRUYWJsZSddLFxuICAgICAgcmVzb3VyY2VzOiBbZ2x1ZVRhYmxlV2lsZGNhcmRBcm5dXG4gICAgfSkpO1xuXG4gICAgLy8gQXRoZW5hIG91dHB1dCBidWNrZXQgcGVybWlzc2lvbnNcbiAgICBjb25zdCBhdGhlbmFPdXRwdXRQYXJzZWQgPSBjZGsuRm4uc3BsaXQoJy8nLCBhdGhlbmFPdXRwdXRQYXJhbS52YWx1ZUFzU3RyaW5nKTtcbiAgICBjb25zdCBhdGhlbmFPdXRwdXRCdWNrZXQgPSBjZGsuRm4uc2VsZWN0KDIsIGF0aGVuYU91dHB1dFBhcnNlZCk7XG4gICAgZW5xdWV1ZUxhbWJkYS5hZGRUb1JvbGVQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgYWN0aW9uczogWydzMzpMaXN0QnVja2V0J10sXG4gICAgICByZXNvdXJjZXM6IFtjZGsuQXJuLmZvcm1hdCh7IHNlcnZpY2U6ICdzMycsIHJlc291cmNlOiBhdGhlbmFPdXRwdXRCdWNrZXQgfSwgdGhpcyldXG4gICAgfSkpO1xuICAgIGVucXVldWVMYW1iZGEuYWRkVG9Sb2xlUG9saWN5KG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgIGFjdGlvbnM6IFsnczM6R2V0T2JqZWN0JywgJ3MzOlB1dE9iamVjdCddLFxuICAgICAgcmVzb3VyY2VzOiBbY2RrLkFybi5mb3JtYXQoeyBzZXJ2aWNlOiAnczMnLCByZXNvdXJjZTogYCR7YXRoZW5hT3V0cHV0QnVja2V0fS8qYCB9LCB0aGlzKV1cbiAgICB9KSk7XG5cbiAgICB0YXNrc1F1ZXVlLmdyYW50U2VuZE1lc3NhZ2VzKGVucXVldWVMYW1iZGEpO1xuXG4gICAgLy8gU2NoZWR1bGUgZm9yIGF1dG9tYXRpYyBlbnF1ZXVlaW5nXG4gICAgY29uc3Qgc2NoZWR1bGVSdWxlID0gbmV3IGV2ZW50cy5SdWxlKHRoaXMsICdTY2hlZHVsZVJ1bGUnLCB7XG4gICAgICBkZXNjcmlwdGlvbjogJ1RyaWdnZXIgcGFyYWxsZWwgcHJvY2Vzc2luZyBwaXBlbGluZScsXG4gICAgICBzY2hlZHVsZTogZXZlbnRzLlNjaGVkdWxlLmNyb24oeyBtaW51dGU6ICcwJywgaG91cjogJzInIH0pXG4gICAgfSk7XG4gICAgc2NoZWR1bGVSdWxlLmFkZFRhcmdldChuZXcgdGFyZ2V0cy5MYW1iZGFGdW5jdGlvbihlbnF1ZXVlTGFtYmRhKSk7XG5cbiAgICAvLyBPcHRpb25hbCBBdGhlbmEgc3RhZ2luZyBjbGVhbnVwIExhbWJkYSArIHNjaGVkdWxlXG4gICAgY29uc3QgZW5hYmxlQXRoZW5hQ2xlYW51cFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0VuYWJsZUF0aGVuYUNsZWFudXAnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdmYWxzZScsXG4gICAgICBhbGxvd2VkVmFsdWVzOiBbJ3RydWUnLCAnZmFsc2UnXSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRW5hYmxlIHNjaGVkdWxlZCBBdGhlbmEgc3RhZ2luZyBjbGVhbnVwIChvcHRpb25hbCknXG4gICAgfSk7XG4gICAgY29uc3QgYXRoZW5hQ2xlYW51cEJ1Y2tldFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYUNsZWFudXBCdWNrZXQnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdTMyBidWNrZXQgZm9yIEF0aGVuYSBzdGFnaW5nIHJlc3VsdHMnXG4gICAgfSk7XG4gICAgY29uc3QgYXRoZW5hQ2xlYW51cFByZWZpeFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYUNsZWFudXBQcmVmaXgnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdhdGhlbmFfc3RhZ2luZycsXG4gICAgICBkZXNjcmlwdGlvbjogJ1MzIHByZWZpeCBmb3IgQXRoZW5hIHN0YWdpbmcgcmVzdWx0cydcbiAgICB9KTtcbiAgICBjb25zdCBhdGhlbmFDbGVhbnVwQWdlRGF5c1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYUNsZWFudXBBZ2VEYXlzJywge1xuICAgICAgdHlwZTogJ051bWJlcicsXG4gICAgICBkZWZhdWx0OiA3LFxuICAgICAgZGVzY3JpcHRpb246ICdEZWxldGUgc3RhZ2luZyBvYmplY3RzIG9sZGVyIHRoYW4gTiBkYXlzJ1xuICAgIH0pO1xuICAgIGNvbnN0IGNsZWFudXBFbmFibGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ0F0aGVuYUNsZWFudXBFbmFibGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbkVxdWFscyhlbmFibGVBdGhlbmFDbGVhbnVwUGFyYW0udmFsdWVBc1N0cmluZywgJ3RydWUnKVxuICAgIH0pO1xuXG4gICAgY29uc3QgY2xlYW51cExhbWJkYSA9IG5ldyBsYW1iZGEuRnVuY3Rpb24odGhpcywgJ0F0aGVuYVN0YWdpbmdDbGVhbnVwJywge1xuICAgICAgcnVudGltZTogbGFtYmRhLlJ1bnRpbWUuUFlUSE9OXzNfMTIsXG4gICAgICBoYW5kbGVyOiAnYXRoZW5hX3N0YWdpbmdfY2xlYW51cC5tYWluJyxcbiAgICAgIGNvZGU6IGxhbWJkYS5Db2RlLmZyb21Bc3NldCgnLi4vc2NyaXB0cycpLFxuICAgICAgdGltZW91dDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoNSlcbiAgICB9KTtcbiAgICAoY2xlYW51cExhbWJkYS5ub2RlLmRlZmF1bHRDaGlsZCBhcyBsYW1iZGEuQ2ZuRnVuY3Rpb24pLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gY2xlYW51cEVuYWJsZWQ7XG5cbiAgICBjbGVhbnVwTGFtYmRhLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbJ3MzOkxpc3RCdWNrZXQnLCAnczM6RGVsZXRlT2JqZWN0JywgJ3MzOkRlbGV0ZU9iamVjdFZlcnNpb24nXSxcbiAgICAgIHJlc291cmNlczogW1xuICAgICAgICBjZGsuQXJuLmZvcm1hdCh7IHNlcnZpY2U6ICdzMycsIHJlc291cmNlOiBhdGhlbmFDbGVhbnVwQnVja2V0UGFyYW0udmFsdWVBc1N0cmluZyB9LCB0aGlzKSxcbiAgICAgICAgY2RrLkFybi5mb3JtYXQoeyBzZXJ2aWNlOiAnczMnLCByZXNvdXJjZTogYCR7YXRoZW5hQ2xlYW51cEJ1Y2tldFBhcmFtLnZhbHVlQXNTdHJpbmd9LypgIH0sIHRoaXMpXG4gICAgICBdXG4gICAgfSkpO1xuXG4gICAgY29uc3QgY2xlYW51cFJ1bGUgPSBuZXcgZXZlbnRzLlJ1bGUodGhpcywgJ0F0aGVuYUNsZWFudXBTY2hlZHVsZScsIHtcbiAgICAgIGRlc2NyaXB0aW9uOiAnU2NoZWR1bGVkIEF0aGVuYSBzdGFnaW5nIGNsZWFudXAnLFxuICAgICAgc2NoZWR1bGU6IGV2ZW50cy5TY2hlZHVsZS5jcm9uKHsgbWludXRlOiAnMCcsIGhvdXI6ICczJyB9KVxuICAgIH0pO1xuICAgIChjbGVhbnVwUnVsZS5ub2RlLmRlZmF1bHRDaGlsZCBhcyBldmVudHMuQ2ZuUnVsZSkuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBjbGVhbnVwRW5hYmxlZDtcbiAgICBjbGVhbnVwUnVsZS5hZGRUYXJnZXQobmV3IHRhcmdldHMuTGFtYmRhRnVuY3Rpb24oY2xlYW51cExhbWJkYSwge1xuICAgICAgZXZlbnQ6IGV2ZW50cy5SdWxlVGFyZ2V0SW5wdXQuZnJvbU9iamVjdCh7XG4gICAgICAgIGJ1Y2tldDogYXRoZW5hQ2xlYW51cEJ1Y2tldFBhcmFtLnZhbHVlQXNTdHJpbmcsXG4gICAgICAgIHByZWZpeDogYXRoZW5hQ2xlYW51cFByZWZpeFBhcmFtLnZhbHVlQXNTdHJpbmcsXG4gICAgICAgIGFnZV9kYXlzOiBhdGhlbmFDbGVhbnVwQWdlRGF5c1BhcmFtLnZhbHVlQXNOdW1iZXIsXG4gICAgICB9KVxuICAgIH0pKTtcblxuICAgIC8vIE1hbnVhbCB0cmlnZ2VyIExhbWJkYVxuICAgIGNvbnN0IG1hbnVhbFRyaWdnZXJMYW1iZGEgPSBuZXcgbGFtYmRhLkZ1bmN0aW9uKHRoaXMsICdNYW51YWxUcmlnZ2VyRnVuY3Rpb24nLCB7XG4gICAgICBydW50aW1lOiBsYW1iZGEuUnVudGltZS5QWVRIT05fM18xMixcbiAgICAgIGhhbmRsZXI6ICdpbmRleC5sYW1iZGFfaGFuZGxlcicsXG4gICAgICBjb2RlOiBsYW1iZGEuQ29kZS5mcm9tQXNzZXQoJy4uL2xhbWJkYS9tYW51YWxfZW5xdWV1ZScpLFxuICAgICAgdGltZW91dDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMSksXG4gICAgICB0cmFjaW5nOiBsYW1iZGEuVHJhY2luZy5BQ1RJVkUsXG4gICAgICBlbnZpcm9ubWVudDogeyBKT0JTX1FVRVVFX1VSTDogdGFza3NRdWV1ZS5xdWV1ZVVybCB9XG4gICAgfSk7XG4gICAgdGFza3NRdWV1ZS5ncmFudFNlbmRNZXNzYWdlcyhtYW51YWxUcmlnZ2VyTGFtYmRhKTtcblxuICAgIC8vIFNOUyB0b3BpYyBmb3IgYWxlcnRzXG4gICAgY29uc3QgYWxlcnRzVG9waWMgPSBuZXcgc25zLlRvcGljKHRoaXMsICdBbGVydHNUb3BpYycsIHtcbiAgICAgIGRpc3BsYXlOYW1lOiAnQW1pcmEgTGFtYmRhIFBhcmFsbGVsIEFsZXJ0cydcbiAgICB9KTtcblxuICAgIC8vIFNsYWNrIG5vdGlmaWNhdGlvbiBMYW1iZGFcbiAgICBjb25zdCBzbGFja05vdGlmaWVyTGFtYmRhID0gbmV3IGxhbWJkYS5GdW5jdGlvbih0aGlzLCAnU2xhY2tOb3RpZmllckZ1bmN0aW9uJywge1xuICAgICAgcnVudGltZTogbGFtYmRhLlJ1bnRpbWUuUFlUSE9OXzNfMTIsXG4gICAgICBoYW5kbGVyOiAnaW5kZXgubGFtYmRhX2hhbmRsZXInLFxuICAgICAgY29kZTogbGFtYmRhLkNvZGUuZnJvbUFzc2V0KCcuLi9sYW1iZGEvc2xhY2tfbm90aWZpZXInKSxcbiAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDMwKSxcbiAgICAgIHRyYWNpbmc6IGxhbWJkYS5UcmFjaW5nLkFDVElWRSxcbiAgICAgIGVudmlyb25tZW50OiB7XG4gICAgICAgIFNMQUNLX1dFQkhPT0tfVVJMOiBzbGFja1dlYmhvb2tQYXJhbS52YWx1ZUFzU3RyaW5nXG4gICAgICB9XG4gICAgfSk7XG5cbiAgICAvLyBTdWJzY3JpYmUgU2xhY2sgbm90aWZpZXIgdG8gU05TIGFsZXJ0c1xuICAgIGNvbnN0IHNsYWNrV2ViaG9va1Byb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ1NsYWNrV2ViaG9va1Byb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHNsYWNrV2ViaG9va1BhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSlcbiAgICB9KTtcblxuICAgIC8vIExhbWJkYSBwZXJtaXNzaW9uIGZvciBTTlMgdG8gaW52b2tlIFNsYWNrIG5vdGlmaWVyXG4gICAgc2xhY2tOb3RpZmllckxhbWJkYS5hZGRQZXJtaXNzaW9uKCdBbGxvd1NOU0ludm9rZScsIHtcbiAgICAgIHByaW5jaXBhbDogbmV3IGlhbS5TZXJ2aWNlUHJpbmNpcGFsKCdzbnMuYW1hem9uYXdzLmNvbScpLFxuICAgICAgc291cmNlQXJuOiBhbGVydHNUb3BpYy50b3BpY0FyblxuICAgIH0pO1xuXG4gICAgLy8gQ29uZGl0aW9uYWwgU05TIHN1YnNjcmlwdGlvbiB0byBTbGFjayBub3RpZmllclxuICAgIGNvbnN0IHNsYWNrU3Vic2NyaXB0aW9uID0gbmV3IHNucy5DZm5TdWJzY3JpcHRpb24odGhpcywgJ1NsYWNrTm90aWZpZXJTdWJzY3JpcHRpb24nLCB7XG4gICAgICB0b3BpY0FybjogYWxlcnRzVG9waWMudG9waWNBcm4sXG4gICAgICBwcm90b2NvbDogJ2xhbWJkYScsXG4gICAgICBlbmRwb2ludDogc2xhY2tOb3RpZmllckxhbWJkYS5mdW5jdGlvbkFyblxuICAgIH0pO1xuICAgIHNsYWNrU3Vic2NyaXB0aW9uLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gc2xhY2tXZWJob29rUHJvdmlkZWQ7XG5cbiAgICAvLyBDbG91ZFdhdGNoIEFsYXJtc1xuICAgIGNvbnN0IGRscURlcHRoQWxhcm0gPSBuZXcgY3cuQWxhcm0odGhpcywgJ0RMUURlcHRoQWxhcm0nLCB7XG4gICAgICBtZXRyaWM6IGRscS5tZXRyaWNBcHByb3hpbWF0ZU51bWJlck9mTWVzc2FnZXNWaXNpYmxlKCksXG4gICAgICB0aHJlc2hvbGQ6IDEsXG4gICAgICBldmFsdWF0aW9uUGVyaW9kczogMSxcbiAgICAgIGNvbXBhcmlzb25PcGVyYXRvcjogY3cuQ29tcGFyaXNvbk9wZXJhdG9yLkdSRUFURVJfVEhBTl9PUl9FUVVBTF9UT19USFJFU0hPTERcbiAgICB9KTtcblxuICAgIGNvbnN0IHByb2Nlc3NpbmdFcnJvcnNBbGFybSA9IG5ldyBjdy5BbGFybSh0aGlzLCAnUHJvY2Vzc2luZ0Vycm9yc0FsYXJtJywge1xuICAgICAgbWV0cmljOiBwcm9jZXNzaW5nTGFtYmRhLm1ldHJpY0Vycm9ycygpLFxuICAgICAgdGhyZXNob2xkOiAxMCxcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAyLFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuR1JFQVRFUl9USEFOX1RIUkVTSE9MRFxuICAgIH0pO1xuXG4gICAgY29uc3QgcXVldWVEZXB0aEFsYXJtID0gbmV3IGN3LkFsYXJtKHRoaXMsICdRdWV1ZURlcHRoQWxhcm0nLCB7XG4gICAgICBtZXRyaWM6IHRhc2tzUXVldWUubWV0cmljQXBwcm94aW1hdGVOdW1iZXJPZk1lc3NhZ2VzVmlzaWJsZSgpLFxuICAgICAgdGhyZXNob2xkOiAxMDAwLFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDMsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fVEhSRVNIT0xEXG4gICAgfSk7XG5cbiAgICBjb25zdCBxdWV1ZUFnZUFsYXJtID0gbmV3IGN3LkFsYXJtKHRoaXMsICdRdWV1ZUFnZUFsYXJtJywge1xuICAgICAgbWV0cmljOiB0YXNrc1F1ZXVlLm1ldHJpY0FwcHJveGltYXRlQWdlT2ZPbGRlc3RNZXNzYWdlKCksXG4gICAgICB0aHJlc2hvbGQ6IDMwMCxcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAzLFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuR1JFQVRFUl9USEFOX1RIUkVTSE9MRFxuICAgIH0pO1xuXG4gICAgLy8gSm9iIGNvbXBsZXRpb24gZGV0ZWN0aW9uIC0gcXVldWUgZW1wdHkgQU5EIG5vIGFjdGl2ZSBMYW1iZGEgZXhlY3V0aW9uc1xuICAgIGNvbnN0IGNvbmN1cnJlbnRFeGVjdXRpb25zTWV0cmljID0gbmV3IGN3Lk1ldHJpYyh7XG4gICAgICBuYW1lc3BhY2U6ICdBV1MvTGFtYmRhJyxcbiAgICAgIG1ldHJpY05hbWU6ICdDb25jdXJyZW50RXhlY3V0aW9ucycsXG4gICAgICBkaW1lbnNpb25zTWFwOiB7IEZ1bmN0aW9uTmFtZTogcHJvY2Vzc2luZ0xhbWJkYS5mdW5jdGlvbk5hbWUgfSxcbiAgICAgIHN0YXRpc3RpYzogJ0F2ZXJhZ2UnLFxuICAgICAgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygyKVxuICAgIH0pO1xuICAgIGNvbnN0IGpvYkNvbXBsZXRpb25FeHByID0gbmV3IGN3Lk1hdGhFeHByZXNzaW9uKHtcbiAgICAgIGV4cHJlc3Npb246ICdJRihxdWV1ZSA8IDEgQU5EIGNvbmN1cnJlbnQgPCAxLCAxLCAwKScsXG4gICAgICB1c2luZ01ldHJpY3M6IHtcbiAgICAgICAgcXVldWU6IHRhc2tzUXVldWUubWV0cmljQXBwcm94aW1hdGVOdW1iZXJPZk1lc3NhZ2VzVmlzaWJsZSh7XG4gICAgICAgICAgc3RhdGlzdGljOiAnQXZlcmFnZScsXG4gICAgICAgICAgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygyKVxuICAgICAgICB9KSxcbiAgICAgICAgY29uY3VycmVudDogY29uY3VycmVudEV4ZWN1dGlvbnNNZXRyaWNcbiAgICAgIH1cbiAgICB9KTtcbiAgICBjb25zdCBqb2JDb21wbGV0aW9uQWxhcm0gPSBuZXcgY3cuQWxhcm0odGhpcywgJ0pvYkNvbXBsZXRpb25BbGFybScsIHtcbiAgICAgIGFsYXJtTmFtZTogJ0pvYkNvbXBsZXRpb25EZXRlY3RlZCcsXG4gICAgICBhbGFybURlc2NyaXB0aW9uOiAnVHJpZ2dlcmVkIHdoZW4gYWxsIGpvYnMgYXJlIHByb2Nlc3NlZCAocXVldWUgZW1wdHkgYW5kIG5vIGFjdGl2ZSBleGVjdXRpb25zKScsXG4gICAgICBtZXRyaWM6IGpvYkNvbXBsZXRpb25FeHByLFxuICAgICAgdGhyZXNob2xkOiAxLFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDEsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fT1JfRVFVQUxfVE9fVEhSRVNIT0xELFxuICAgICAgdHJlYXRNaXNzaW5nRGF0YTogY3cuVHJlYXRNaXNzaW5nRGF0YS5OT1RfQlJFQUNISU5HXG4gICAgfSk7XG5cbiAgICBjb25zdCBhbGVydEFjdGlvbiA9IG5ldyBjd2FjdGlvbnMuU25zQWN0aW9uKGFsZXJ0c1RvcGljKTtcblxuICAgIGRscURlcHRoQWxhcm0uYWRkQWxhcm1BY3Rpb24oYWxlcnRBY3Rpb24pO1xuICAgIHByb2Nlc3NpbmdFcnJvcnNBbGFybS5hZGRBbGFybUFjdGlvbihhbGVydEFjdGlvbik7XG4gICAgcXVldWVEZXB0aEFsYXJtLmFkZEFsYXJtQWN0aW9uKGFsZXJ0QWN0aW9uKTtcbiAgICBqb2JDb21wbGV0aW9uQWxhcm0uYWRkQWxhcm1BY3Rpb24oYWxlcnRBY3Rpb24pO1xuICAgIHF1ZXVlQWdlQWxhcm0uYWRkQWxhcm1BY3Rpb24oYWxlcnRBY3Rpb24pO1xuXG4gICAgLy8gQ2xvdWRXYXRjaCBEYXNoYm9hcmRcbiAgICBjb25zdCBkYXNoYm9hcmQgPSBuZXcgY3cuRGFzaGJvYXJkKHRoaXMsICdQYXJhbGxlbFByb2Nlc3NpbmdEYXNoYm9hcmQnLCB7XG4gICAgICBkYXNoYm9hcmROYW1lOiAnQW1pcmFMYW1iZGFQYXJhbGxlbCdcbiAgICB9KTtcblxuICAgIGRhc2hib2FyZC5hZGRXaWRnZXRzKFxuICAgICAgbmV3IGN3LkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdTUVMgUXVldWUgTWV0cmljcycsXG4gICAgICAgIGxlZnQ6IFt0YXNrc1F1ZXVlLm1ldHJpY0FwcHJveGltYXRlTnVtYmVyT2ZNZXNzYWdlc1Zpc2libGUoKV0sXG4gICAgICAgIHJpZ2h0OiBbdGFza3NRdWV1ZS5tZXRyaWNBcHByb3hpbWF0ZUFnZU9mT2xkZXN0TWVzc2FnZSgpXSxcbiAgICAgICAgd2lkdGg6IDEyXG4gICAgICB9KSxcbiAgICAgIG5ldyBjdy5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnTGFtYmRhIFByb2Nlc3NpbmcgTWV0cmljcycsXG4gICAgICAgIGxlZnQ6IFtwcm9jZXNzaW5nTGFtYmRhLm1ldHJpY0ludm9jYXRpb25zKCksIHByb2Nlc3NpbmdMYW1iZGEubWV0cmljRHVyYXRpb24oKV0sXG4gICAgICAgIHJpZ2h0OiBbcHJvY2Vzc2luZ0xhbWJkYS5tZXRyaWNFcnJvcnMoKSwgcHJvY2Vzc2luZ0xhbWJkYS5tZXRyaWNUaHJvdHRsZXMoKV0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSksXG4gICAgICBuZXcgY3cuR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0xhbWJkYSBDb25jdXJyZW5jeScsXG4gICAgICAgIGxlZnQ6IFtjb25jdXJyZW50RXhlY3V0aW9uc01ldHJpY10sXG4gICAgICAgIHdpZHRoOiAyNFxuICAgICAgfSksXG4gICAgICBuZXcgY3cuR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0RMUSBEZXB0aCcsXG4gICAgICAgIGxlZnQ6IFtkbHEubWV0cmljQXBwcm94aW1hdGVOdW1iZXJPZk1lc3NhZ2VzVmlzaWJsZSgpXSxcbiAgICAgICAgd2lkdGg6IDEyXG4gICAgICB9KSxcbiAgICAgIG5ldyBjdy5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnSm9iIENvbXBsZXRpb24gVHJhY2tpbmcnLFxuICAgICAgICBsZWZ0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7XG4gICAgICAgICAgICBuYW1lc3BhY2U6ICdBbWlyYS9Kb2JzJyxcbiAgICAgICAgICAgIG1ldHJpY05hbWU6ICdKb2JzQ29tcGxldGVkJyxcbiAgICAgICAgICAgIHN0YXRpc3RpYzogJ1N1bSdcbiAgICAgICAgICB9KSxcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHtcbiAgICAgICAgICAgIG5hbWVzcGFjZTogJ0FtaXJhL0pvYnMnLFxuICAgICAgICAgICAgbWV0cmljTmFtZTogJ0pvYnNGYWlsZWQnLFxuICAgICAgICAgICAgc3RhdGlzdGljOiAnU3VtJ1xuICAgICAgICAgIH0pXG4gICAgICAgIF0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSksXG4gICAgICBuZXcgY3cuR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ1Byb2Nlc3NpbmdUaW1lIChtcyknLFxuICAgICAgICBsZWZ0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0FtaXJhL0pvYnMnLCBtZXRyaWNOYW1lOiAnUHJvY2Vzc2luZ1RpbWUnLCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KVxuICAgICAgICBdLFxuICAgICAgICByaWdodDogW1xuICAgICAgICAgIG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdBbWlyYS9Kb2JzJywgbWV0cmljTmFtZTogJ1Byb2Nlc3NpbmdUaW1lJywgc3RhdGlzdGljOiAncDk1JyB9KVxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3LkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdJbmZlcmVuY2UgVG90YWwgKHA5NSBtcyknLFxuICAgICAgICBsZWZ0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0FtaXJhL0luZmVyZW5jZScsIG1ldHJpY05hbWU6ICdJbmZlcmVuY2VUb3RhbE1zJywgc3RhdGlzdGljOiAncDk1JyB9KVxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3LkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdBY3Rpdml0eSBUb3RhbCAocDk1IG1zKScsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQW1pcmEvQWN0aXZpdHknLCBtZXRyaWNOYW1lOiAnQWN0aXZpdHlUb3RhbE1zJywgc3RhdGlzdGljOiAncDk1JyB9KVxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pXG4gICAgKTtcblxuICAgIC8vIE91dHB1dHNcbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnVGFza3NRdWV1ZVVybCcsIHtcbiAgICAgIHZhbHVlOiB0YXNrc1F1ZXVlLnF1ZXVlVXJsLFxuICAgICAgZGVzY3JpcHRpb246ICdTUVMgVGFza3MgUXVldWUgVVJMJ1xuICAgIH0pO1xuXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1Byb2Nlc3NpbmdMYW1iZGFBcm4nLCB7XG4gICAgICB2YWx1ZTogcHJvY2Vzc2luZ0xhbWJkYS5mdW5jdGlvbkFybixcbiAgICAgIGRlc2NyaXB0aW9uOiAnUHJvY2Vzc2luZyBMYW1iZGEgRnVuY3Rpb24gQVJOJ1xuICAgIH0pO1xuXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1Jlc3VsdHNCdWNrZXROYW1lJywge1xuICAgICAgdmFsdWU6IHJlc3VsdHNCdWNrZXQuYnVja2V0TmFtZSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnUzMgUmVzdWx0cyBCdWNrZXQgTmFtZSdcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdNYW51YWxUcmlnZ2VyRnVuY3Rpb25OYW1lJywge1xuICAgICAgdmFsdWU6IG1hbnVhbFRyaWdnZXJMYW1iZGEuZnVuY3Rpb25OYW1lLFxuICAgICAgZGVzY3JpcHRpb246ICdNYW51YWwgdHJpZ2dlciBMYW1iZGEgZnVuY3Rpb24gbmFtZSAodXNlIHdpdGggQVdTIENMSSknXG4gICAgfSk7XG5cbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnRGFzaGJvYXJkVXJsJywge1xuICAgICAgdmFsdWU6IGBodHRwczovLyR7dGhpcy5yZWdpb259LmNvbnNvbGUuYXdzLmFtYXpvbi5jb20vY2xvdWR3YXRjaC9ob21lP3JlZ2lvbj0ke3RoaXMucmVnaW9ufSNkYXNoYm9hcmRzOm5hbWU9JHtkYXNoYm9hcmQuZGFzaGJvYXJkTmFtZX1gLFxuICAgICAgZGVzY3JpcHRpb246ICdDbG91ZFdhdGNoIERhc2hib2FyZCBVUkwnXG4gICAgfSk7XG4gIH1cbn1cbiJdfQ==
