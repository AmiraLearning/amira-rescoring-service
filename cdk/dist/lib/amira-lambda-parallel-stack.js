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
        // Secrets Manager permissions for AppSync credentials
        processingLambda.addToRolePolicy(new iam.PolicyStatement({
            actions: [
                'secretsmanager:GetSecretValue'
            ],
            resources: [
                cdk.Arn.format({
                    service: 'secretsmanager',
                    resource: 'secret',
                    resourceName: 'amira/appsync/*'
                }, this)
            ]
        }));
        // Slack notification Lambda (defined early for enqueue lambda reference)
        const slackNotifierLambda = new lambda.Function(this, 'SlackNotifierFunction', {
            runtime: lambda.Runtime.PYTHON_3_12,
            handler: 'index.lambda_handler',
            code: lambda.Code.fromAsset('../lambda/slack_notifier'),
            timeout: cdk.Duration.seconds(30),
            tracing: lambda.Tracing.ACTIVE,
            environment: {
                SLACK_WEBHOOK_URL: slackWebhookParam.valueAsString,
                AUDIO_ENV: cdk.Token.asString(cdk.Fn.ref('AWS::NoValue')) // Will be set via environment at runtime
            }
        });
        const slackWebhookProvided = new cdk.CfnCondition(this, 'SlackWebhookProvided', {
            expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(slackWebhookParam.valueAsString, ''))
        });
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
                ATHENA_QUERY: athenaQueryParam.valueAsString,
                SLACK_NOTIFIER_FUNCTION_NAME: cdk.Token.asString(cdk.Fn.conditionIf(slackWebhookProvided.logicalId, slackNotifierLambda.functionName, '')),
                AUDIO_ENV: cdk.Token.asString(cdk.Fn.ref('AWS::NoValue')) // Will be set via environment at runtime
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
        // Allow enqueue Lambda to invoke Slack notifier for kickoff notifications
        const slackInvokePermission = new iam.PolicyStatement({
            actions: ['lambda:InvokeFunction'],
            resources: [slackNotifierLambda.functionArn]
        });
        const cfnSlackInvokePolicy = new iam.CfnPolicy(this, 'EnqueueSlackInvokePolicy', {
            policyDocument: new iam.PolicyDocument({ statements: [slackInvokePermission] }),
            roles: [enqueueLambda.role.roleName],
            policyName: `EnqueueSlackInvokePolicy-${cdk.Stack.of(this).stackName}`
        });
        cfnSlackInvokePolicy.cfnOptions.condition = slackWebhookProvided;
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
        // Subscribe Slack notifier to SNS alerts
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
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiYW1pcmEtbGFtYmRhLXBhcmFsbGVsLXN0YWNrLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsiLi4vLi4vbGliL2FtaXJhLWxhbWJkYS1wYXJhbGxlbC1zdGFjay50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOzs7QUFBQSxtQ0FBbUM7QUFDbkMsaURBQWlEO0FBRWpELDJDQUEyQztBQUMzQyx5Q0FBeUM7QUFDekMsMkNBQTJDO0FBQzNDLGlEQUFpRDtBQUNqRCwwREFBMEQ7QUFDMUQsZ0VBQWdFO0FBQ2hFLDJDQUEyQztBQUMzQyw2Q0FBNkM7QUFDN0MsMkNBQTJDO0FBQzNDLGlEQUFpRDtBQUNqRCwyQ0FBMkM7QUFFM0MsZ0VBQWdFO0FBQ2hFLG1EQUFtRDtBQUduRCxNQUFhLHdCQUF5QixTQUFRLEdBQUcsQ0FBQyxLQUFLO0lBQ3JELFlBQVksS0FBZ0IsRUFBRSxFQUFVLEVBQUUsS0FBc0I7UUFDOUQsS0FBSyxDQUFDLEtBQUssRUFBRSxFQUFFLEVBQUUsS0FBSyxDQUFDLENBQUM7UUFFeEIsK0JBQStCO1FBQy9CLE1BQU0sYUFBYSxHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsZ0JBQWdCLEVBQUU7WUFDakUsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsU0FBUztZQUNsQixXQUFXLEVBQUUsc0JBQXNCO1NBQ3BDLENBQUMsQ0FBQztRQUNILE1BQU0saUJBQWlCLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxjQUFjLEVBQUU7WUFDbkUsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsNEJBQTRCO1lBQ3JDLFdBQVcsRUFBRSw2QkFBNkI7U0FDM0MsQ0FBQyxDQUFDO1FBQ0gsTUFBTSxnQkFBZ0IsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLGFBQWEsRUFBRTtZQUNqRSxJQUFJLEVBQUUsUUFBUTtZQUNkLE9BQU8sRUFBRSwyREFBMkQ7WUFDcEUsV0FB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QVUsRUFBRSxpQkFBaUIsRUFBRSxTQUFTLEVBQUUsS0FBSyxFQUFFLENBQUM7YUFDaEc7WUFDRCxLQUFLLEVBQUUsRUFBRTtTQUNWLENBQUMsQ0FDSCxDQUFDO1FBRUYsVUFBVTtRQUNWLElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsZUFBZSxFQUFFO1lBQ3ZDLEtBQUssRUFBRSxVQUFVLENBQUMsUUFBUTtZQUMxQixXQUFXLEVBQUUscUJBQXFCO1NBQ25DLENBQUMsQ0FBQztRQUVILElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUscUJBQXFCLEVBQUU7WUFDN0MsS0FBSyxFQUFFLGdCQUFnQixDQUFDLFdBQVc7WUFDbkMsV0FBVyxFQUFFLGdDQUFnQztTQUM5QyxDQUFDLENBQUM7UUFFSCxJQUFJLEdBQUcsQ0FBQyxTQUFTLENBQUMsSUFBSSxFQUFFLG1CQUFtQixFQUFFO1lBQzNDLEtBQUssRUFBRSxhQUFhLENBQUMsVUFBVTtZQUMvQixXQUFXLEVBQUUsd0JBQXdCO1NBQ3RDLENBQUMsQ0FBQztRQUVILElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsMkJBQTJCLEVBQUU7WUFDbkQsS0FBSyxFQUFFLG1CQUFtQixDQUFDLFlBQVk7WUFDdkMsV0FBVyxFQUFFLHdEQUF3RDtTQUN0RSxDQUFDLENBQUM7UUFFSCxJQUFJLEdBQUcsQ0FBQyxTQUFTLENBQUMsSUFBSSxFQUFFLGNBQWMsRUFBRTtZQUN0QyxLQUFLLEVBQUUsV0FBVyxJQUFJLENBQUMsTUFBTSxrREFBa0QsSUFBSSxDQUFDLE1BQU0sb0JBQW9CLFNBQVMsQ0FBQyxhQUFhLEVBQUU7WUFDdkksV0FBVyxFQUFFLDBCQUEwQjtTQUN4QyxDQUFDLENBQUM7SUFDTCxDQUFDO0NBQ0Y7QUEzc0JELDREQTJzQkMiLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgKiBhcyBjZGsgZnJvbSAnYXdzLWNkay1saWInO1xuaW1wb3J0ICogYXMgbGFtYmRhIGZyb20gJ2F3cy1jZGstbGliL2F3cy1sYW1iZGEnO1xuaW1wb3J0ICogYXMgZWMyIGZyb20gJ2F3cy1jZGstbGliL2F3cy1lYzInO1xuaW1wb3J0ICogYXMgc3FzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zcXMnO1xuaW1wb3J0ICogYXMgczMgZnJvbSAnYXdzLWNkay1saWIvYXdzLXMzJztcbmltcG9ydCAqIGFzIGlhbSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtaWFtJztcbmltcG9ydCAqIGFzIGV2ZW50cyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZXZlbnRzJztcbmltcG9ydCAqIGFzIHRhcmdldHMgZnJvbSAnYXdzLWNkay1saWIvYXdzLWV2ZW50cy10YXJnZXRzJztcbmltcG9ydCAqIGFzIHNvdXJjZXMgZnJvbSAnYXdzLWNkay1saWIvYXdzLWxhbWJkYS1ldmVudC1zb3VyY2VzJztcbmltcG9ydCAqIGFzIGttcyBmcm9tICdhd3MtY2RrLWxpYi9hd3Mta21zJztcbmltcG9ydCAqIGFzIGxvZ3MgZnJvbSAnYXdzLWNkay1saWIvYXdzLWxvZ3MnO1xuaW1wb3J0ICogYXMgc3NtIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zc20nO1xuaW1wb3J0ICogYXMgY3cgZnJvbSAnYXdzLWNkay1saWIvYXdzLWNsb3Vkd2F0Y2gnO1xuaW1wb3J0ICogYXMgc25zIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zbnMnO1xuaW1wb3J0ICogYXMgc25zX3N1YnNjcmlwdGlvbnMgZnJvbSAnYXdzLWNkay1saWIvYXdzLXNucy1zdWJzY3JpcHRpb25zJztcbmltcG9ydCAqIGFzIGN3YWN0aW9ucyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtY2xvdWR3YXRjaC1hY3Rpb25zJztcbmltcG9ydCAqIGFzIGNyIGZyb20gJ2F3cy1jZGstbGliL2N1c3RvbS1yZXNvdXJjZXMnO1xuaW1wb3J0IHsgQ29uc3RydWN0IH0gZnJvbSAnY29uc3RydWN0cyc7XG5cbmV4cG9ydCBjbGFzcyBBbWlyYUxhbWJkYVBhcmFsbGVsU3RhY2sgZXh0ZW5kcyBjZGsuU3RhY2sge1xuICBjb25zdHJ1Y3RvcihzY29wZTogQ29uc3RydWN0LCBpZDogc3RyaW5nLCBwcm9wcz86IGNkay5TdGFja1Byb3BzKSB7XG4gICAgc3VwZXIoc2NvcGUsIGlkLCBwcm9wcyk7XG5cbiAgICAvLyBQYXJhbWV0ZXJzIGZvciBjb25maWd1cmF0aW9uXG4gICAgY29uc3QgYXRoZW5hRGJQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFEYXRhYmFzZScsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2RlZmF1bHQnLFxuICAgICAgZGVzY3JpcHRpb246ICdBdGhlbmEgZGF0YWJhc2UgbmFtZSdcbiAgICB9KTtcbiAgICBjb25zdCBhdGhlbmFPdXRwdXRQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFPdXRwdXQnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdzMzovL2F0aGVuYS1xdWVyeS1yZXN1bHRzLycsXG4gICAgICBkZXNjcmlwdGlvbjogJ0F0aGVuYSBxdWVyeSBvdXRwdXQgUzMgcGF0aCdcbiAgICB9KTtcbiAgICBjb25zdCBhdGhlbmFRdWVyeVBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYVF1ZXJ5Jywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnU0VMRUNUIGFjdGl2aXR5X2lkIEZST00gYWN0aXZpdGllcyBXSEVSRSBwcm9jZXNzX2ZsYWcgPSAxJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnQXRoZW5hIFNRTCB0byBwcm9kdWNlIGFjdGl2aXR5IElEcydcbiAgICB9KTtcbiAgICBjb25zdCBtb2RlbFBhdGhQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdNb2RlbFBhdGgnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdmYWNlYm9vay93YXYydmVjMi1iYXNlLTk2MGgnLFxuICAgICAgZGVzY3JpcHRpb246ICdIRiBtb2RlbCBwYXRoIGZvciBXYXYyVmVjMidcbiAgICB9KTtcbiAgICBjb25zdCByZXN1bHRzUHJlZml4UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnUmVzdWx0c1ByZWZpeCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3Jlc3VsdHMvJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnUzMga2V5IHByZWZpeCBmb3IgcmVzdWx0cyB3cml0ZXMnXG4gICAgfSk7XG4gICAgY29uc3QgYXVkaW9CdWNrZXROYW1lUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXVkaW9CdWNrZXROYW1lJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnT3B0aW9uYWwgUzMgYnVja2V0IG5hbWUgZm9yIGlucHV0IGF1ZGlvIChyZWFkLW9ubHkpLiBMZWF2ZSBibGFuayB0byBza2lwLidcbiAgICB9KTtcbiAgICBjb25zdCBzbGFja1dlYmhvb2tQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdTbGFja1dlYmhvb2tVcmwnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdTbGFjayB3ZWJob29rIFVSTCBmb3Igam9iIGNvbXBsZXRpb24gYW5kIGVycm9yIG5vdGlmaWNhdGlvbnMnLFxuICAgICAgbm9FY2hvOiB0cnVlXG4gICAgfSk7XG4gICAgY29uc3QgdHJpdG9uQ2x1c3RlclVybFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1RyaXRvbkNsdXN0ZXJVcmwnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBUcml0b24gR1BVIGNsdXN0ZXIgVVJMIGZvciByZW1vdGUgaW5mZXJlbmNlLiBMZWF2ZSBibGFuayB0byBhdXRvLXJlc29sdmUgZnJvbSBTU00gcGFyYW1ldGVyIC9hbWlyYS90cml0b25fYWxiX3VybC4nXG4gICAgfSk7XG5cbiAgICBjb25zdCBlbmFibGVUcml0b25QYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdFbmFibGVUcml0b24nLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdmYWxzZScsXG4gICAgICBhbGxvd2VkVmFsdWVzOiBbJ3RydWUnLCAnZmFsc2UnXSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRW5hYmxlIGNhbGxpbmcgYSBUcml0b24gY2x1c3RlciAoaW50ZXJuYWwgQUxCKSBmcm9tIHRoZSBwcm9jZXNzaW5nIExhbWJkYSdcbiAgICB9KTtcblxuICAgIC8vIE9wdGlvbmFsIFZQQyBhdHRhY2htZW50IGZvciBjYWxsaW5nIGludGVybmFsIEFMQiBkaXJlY3RseVxuICAgIGNvbnN0IHZwY0lkUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnVnBjSWQnLCB7XG4gICAgICB0eXBlOiAnQVdTOjpFQzI6OlZQQzo6SWQnLFxuICAgICAgZGVmYXVsdDogY2RrLkF3cy5OT19WQUxVRSBhcyBhbnksXG4gICAgICBkZXNjcmlwdGlvbjogJ1ZQQyBJRCB0byBhdHRhY2ggdGhlIHByb2Nlc3NpbmcgTGFtYmRhIHRvIChmb3IgaW50ZXJuYWwgQUxCIGFjY2VzcyknXG4gICAgfSk7XG4gICAgY29uc3QgcHJpdmF0ZVN1Ym5ldElkc0NzdlBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1ByaXZhdGVTdWJuZXRJZHNDc3YnLCB7XG4gICAgICB0eXBlOiAnTGlzdDxBV1M6OkVDMjo6U3VibmV0OjpJZD4nLFxuICAgICAgZGVzY3JpcHRpb246ICdQcml2YXRlIHN1Ym5ldCBJRHMgZm9yIHRoZSBMYW1iZGEgVlBDIGNvbmZpZydcbiAgICB9KTtcbiAgICBjb25zdCBsYW1iZGFTZWN1cml0eUdyb3VwSWRQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdMYW1iZGFTZWN1cml0eUdyb3VwSWQnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6IGNkay5Bd3MuTk9fVkFMVUUgYXMgYW55LFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBleGlzdGluZyBTZWN1cml0eSBHcm91cCBJRCBmb3IgdGhlIHByb2Nlc3NpbmcgTGFtYmRhJ1xuICAgIH0pO1xuICAgIGNvbnN0IHZwY1Byb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ1ZwY1Byb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHZwY0lkUGFyYW0udmFsdWVBc1N0cmluZywgJycpKVxuICAgIH0pO1xuICAgIGNvbnN0IGxhbWJkYVNnUHJvdmlkZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnTGFtYmRhU2dQcm92aWRlZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25Ob3QoY2RrLkZuLmNvbmRpdGlvbkVxdWFscyhsYW1iZGFTZWN1cml0eUdyb3VwSWRQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpXG4gICAgfSk7XG5cbiAgICAvLyBLTVMga2V5IGZvciBlbmNyeXB0aW9uXG4gICAgY29uc3Qga21zS2V5ID0gbmV3IGttcy5LZXkodGhpcywgJ0FtaXJhUGFyYWxsZWxLZXknLCB7XG4gICAgICBlbmFibGVLZXlSb3RhdGlvbjogdHJ1ZSxcbiAgICAgIGFsaWFzOiAnYWxpYXMvYW1pcmEtbGFtYmRhLXBhcmFsbGVsJ1xuICAgIH0pO1xuXG4gICAgLy8gUmVzdWx0cyBidWNrZXRcbiAgICBjb25zdCBhY2Nlc3NMb2dzQnVja2V0ID0gbmV3IHMzLkJ1Y2tldCh0aGlzLCAnTGFtYmRhQWNjZXNzTG9nc0J1Y2tldCcsIHtcbiAgICAgIHZlcnNpb25lZDogZmFsc2UsXG4gICAgICBibG9ja1B1YmxpY0FjY2VzczogczMuQmxvY2tQdWJsaWNBY2Nlc3MuQkxPQ0tfQUxMLFxuICAgICAgZW5jcnlwdGlvbjogczMuQnVja2V0RW5jcnlwdGlvbi5TM19NQU5BR0VELFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZSxcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNkay5SZW1vdmFsUG9saWN5LlJFVEFJTlxuICAgIH0pO1xuXG4gICAgY29uc3QgcmVzdWx0c0J1Y2tldCA9IG5ldyBzMy5CdWNrZXQodGhpcywgJ1Jlc3VsdHNCdWNrZXQnLCB7XG4gICAgICB2ZXJzaW9uZWQ6IGZhbHNlLFxuICAgICAgYmxvY2tQdWJsaWNBY2Nlc3M6IHMzLkJsb2NrUHVibGljQWNjZXNzLkJMT0NLX0FMTCxcbiAgICAgIGVuY3J5cHRpb246IHMzLkJ1Y2tldEVuY3J5cHRpb24uS01TLFxuICAgICAgZW5jcnlwdGlvbktleToga21zS2V5LFxuICAgICAgYnVja2V0S2V5RW5hYmxlZDogdHJ1ZSxcbiAgICAgIHNlcnZlckFjY2Vzc0xvZ3NCdWNrZXQ6IGFjY2Vzc0xvZ3NCdWNrZXQsXG4gICAgICBzZXJ2ZXJBY2Nlc3NMb2dzUHJlZml4OiAnczMtYWNjZXNzLWxvZ3MvJyxcbiAgICAgIGVuZm9yY2VTU0w6IHRydWUsXG4gICAgICBsaWZlY3ljbGVSdWxlczogW1xuICAgICAgICB7XG4gICAgICAgICAgaWQ6ICdJbnRlbGxpZ2VudFRpZXJpbmdOb3cnLFxuICAgICAgICAgIGVuYWJsZWQ6IHRydWUsXG4gICAgICAgICAgdHJhbnNpdGlvbnM6IFtcbiAgICAgICAgICAgIHsgc3RvcmFnZUNsYXNzOiBzMy5TdG9yYWdlQ2xhc3MuSU5URUxMSUdFTlRfVElFUklORywgdHJhbnNpdGlvbkFmdGVyOiBjZGsuRHVyYXRpb24uZGF5cygwKSB9XG4gICAgICAgICAgXVxuICAgICAgICB9LFxuICAgICAgICB7XG4gICAgICAgICAgaWQ6ICdUcmFuc2l0aW9uVG9JQTMwZCcsXG4gICAgICAgICAgZW5hYmxlZDogdHJ1ZSxcbiAgICAgICAgICB0cmFuc2l0aW9uczogW1xuICAgICAgICAgICAgeyBzdG9yYWdlQ2xhc3M6IHMzLlN0b3JhZ2VDbGFzcy5JTkZSRVFVRU5UX0FDQ0VTUywgdHJhbnNpdGlvbkFmdGVyOiBjZGsuRHVyYXRpb24uZGF5cygzMCkgfVxuICAgICAgICAgIF1cbiAgICAgICAgfSxcbiAgICAgICAge1xuICAgICAgICAgIGlkOiAnVHJhbnNpdGlvblRvR2xhY2llcjEyMGQnLFxuICAgICAgICAgIGVuYWJsZWQ6IHRydWUsXG4gICAgICAgICAgdHJhbnNpdGlvbnM6IFtcbiAgICAgICAgICAgIHsgc3RvcmFnZUNsYXNzOiBzMy5TdG9yYWdlQ2xhc3MuR0xBQ0lFUl9JTlNUQU5UX1JFVFJJRVZBTCwgdHJhbnNpdGlvbkFmdGVyOiBjZGsuRHVyYXRpb24uZGF5cygxMjApIH1cbiAgICAgICAgICBdXG4gICAgICAgIH1cbiAgICAgIF0sXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5SRVRBSU5cbiAgICB9KTtcbiAgICByZXN1bHRzQnVja2V0LmFkZFRvUmVzb3VyY2VQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgc2lkOiAnRGVueUluc2VjdXJlVHJhbnNwb3J0JyxcbiAgICAgIGVmZmVjdDogaWFtLkVmZmVjdC5ERU5ZLFxuICAgICAgcHJpbmNpcGFsczogW25ldyBpYW0uQW55UHJpbmNpcGFsKCldLFxuICAgICAgYWN0aW9uczogWydzMzoqJ10sXG4gICAgICByZXNvdXJjZXM6IFtyZXN1bHRzQnVja2V0LmJ1Y2tldEFybiwgYCR7cmVzdWx0c0J1Y2tldC5idWNrZXRBcm59LypgXSxcbiAgICAgIGNvbmRpdGlvbnM6IHsgQm9vbDogeyAnYXdzOlNlY3VyZVRyYW5zcG9ydCc6ICdmYWxzZScgfSB9XG4gICAgfSkpO1xuICAgIHJlc3VsdHNCdWNrZXQuYWRkVG9SZXNvdXJjZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBzaWQ6ICdEZW55VW5FbmNyeXB0ZWRPYmplY3RVcGxvYWRzJyxcbiAgICAgIGVmZmVjdDogaWFtLkVmZmVjdC5ERU5ZLFxuICAgICAgcHJpbmNpcGFsczogW25ldyBpYW0uQW55UHJpbmNpcGFsKCldLFxuICAgICAgYWN0aW9uczogWydzMzpQdXRPYmplY3QnXSxcbiAgICAgIHJlc291cmNlczogW2Ake3Jlc3VsdHNCdWNrZXQuYnVja2V0QXJufS8qYF0sXG4gICAgICBjb25kaXRpb25zOiB7IFN0cmluZ05vdEVxdWFsczogeyAnczM6eC1hbXotc2VydmVyLXNpZGUtZW5jcnlwdGlvbic6ICdhd3M6a21zJyB9IH1cbiAgICB9KSk7XG5cbiAgICAvLyBTUVMgRGVhZCBMZXR0ZXIgUXVldWVcbiAgICBjb25zdCBkbHEgPSBuZXcgc3FzLlF1ZXVlKHRoaXMsICdQcm9jZXNzaW5nRExRJywge1xuICAgICAgcmV0ZW50aW9uUGVyaW9kOiBjZGsuRHVyYXRpb24uZGF5cygxNCksXG4gICAgICBlbmNyeXB0aW9uOiBzcXMuUXVldWVFbmNyeXB0aW9uLktNUyxcbiAgICAgIGVuY3J5cHRpb25NYXN0ZXJLZXk6IGttc0tleSxcbiAgICAgIGVuZm9yY2VTU0w6IHRydWVcbiAgICB9KTtcblxuICAgIC8vIE1haW4gU1FTIHF1ZXVlIGZvciB0YXNrc1xuICAgIGNvbnN0IHRhc2tzUXVldWUgPSBuZXcgc3FzLlF1ZXVlKHRoaXMsICdUYXNrc1F1ZXVlJywge1xuICAgICAgdmlzaWJpbGl0eVRpbWVvdXQ6IGNkay5EdXJhdGlvbi5ob3VycygyKSxcbiAgICAgIGRlYWRMZXR0ZXJRdWV1ZTogeyBxdWV1ZTogZGxxLCBtYXhSZWNlaXZlQ291bnQ6IDMgfSxcbiAgICAgIGVuY3J5cHRpb246IHNxcy5RdWV1ZUVuY3J5cHRpb24uS01TLFxuICAgICAgZW5jcnlwdGlvbk1hc3RlcktleToga21zS2V5LFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZSxcbiAgICAgIHJlY2VpdmVNZXNzYWdlV2FpdFRpbWU6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDApLFxuICAgIH0pO1xuXG4gICAgLy8gQ2xvdWRXYXRjaCBMb2cgR3JvdXAgZm9yIExhbWJkYVxuICAgIGNvbnN0IGxvZ0dyb3VwID0gbmV3IGxvZ3MuTG9nR3JvdXAodGhpcywgJ1Byb2Nlc3NpbmdMb2dHcm91cCcsIHtcbiAgICAgIGxvZ0dyb3VwTmFtZTogJy9hd3MvbGFtYmRhL2FtaXJhLXBhcmFsbGVsLXByb2Nlc3NvcicsXG4gICAgICByZXRlbnRpb246IGxvZ3MuUmV0ZW50aW9uRGF5cy5PTkVfTU9OVEgsXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5ERVNUUk9ZLFxuICAgICAgZW5jcnlwdGlvbktleToga21zS2V5XG4gICAgfSk7XG5cbiAgICAvLyBDb25kaXRpb25zXG4gICAgY29uc3QgYXVkaW9Qcm92aWRlZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdBdWRpb0J1Y2tldFByb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGF1ZGlvQnVja2V0TmFtZVBhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSlcbiAgICB9KTtcbiAgICBjb25zdCB0cml0b25VcmxQcm92aWRlZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdUcml0b25VcmxQcm92aWRlZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25Ob3QoY2RrLkZuLmNvbmRpdGlvbkVxdWFscyh0cml0b25DbHVzdGVyVXJsUGFyYW0udmFsdWVBc1N0cmluZywgJycpKVxuICAgIH0pO1xuICAgIGNvbnN0IHVzZVRyaXRvbkNvbmQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnVXNlVHJpdG9uQ29uZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25FcXVhbHMoZW5hYmxlVHJpdG9uUGFyYW0udmFsdWVBc1N0cmluZywgJ3RydWUnKVxuICAgIH0pO1xuXG4gICAgLy8gQ3VzdG9tIHJlc291cmNlIHRvIGZhaWwgZmFzdCBpZiBTU00gL2FtaXJhL3RyaXRvbl9hbGJfdXJsIGlzIG1pc3Npbmcgd2hlbiBUcml0b24gVVJMIHBhcmFtIGlzIGJsYW5rXG4gICAgY29uc3Qgc3NtUGFyYW1OYW1lID0gJy9hbWlyYS90cml0b25fYWxiX3VybCc7XG4gICAgY29uc3Qgc3NtUGFyYW1Bcm4gPSBjZGsuQXJuLmZvcm1hdCh7IHNlcnZpY2U6ICdzc20nLCByZXNvdXJjZTogJ3BhcmFtZXRlcicsIHJlc291cmNlTmFtZTogJ2FtaXJhL3RyaXRvbl9hbGJfdXJsJyB9LCB0aGlzKTtcbiAgICBjb25zdCB0cml0b25VcmxTc21DaGVjayA9IG5ldyBjci5Bd3NDdXN0b21SZXNvdXJjZSh0aGlzLCAnVHJpdG9uVXJsU3NtQ2hlY2snLCB7XG4gICAgICBvbkNyZWF0ZToge1xuICAgICAgICBzZXJ2aWNlOiAnU1NNJyxcbiAgICAgICAgYWN0aW9uOiAnZ2V0UGFyYW1ldGVyJyxcbiAgICAgICAgcGFyYW1ldGVyczogeyBOYW1lOiBzc21QYXJhbU5hbWUgfSxcbiAgICAgICAgcGh5c2ljYWxSZXNvdXJjZUlkOiBjci5QaHlzaWNhbFJlc291cmNlSWQub2YoJ1RyaXRvblVybFNzbUNoZWNrJylcbiAgICAgIH0sXG4gICAgICBvblVwZGF0ZToge1xuICAgICAgICBzZXJ2aWNlOiAnU1NNJyxcbiAgICAgICAgYWN0aW9uOiAnZ2V0UGFyYW1ldGVyJyxcbiAgICAgICAgcGFyYW1ldGVyczogeyBOYW1lOiBzc21QYXJhbU5hbWUgfSxcbiAgICAgICAgcGh5c2ljYWxSZXNvdXJjZUlkOiBjci5QaHlzaWNhbFJlc291cmNlSWQub2YoJ1RyaXRvblVybFNzbUNoZWNrJylcbiAgICAgIH0sXG4gICAgICBwb2xpY3k6IGNyLkF3c0N1c3RvbVJlc291cmNlUG9saWN5LmZyb21TZGtDYWxscyh7IHJlc291cmNlczogW3NzbVBhcmFtQXJuXSB9KVxuICAgIH0pO1xuICAgIGNvbnN0IHNzbUNoZWNrQ29uZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdTc21DaGVja1doZW5Vc2luZ1RyaXRvbkFuZE5vVXJsJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbkFuZChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGVuYWJsZVRyaXRvblBhcmFtLnZhbHVlQXNTdHJpbmcsICd0cnVlJyksIGNkay5Gbi5jb25kaXRpb25FcXVhbHModHJpdG9uQ2x1c3RlclVybFBhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSlcbiAgICB9KTtcbiAgICBjb25zdCBjdXN0b21SZXNvdXJjZUNmbiA9IHRyaXRvblVybFNzbUNoZWNrLm5vZGUuZGVmYXVsdENoaWxkIGFzIGNkay5DZm5DdXN0b21SZXNvdXJjZTtcbiAgICBpZiAoY3VzdG9tUmVzb3VyY2VDZm4gJiYgY3VzdG9tUmVzb3VyY2VDZm4uY2ZuT3B0aW9ucykge1xuICAgICAgY3VzdG9tUmVzb3VyY2VDZm4uY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBzc21DaGVja0NvbmQ7XG4gICAgfVxuXG4gICAgLy8gUHJvY2Vzc2luZyBMYW1iZGEgZnVuY3Rpb24gYXMgRG9ja2VyIGltYWdlIChwcmUtY2FjaGVkIG1vZGVsKVxuICAgIGNvbnN0IHByb2Nlc3NpbmdMYW1iZGEgPSBuZXcgbGFtYmRhLkRvY2tlckltYWdlRnVuY3Rpb24odGhpcywgJ1Byb2Nlc3NpbmdGdW5jdGlvbicsIHtcbiAgICAgIGZ1bmN0aW9uTmFtZTogJ2FtaXJhLXBhcmFsbGVsLXByb2Nlc3NvcicsXG4gICAgICBjb2RlOiBsYW1iZGEuRG9ja2VySW1hZ2VDb2RlLmZyb21JbWFnZUFzc2V0KCcuLi9sYW1iZGEvcGFyYWxsZWxfcHJvY2Vzc29yJyksXG4gICAgICB0aW1lb3V0OiBjZGsuRHVyYXRpb24ubWludXRlcygxNSksXG4gICAgICBtZW1vcnlTaXplOiAxMDI0MCxcbiAgICAgIC8vIFJlbW92ZWQgcmVzZXJ2ZWQgY29uY3VycmVuY3kgdG8gYXZvaWQgdW5pbnRlbmRlZCB0aHJvdHRsaW5nIGR1cmluZyB0ZXN0c1xuICAgICAgZGVhZExldHRlclF1ZXVlOiBkbHEsXG4gICAgICB0cmFjaW5nOiBsYW1iZGEuVHJhY2luZy5BQ1RJVkUsXG4gICAgICBlbnZpcm9ubWVudDoge1xuICAgICAgICBSRVNVTFRTX0JVQ0tFVDogcmVzdWx0c0J1Y2tldC5idWNrZXROYW1lLFxuICAgICAgICBSRVNVTFRTX1BSRUZJWDogcmVzdWx0c1ByZWZpeFBhcmFtLnZhbHVlQXNTdHJpbmcsXG4gICAgICAgIE1PREVMX1BBVEg6ICcvb3B0L21vZGVscy93YXYydmVjMi1vcHRpbWl6ZWQnLFxuICAgICAgICBBVURJT19CVUNLRVQ6IGF1ZGlvQnVja2V0TmFtZVBhcmFtLnZhbHVlQXNTdHJpbmcsXG4gICAgICAgIEtNU19LRVlfSUQ6IGttc0tleS5rZXlJZCxcbiAgICAgICAgU0xBQ0tfV0VCSE9PS19VUkw6IHNsYWNrV2ViaG9va1BhcmFtLnZhbHVlQXNTdHJpbmcsXG4gICAgICAgIE1BWF9DT05DVVJSRU5DWTogJzEwJywgLy8gVHVuZWQgZm9yIGluaXRpYWwgYWxpZ25tZW50OyBhZGp1c3QgdmlhIGVudiBpZiBuZWVkZWRcbiAgICAgICAgQkFUQ0hfQUxMX1BIUkFTRVM6ICd0cnVlJyxcbiAgICAgICAgVVNFX0ZMT0FUMTY6ICd0cnVlJyxcbiAgICAgICAgSU5DTFVERV9DT05GSURFTkNFOiAndHJ1ZScsXG4gICAgICAgIFRFU1RfTU9ERTogJ2ZhbHNlJyxcbiAgICAgICAgVVNFX1RSSVRPTjogZW5hYmxlVHJpdG9uUGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgVFJJVE9OX1VSTDogY2RrLlRva2VuLmFzU3RyaW5nKFxuICAgICAgICAgIGNkay5Gbi5jb25kaXRpb25JZihcbiAgICAgICAgICAgIHVzZVRyaXRvbkNvbmQubG9naWNhbElkLFxuICAgICAgICAgICAgY2RrLkZuLmNvbmRpdGlvbklmKFxuICAgICAgICAgICAgICB0cml0b25VcmxQcm92aWRlZC5sb2dpY2FsSWQsXG4gICAgICAgICAgICAgIHRyaXRvbkNsdXN0ZXJVcmxQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICAgICAgICBzc20uU3RyaW5nUGFyYW1ldGVyLnZhbHVlRm9yU3RyaW5nUGFyYW1ldGVyKHRoaXMsICcvYW1pcmEvdHJpdG9uX2FsYl91cmwnKVxuICAgICAgICAgICAgKSxcbiAgICAgICAgICAgICcnXG4gICAgICAgICAgKVxuICAgICAgICApLFxuICAgICAgICBUUklUT05fTU9ERUw6ICd3MnYyJyxcbiAgICAgICAgUFlUSE9OT1BUSU1JWkU6ICcyJyxcbiAgICAgICAgVE9SQ0hfTlVNX1RIUkVBRFM6ICc2JyxcbiAgICAgICAgT01QX05VTV9USFJFQURTOiAnNicsXG4gICAgICAgIFRSQU5TRk9STUVSU19DQUNIRTogJy90bXAvbW9kZWxzJyxcbiAgICAgICAgSEZfSFVCX0NBQ0hFOiAnL3RtcC9oZl9jYWNoZSdcbiAgICAgIH1cbiAgICB9KTtcblxuICAgIC8vIFZQQyBjb25maWd1cmF0aW9uIGlzIGhhbmRsZWQgZGlyZWN0bHkgaW4gQ0ZOIHNpbmNlIENESyB2YWxpZGF0aW9uIGNvbmZsaWN0cyB3aXRoIGNvbmRpdGlvbmFsIHBhcmFtZXRlcnNcbiAgICBjb25zdCBzdWJuZXRzTGlzdCA9IHByaXZhdGVTdWJuZXRJZHNDc3ZQYXJhbS52YWx1ZUFzTGlzdDtcbiAgICBjb25zdCBjZm5GdW5jID0gcHJvY2Vzc2luZ0xhbWJkYS5ub2RlLmRlZmF1bHRDaGlsZCBhcyBsYW1iZGEuQ2ZuRnVuY3Rpb247XG4gICAgY2ZuRnVuYy52cGNDb25maWcgPSBjZGsuVG9rZW4uYXNBbnkoY2RrLkZuLmNvbmRpdGlvbklmKFxuICAgICAgdnBjUHJvdmlkZWQubG9naWNhbElkLFxuICAgICAge1xuICAgICAgICBTZWN1cml0eUdyb3VwSWRzOiBjZGsuVG9rZW4uYXNMaXN0KGNkay5Gbi5jb25kaXRpb25JZihsYW1iZGFTZ1Byb3ZpZGVkLmxvZ2ljYWxJZCwgW2xhbWJkYVNlY3VyaXR5R3JvdXBJZFBhcmFtLnZhbHVlQXNTdHJpbmddLCBjZGsuQXdzLk5PX1ZBTFVFIGFzIGFueSkpLFxuICAgICAgICBTdWJuZXRJZHM6IHN1Ym5ldHNMaXN0XG4gICAgICB9LFxuICAgICAgY2RrLkF3cy5OT19WQUxVRVxuICAgICkpO1xuXG4gICAgLy8gRW5mb3JjZTogaWYgVnBjSWQgaXMgcHJvdmlkZWQsIExhbWJkYVNlY3VyaXR5R3JvdXBJZCBtdXN0IGJlIHByb3ZpZGVkXG4gICAgbmV3IGNkay5DZm5SdWxlKHRoaXMsICdWcGNSZXF1aXJlc0xhbWJkYVNnJywge1xuICAgICAgcnVsZUNvbmRpdGlvbjogY2RrLkZuLmNvbmRpdGlvbkFuZChjZGsuRm4uY29uZGl0aW9uTm90KGNkay5Gbi5jb25kaXRpb25FcXVhbHModnBjSWRQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpLCBjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGVuYWJsZVRyaXRvblBhcmFtLnZhbHVlQXNTdHJpbmcsICd0cnVlJykpLFxuICAgICAgYXNzZXJ0aW9uczogW1xuICAgICAgICB7XG4gICAgICAgICAgYXNzZXJ0OiBjZGsuRm4uY29uZGl0aW9uTm90KGNkay5Gbi5jb25kaXRpb25FcXVhbHMobGFtYmRhU2VjdXJpdHlHcm91cElkUGFyYW0udmFsdWVBc1N0cmluZywgJycpKSxcbiAgICAgICAgICBhc3NlcnREZXNjcmlwdGlvbjogJ1doZW4gVnBjSWQgaXMgcHJvdmlkZWQsIExhbWJkYVNlY3VyaXR5R3JvdXBJZCBtdXN0IGFsc28gYmUgcHJvdmlkZWQuJ1xuICAgICAgICB9XG4gICAgICBdXG4gICAgfSk7XG5cbiAgICAvLyBTUVMgRXZlbnQgU291cmNlIGZvciBMYW1iZGFcbiAgICBjb25zdCBtYXhFdmVudFNvdXJjZUNvbmN1cnJlbmN5UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnTWF4RXZlbnRTb3VyY2VDb25jdXJyZW5jeScsIHtcbiAgICAgIHR5cGU6ICdOdW1iZXInLFxuICAgICAgZGVmYXVsdDogMTAsXG4gICAgICBkZXNjcmlwdGlvbjogJ1NRUyBldmVudCBzb3VyY2UgbWF4IGNvbmN1cnJlbmN5IGZvciB0aGUgcHJvY2Vzc2luZyBMYW1iZGEnXG4gICAgfSk7XG5cbiAgICBjb25zdCBldmVudFNvdXJjZSA9IG5ldyBzb3VyY2VzLlNxc0V2ZW50U291cmNlKHRhc2tzUXVldWUsIHtcbiAgICAgIGJhdGNoU2l6ZTogMSxcbiAgICAgIG1heENvbmN1cnJlbmN5OiBtYXhFdmVudFNvdXJjZUNvbmN1cnJlbmN5UGFyYW0udmFsdWVBc051bWJlcixcbiAgICAgIHJlcG9ydEJhdGNoSXRlbUZhaWx1cmVzOiB0cnVlLFxuICAgICAgbWF4QmF0Y2hpbmdXaW5kb3c6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDApLFxuICAgIH0pO1xuICAgIHByb2Nlc3NpbmdMYW1iZGEuYWRkRXZlbnRTb3VyY2UoZXZlbnRTb3VyY2UpO1xuXG4gICAgLy8gT3B0aW9uYWwgd2FybWluZyBydWxlXG4gICAgY29uc3QgZW5hYmxlV2FybWluZ1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0VuYWJsZVdhcm1pbmcnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdmYWxzZScsXG4gICAgICBhbGxvd2VkVmFsdWVzOiBbJ3RydWUnLCAnZmFsc2UnXSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRW5hYmxlIHBlcmlvZGljIHdhcm0gaW52b2NhdGlvbiB0byByZWR1Y2UgY29sZCBzdGFydHMnXG4gICAgfSk7XG4gICAgY29uc3Qgd2FybVJhdGVNaW51dGVzUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnV2FybVJhdGVNaW51dGVzJywge1xuICAgICAgdHlwZTogJ051bWJlcicsXG4gICAgICBkZWZhdWx0OiAxNSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnV2FybSBwaW5nIHJhdGUgaW4gbWludXRlcyB3aGVuIEVuYWJsZVdhcm1pbmc9dHJ1ZSdcbiAgICB9KTtcbiAgICBjb25zdCB3YXJtRW5hYmxlZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdXYXJtRW5hYmxlZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25FcXVhbHMoZW5hYmxlV2FybWluZ1BhcmFtLnZhbHVlQXNTdHJpbmcsICd0cnVlJylcbiAgICB9KTtcbiAgICBjb25zdCB3YXJtaW5nUnVsZSA9IG5ldyBldmVudHMuQ2ZuUnVsZSh0aGlzLCAnUHJvY2Vzc2luZ1dhcm1SdWxlJywge1xuICAgICAgc2NoZWR1bGVFeHByZXNzaW9uOiBjZGsuRm4uc3ViKCdyYXRlKCR7TWludXRlc30gbWludXRlcyknLCB7IE1pbnV0ZXM6IHdhcm1SYXRlTWludXRlc1BhcmFtLnZhbHVlQXNTdHJpbmcgfSksXG4gICAgICBzdGF0ZTogJ0VOQUJMRUQnLFxuICAgICAgdGFyZ2V0czogW1xuICAgICAgICB7XG4gICAgICAgICAgaWQ6ICdUYXJnZXQwJyxcbiAgICAgICAgICBhcm46IHByb2Nlc3NpbmdMYW1iZGEuZnVuY3Rpb25Bcm4sXG4gICAgICAgICAgaW5wdXQ6IEpTT04uc3RyaW5naWZ5KHsgd2FybTogdHJ1ZSB9KVxuICAgICAgICB9XG4gICAgICBdXG4gICAgfSk7XG4gICAgd2FybWluZ1J1bGUuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSB3YXJtRW5hYmxlZDtcbiAgICBjb25zdCB3YXJtUGVybWlzc2lvbiA9IG5ldyBsYW1iZGEuQ2ZuUGVybWlzc2lvbih0aGlzLCAnQWxsb3dFdmVudEJyaWRnZUludm9rZVdhcm0nLCB7XG4gICAgICBhY3Rpb246ICdsYW1iZGE6SW52b2tlRnVuY3Rpb24nLFxuICAgICAgZnVuY3Rpb25OYW1lOiBwcm9jZXNzaW5nTGFtYmRhLmZ1bmN0aW9uTmFtZSxcbiAgICAgIHByaW5jaXBhbDogJ2V2ZW50cy5hbWF6b25hd3MuY29tJyxcbiAgICAgIHNvdXJjZUFybjogd2FybWluZ1J1bGUuYXR0ckFyblxuICAgIH0pO1xuICAgIHdhcm1QZXJtaXNzaW9uLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gd2FybUVuYWJsZWQ7XG5cbiAgICBjb25zdCBhdWRpb1BvbGljeURvYyA9IG5ldyBpYW0uUG9saWN5RG9jdW1lbnQoe1xuICAgICAgc3RhdGVtZW50czogW1xuICAgICAgICBuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICAgICAgYWN0aW9uczogWydzMzpMaXN0QnVja2V0J10sXG4gICAgICAgICAgcmVzb3VyY2VzOiBbY2RrLkZuLnN1YignYXJuOmF3czpzMzo6OiR7QnVja2V0TmFtZX0nLCB7IEJ1Y2tldE5hbWU6IGF1ZGlvQnVja2V0TmFtZVBhcmFtLnZhbHVlQXNTdHJpbmcgfSldXG4gICAgICAgIH0pLFxuICAgICAgICBuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICAgICAgYWN0aW9uczogWydzMzpHZXRPYmplY3QnXSxcbiAgICAgICAgICByZXNvdXJjZXM6IFtjZGsuRm4uc3ViKCdhcm46YXdzOnMzOjo6JHtCdWNrZXROYW1lfS8qJywgeyBCdWNrZXROYW1lOiBhdWRpb0J1Y2tldE5hbWVQYXJhbS52YWx1ZUFzU3RyaW5nIH0pXVxuICAgICAgICB9KVxuICAgICAgXVxuICAgIH0pO1xuXG4gICAgY29uc3QgYXVkaW9DZm5Qb2xpY3kgPSBuZXcgaWFtLkNmblBvbGljeSh0aGlzLCAnUHJvY2Vzc2luZ0xhbWJkYUF1ZGlvUG9saWN5Jywge1xuICAgICAgcG9saWN5RG9jdW1lbnQ6IGF1ZGlvUG9saWN5RG9jLFxuICAgICAgcm9sZXM6IFtwcm9jZXNzaW5nTGFtYmRhLnJvbGUhLnJvbGVOYW1lXSxcbiAgICAgIHBvbGljeU5hbWU6IGBQcm9jZXNzaW5nTGFtYmRhQXVkaW9Qb2xpY3ktJHtjZGsuU3RhY2sub2YodGhpcykuc3RhY2tOYW1lfWBcbiAgICB9KTtcbiAgICBhdWRpb0NmblBvbGljeS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGF1ZGlvUHJvdmlkZWQ7XG5cbiAgICAvLyBSZXN1bHRzIGJ1Y2tldCBhbmQgS01TIHBlcm1pc3Npb25zXG4gICAgcmVzdWx0c0J1Y2tldC5ncmFudFdyaXRlKHByb2Nlc3NpbmdMYW1iZGEpO1xuICAgIGttc0tleS5ncmFudEVuY3J5cHREZWNyeXB0KHByb2Nlc3NpbmdMYW1iZGEpO1xuXG4gICAgLy8gQ2xvdWRXYXRjaCBtZXRyaWNzIHBlcm1pc3Npb25zIGZvciBqb2IgdHJhY2tpbmdcbiAgICBwcm9jZXNzaW5nTGFtYmRhLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbXG4gICAgICAgICdjbG91ZHdhdGNoOlB1dE1ldHJpY0RhdGEnXG4gICAgICBdLFxuICAgICAgcmVzb3VyY2VzOiBbJyonXVxuICAgIH0pKTtcblxuICAgIC8vIFNlY3JldHMgTWFuYWdlciBwZXJtaXNzaW9ucyBmb3IgQXBwU3luYyBjcmVkZW50aWFsc1xuICAgIHByb2Nlc3NpbmdMYW1iZGEuYWRkVG9Sb2xlUG9saWN5KG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgIGFjdGlvbnM6IFtcbiAgICAgICAgJ3NlY3JldHNtYW5hZ2VyOkdldFNlY3JldFZhbHVlJ1xuICAgICAgXSxcbiAgICAgIHJlc291cmNlczogW1xuICAgICAgICBjZGsuQXJuLmZvcm1hdCh7XG4gICAgICAgICAgc2VydmljZTogJ3NlY3JldHNtYW5hZ2VyJyxcbiAgICAgICAgICByZXNvdXJjZTogJ3NlY3JldCcsXG4gICAgICAgICAgcmVzb3VyY2VOYW1lOiAnYW1pcmEvYXBwc3luYy8qJ1xuICAgICAgICB9LCB0aGlzKVxuICAgICAgXVxuICAgIH0pKTtcblxuICAgIC8vIFNsYWNrIG5vdGlmaWNhdGlvbiBMYW1iZGEgKGRlZmluZWQgZWFybHkgZm9yIGVucXVldWUgbGFtYmRhIHJlZmVyZW5jZSlcbiAgICBjb25zdCBzbGFja05vdGlmaWVyTGFtYmRhID0gbmV3IGxhbWJkYS5GdW5jdGlvbih0aGlzLCAnU2xhY2tOb3RpZmllckZ1bmN0aW9uJywge1xuICAgICAgcnVudGltZTogbGFtYmRhLlJ1bnRpbWUuUFlUSE9OXzNfMTIsXG4gICAgICBoYW5kbGVyOiAnaW5kZXgubGFtYmRhX2hhbmRsZXInLFxuICAgICAgY29kZTogbGFtYmRhLkNvZGUuZnJvbUFzc2V0KCcuLi9sYW1iZGEvc2xhY2tfbm90aWZpZXInKSxcbiAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDMwKSxcbiAgICAgIHRyYWNpbmc6IGxhbWJkYS5UcmFjaW5nLkFDVElWRSxcbiAgICAgIGVudmlyb25tZW50OiB7XG4gICAgICAgIFNMQUNLX1dFQkhPT0tfVVJMOiBzbGFja1dlYmhvb2tQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBBVURJT19FTlY6IGNkay5Ub2tlbi5hc1N0cmluZyhjZGsuRm4ucmVmKCdBV1M6Ok5vVmFsdWUnKSkgLy8gV2lsbCBiZSBzZXQgdmlhIGVudmlyb25tZW50IGF0IHJ1bnRpbWVcbiAgICAgIH1cbiAgICB9KTtcblxuICAgIGNvbnN0IHNsYWNrV2ViaG9va1Byb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ1NsYWNrV2ViaG9va1Byb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHNsYWNrV2ViaG9va1BhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSlcbiAgICB9KTtcblxuICAgIC8vIEVucXVldWUgTGFtYmRhIGZ1bmN0aW9uXG4gICAgY29uc3QgZW5xdWV1ZUxhbWJkYSA9IG5ldyBsYW1iZGEuRnVuY3Rpb24odGhpcywgJ0VucXVldWVGdW5jdGlvbicsIHtcbiAgICAgIHJ1bnRpbWU6IGxhbWJkYS5SdW50aW1lLlBZVEhPTl8zXzEyLFxuICAgICAgaGFuZGxlcjogJ2luZGV4LmxhbWJkYV9oYW5kbGVyJyxcbiAgICAgIGNvZGU6IGxhbWJkYS5Db2RlLmZyb21Bc3NldCgnLi4vbGFtYmRhL2VucXVldWVfam9icycpLFxuICAgICAgdGltZW91dDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoNSksXG4gICAgICB0cmFjaW5nOiBsYW1iZGEuVHJhY2luZy5BQ1RJVkUsXG4gICAgICBlbnZpcm9ubWVudDoge1xuICAgICAgICBKT0JTX1FVRVVFX1VSTDogdGFza3NRdWV1ZS5xdWV1ZVVybCxcbiAgICAgICAgQVRIRU5BX0RBVEFCQVNFOiBhdGhlbmFEYlBhcmFtLnZhbHVlQXNTdHJpbmcsXG4gICAgICAgIEFUSEVOQV9PVVRQVVQ6IGF0aGVuYU91dHB1dFBhcmFtLnZhbHVlQXNTdHJpbmcsXG4gICAgICAgIEFUSEVOQV9RVUVSWTogYXRoZW5hUXVlcnlQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBTTEFDS19OT1RJRklFUl9GVU5DVElPTl9OQU1FOiBjZGsuVG9rZW4uYXNTdHJpbmcoY2RrLkZuLmNvbmRpdGlvbklmKFxuICAgICAgICAgIHNsYWNrV2ViaG9va1Byb3ZpZGVkLmxvZ2ljYWxJZCxcbiAgICAgICAgICBzbGFja05vdGlmaWVyTGFtYmRhLmZ1bmN0aW9uTmFtZSxcbiAgICAgICAgICAnJ1xuICAgICAgICApKSxcbiAgICAgICAgQVVESU9fRU5WOiBjZGsuVG9rZW4uYXNTdHJpbmcoY2RrLkZuLnJlZignQVdTOjpOb1ZhbHVlJykpIC8vIFdpbGwgYmUgc2V0IHZpYSBlbnZpcm9ubWVudCBhdCBydW50aW1lXG4gICAgICB9XG4gICAgfSk7XG5cbiAgICAvLyBJQU0gcGVybWlzc2lvbnMgZm9yIGVucXVldWUgTGFtYmRhXG4gICAgY29uc3QgYXRoZW5hV29ya2dyb3VwQXJuID0gY2RrLkFybi5mb3JtYXQoe1xuICAgICAgc2VydmljZTogJ2F0aGVuYScsXG4gICAgICByZXNvdXJjZTogJ3dvcmtncm91cCcsXG4gICAgICByZXNvdXJjZU5hbWU6ICdwcmltYXJ5J1xuICAgIH0sIHRoaXMpO1xuICAgIGNvbnN0IGdsdWVEYkFybiA9IGNkay5Bcm4uZm9ybWF0KHtcbiAgICAgIHNlcnZpY2U6ICdnbHVlJyxcbiAgICAgIHJlc291cmNlOiAnZGF0YWJhc2UnLFxuICAgICAgcmVzb3VyY2VOYW1lOiBhdGhlbmFEYlBhcmFtLnZhbHVlQXNTdHJpbmdcbiAgICB9LCB0aGlzKTtcbiAgICBjb25zdCBnbHVlVGFibGVXaWxkY2FyZEFybiA9IGNkay5Bcm4uZm9ybWF0KHtcbiAgICAgIHNlcnZpY2U6ICdnbHVlJyxcbiAgICAgIHJlc291cmNlOiAndGFibGUnLFxuICAgICAgcmVzb3VyY2VOYW1lOiBgJHthdGhlbmFEYlBhcmFtLnZhbHVlQXNTdHJpbmd9LypgXG4gICAgfSwgdGhpcyk7XG5cbiAgICBlbnF1ZXVlTGFtYmRhLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbJ2F0aGVuYTpTdGFydFF1ZXJ5RXhlY3V0aW9uJywgJ2F0aGVuYTpHZXRRdWVyeUV4ZWN1dGlvbicsICdhdGhlbmE6R2V0UXVlcnlSZXN1bHRzJ10sXG4gICAgICByZXNvdXJjZXM6IFthdGhlbmFXb3JrZ3JvdXBBcm5dXG4gICAgfSkpO1xuICAgIGVucXVldWVMYW1iZGEuYWRkVG9Sb2xlUG9saWN5KG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgIGFjdGlvbnM6IFsnZ2x1ZTpHZXREYXRhYmFzZSddLFxuICAgICAgcmVzb3VyY2VzOiBbZ2x1ZURiQXJuXVxuICAgIH0pKTtcbiAgICBlbnF1ZXVlTGFtYmRhLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbJ2dsdWU6R2V0VGFibGUnXSxcbiAgICAgIHJlc291cmNlczogW2dsdWVUYWJsZVdpbGRjYXJkQXJuXVxuICAgIH0pKTtcblxuICAgIC8vIEF0aGVuYSBvdXRwdXQgYnVja2V0IHBlcm1pc3Npb25zXG4gICAgY29uc3QgYXRoZW5hT3V0cHV0UGFyc2VkID0gY2RrLkZuLnNwbGl0KCcvJywgYXRoZW5hT3V0cHV0UGFyYW0udmFsdWVBc1N0cmluZyk7XG4gICAgY29uc3QgYXRoZW5hT3V0cHV0QnVja2V0ID0gY2RrLkZuLnNlbGVjdCgyLCBhdGhlbmFPdXRwdXRQYXJzZWQpO1xuICAgIGVucXVldWVMYW1iZGEuYWRkVG9Sb2xlUG9saWN5KG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgIGFjdGlvbnM6IFsnczM6TGlzdEJ1Y2tldCddLFxuICAgICAgcmVzb3VyY2VzOiBbY2RrLkFybi5mb3JtYXQoeyBzZXJ2aWNlOiAnczMnLCByZXNvdXJjZTogYXRoZW5hT3V0cHV0QnVja2V0IH0sIHRoaXMpXVxuICAgIH0pKTtcbiAgICBlbnF1ZXVlTGFtYmRhLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbJ3MzOkdldE9iamVjdCcsICdzMzpQdXRPYmplY3QnXSxcbiAgICAgIHJlc291cmNlczogW2Nkay5Bcm4uZm9ybWF0KHsgc2VydmljZTogJ3MzJywgcmVzb3VyY2U6IGAke2F0aGVuYU91dHB1dEJ1Y2tldH0vKmAgfSwgdGhpcyldXG4gICAgfSkpO1xuXG4gICAgdGFza3NRdWV1ZS5ncmFudFNlbmRNZXNzYWdlcyhlbnF1ZXVlTGFtYmRhKTtcblxuICAgIC8vIEFsbG93IGVucXVldWUgTGFtYmRhIHRvIGludm9rZSBTbGFjayBub3RpZmllciBmb3Iga2lja29mZiBub3RpZmljYXRpb25zXG4gICAgY29uc3Qgc2xhY2tJbnZva2VQZXJtaXNzaW9uID0gbmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgYWN0aW9uczogWydsYW1iZGE6SW52b2tlRnVuY3Rpb24nXSxcbiAgICAgIHJlc291cmNlczogW3NsYWNrTm90aWZpZXJMYW1iZGEuZnVuY3Rpb25Bcm5dXG4gICAgfSk7XG4gICAgY29uc3QgY2ZuU2xhY2tJbnZva2VQb2xpY3kgPSBuZXcgaWFtLkNmblBvbGljeSh0aGlzLCAnRW5xdWV1ZVNsYWNrSW52b2tlUG9saWN5Jywge1xuICAgICAgcG9saWN5RG9jdW1lbnQ6IG5ldyBpYW0uUG9saWN5RG9jdW1lbnQoeyBzdGF0ZW1lbnRzOiBbc2xhY2tJbnZva2VQZXJtaXNzaW9uXSB9KSxcbiAgICAgIHJvbGVzOiBbZW5xdWV1ZUxhbWJkYS5yb2xlIS5yb2xlTmFtZV0sXG4gICAgICBwb2xpY3lOYW1lOiBgRW5xdWV1ZVNsYWNrSW52b2tlUG9saWN5LSR7Y2RrLlN0YWNrLm9mKHRoaXMpLnN0YWNrTmFtZX1gXG4gICAgfSk7XG4gICAgY2ZuU2xhY2tJbnZva2VQb2xpY3kuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBzbGFja1dlYmhvb2tQcm92aWRlZDtcblxuICAgIC8vIFNjaGVkdWxlIGZvciBhdXRvbWF0aWMgZW5xdWV1ZWluZ1xuICAgIGNvbnN0IHNjaGVkdWxlUnVsZSA9IG5ldyBldmVudHMuUnVsZSh0aGlzLCAnU2NoZWR1bGVSdWxlJywge1xuICAgICAgZGVzY3JpcHRpb246ICdUcmlnZ2VyIHBhcmFsbGVsIHByb2Nlc3NpbmcgcGlwZWxpbmUnLFxuICAgICAgc2NoZWR1bGU6IGV2ZW50cy5TY2hlZHVsZS5jcm9uKHsgbWludXRlOiAnMCcsIGhvdXI6ICcyJyB9KVxuICAgIH0pO1xuICAgIHNjaGVkdWxlUnVsZS5hZGRUYXJnZXQobmV3IHRhcmdldHMuTGFtYmRhRnVuY3Rpb24oZW5xdWV1ZUxhbWJkYSkpO1xuXG4gICAgLy8gT3B0aW9uYWwgQXRoZW5hIHN0YWdpbmcgY2xlYW51cCBMYW1iZGEgKyBzY2hlZHVsZVxuICAgIGNvbnN0IGVuYWJsZUF0aGVuYUNsZWFudXBQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdFbmFibGVBdGhlbmFDbGVhbnVwJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnZmFsc2UnLFxuICAgICAgYWxsb3dlZFZhbHVlczogWyd0cnVlJywgJ2ZhbHNlJ10sXG4gICAgICBkZXNjcmlwdGlvbjogJ0VuYWJsZSBzY2hlZHVsZWQgQXRoZW5hIHN0YWdpbmcgY2xlYW51cCAob3B0aW9uYWwpJ1xuICAgIH0pO1xuICAgIGNvbnN0IGF0aGVuYUNsZWFudXBCdWNrZXRQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFDbGVhbnVwQnVja2V0Jywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnUzMgYnVja2V0IGZvciBBdGhlbmEgc3RhZ2luZyByZXN1bHRzJ1xuICAgIH0pO1xuICAgIGNvbnN0IGF0aGVuYUNsZWFudXBQcmVmaXhQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFDbGVhbnVwUHJlZml4Jywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnYXRoZW5hX3N0YWdpbmcnLFxuICAgICAgZGVzY3JpcHRpb246ICdTMyBwcmVmaXggZm9yIEF0aGVuYSBzdGFnaW5nIHJlc3VsdHMnXG4gICAgfSk7XG4gICAgY29uc3QgYXRoZW5hQ2xlYW51cEFnZURheXNQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFDbGVhbnVwQWdlRGF5cycsIHtcbiAgICAgIHR5cGU6ICdOdW1iZXInLFxuICAgICAgZGVmYXVsdDogNyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRGVsZXRlIHN0YWdpbmcgb2JqZWN0cyBvbGRlciB0aGFuIE4gZGF5cydcbiAgICB9KTtcbiAgICBjb25zdCBjbGVhbnVwRW5hYmxlZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdBdGhlbmFDbGVhbnVwRW5hYmxlZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25FcXVhbHMoZW5hYmxlQXRoZW5hQ2xlYW51cFBhcmFtLnZhbHVlQXNTdHJpbmcsICd0cnVlJylcbiAgICB9KTtcblxuICAgIGNvbnN0IGNsZWFudXBMYW1iZGEgPSBuZXcgbGFtYmRhLkZ1bmN0aW9uKHRoaXMsICdBdGhlbmFTdGFnaW5nQ2xlYW51cCcsIHtcbiAgICAgIHJ1bnRpbWU6IGxhbWJkYS5SdW50aW1lLlBZVEhPTl8zXzEyLFxuICAgICAgaGFuZGxlcjogJ2F0aGVuYV9zdGFnaW5nX2NsZWFudXAubWFpbicsXG4gICAgICBjb2RlOiBsYW1iZGEuQ29kZS5mcm9tQXNzZXQoJy4uL3NjcmlwdHMnKSxcbiAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5taW51dGVzKDUpXG4gICAgfSk7XG4gICAgKGNsZWFudXBMYW1iZGEubm9kZS5kZWZhdWx0Q2hpbGQgYXMgbGFtYmRhLkNmbkZ1bmN0aW9uKS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGNsZWFudXBFbmFibGVkO1xuXG4gICAgY2xlYW51cExhbWJkYS5hZGRUb1JvbGVQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgYWN0aW9uczogWydzMzpMaXN0QnVja2V0JywgJ3MzOkRlbGV0ZU9iamVjdCcsICdzMzpEZWxldGVPYmplY3RWZXJzaW9uJ10sXG4gICAgICByZXNvdXJjZXM6IFtcbiAgICAgICAgY2RrLkFybi5mb3JtYXQoeyBzZXJ2aWNlOiAnczMnLCByZXNvdXJjZTogYXRoZW5hQ2xlYW51cEJ1Y2tldFBhcmFtLnZhbHVlQXNTdHJpbmcgfSwgdGhpcyksXG4gICAgICAgIGNkay5Bcm4uZm9ybWF0KHsgc2VydmljZTogJ3MzJywgcmVzb3VyY2U6IGAke2F0aGVuYUNsZWFudXBCdWNrZXRQYXJhbS52YWx1ZUFzU3RyaW5nfS8qYCB9LCB0aGlzKVxuICAgICAgXVxuICAgIH0pKTtcblxuICAgIGNvbnN0IGNsZWFudXBSdWxlID0gbmV3IGV2ZW50cy5SdWxlKHRoaXMsICdBdGhlbmFDbGVhbnVwU2NoZWR1bGUnLCB7XG4gICAgICBkZXNjcmlwdGlvbjogJ1NjaGVkdWxlZCBBdGhlbmEgc3RhZ2luZyBjbGVhbnVwJyxcbiAgICAgIHNjaGVkdWxlOiBldmVudHMuU2NoZWR1bGUuY3Jvbih7IG1pbnV0ZTogJzAnLCBob3VyOiAnMycgfSlcbiAgICB9KTtcbiAgICAoY2xlYW51cFJ1bGUubm9kZS5kZWZhdWx0Q2hpbGQgYXMgZXZlbnRzLkNmblJ1bGUpLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gY2xlYW51cEVuYWJsZWQ7XG4gICAgY2xlYW51cFJ1bGUuYWRkVGFyZ2V0KG5ldyB0YXJnZXRzLkxhbWJkYUZ1bmN0aW9uKGNsZWFudXBMYW1iZGEsIHtcbiAgICAgIGV2ZW50OiBldmVudHMuUnVsZVRhcmdldElucHV0LmZyb21PYmplY3Qoe1xuICAgICAgICBidWNrZXQ6IGF0aGVuYUNsZWFudXBCdWNrZXRQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBwcmVmaXg6IGF0aGVuYUNsZWFudXBQcmVmaXhQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBhZ2VfZGF5czogYXRoZW5hQ2xlYW51cEFnZURheXNQYXJhbS52YWx1ZUFzTnVtYmVyLFxuICAgICAgfSlcbiAgICB9KSk7XG5cbiAgICAvLyBNYW51YWwgdHJpZ2dlciBMYW1iZGFcbiAgICBjb25zdCBtYW51YWxUcmlnZ2VyTGFtYmRhID0gbmV3IGxhbWJkYS5GdW5jdGlvbih0aGlzLCAnTWFudWFsVHJpZ2dlckZ1bmN0aW9uJywge1xuICAgICAgcnVudGltZTogbGFtYmRhLlJ1bnRpbWUuUFlUSE9OXzNfMTIsXG4gICAgICBoYW5kbGVyOiAnaW5kZXgubGFtYmRhX2hhbmRsZXInLFxuICAgICAgY29kZTogbGFtYmRhLkNvZGUuZnJvbUFzc2V0KCcuLi9sYW1iZGEvbWFudWFsX2VucXVldWUnKSxcbiAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5taW51dGVzKDEpLFxuICAgICAgdHJhY2luZzogbGFtYmRhLlRyYWNpbmcuQUNUSVZFLFxuICAgICAgZW52aXJvbm1lbnQ6IHsgSk9CU19RVUVVRV9VUkw6IHRhc2tzUXVldWUucXVldWVVcmwgfVxuICAgIH0pO1xuICAgIHRhc2tzUXVldWUuZ3JhbnRTZW5kTWVzc2FnZXMobWFudWFsVHJpZ2dlckxhbWJkYSk7XG5cbiAgICAvLyBTTlMgdG9waWMgZm9yIGFsZXJ0c1xuICAgIGNvbnN0IGFsZXJ0c1RvcGljID0gbmV3IHNucy5Ub3BpYyh0aGlzLCAnQWxlcnRzVG9waWMnLCB7XG4gICAgICBkaXNwbGF5TmFtZTogJ0FtaXJhIExhbWJkYSBQYXJhbGxlbCBBbGVydHMnXG4gICAgfSk7XG5cbiAgICAvLyBTdWJzY3JpYmUgU2xhY2sgbm90aWZpZXIgdG8gU05TIGFsZXJ0c1xuXG4gICAgLy8gTGFtYmRhIHBlcm1pc3Npb24gZm9yIFNOUyB0byBpbnZva2UgU2xhY2sgbm90aWZpZXJcbiAgICBzbGFja05vdGlmaWVyTGFtYmRhLmFkZFBlcm1pc3Npb24oJ0FsbG93U05TSW52b2tlJywge1xuICAgICAgcHJpbmNpcGFsOiBuZXcgaWFtLlNlcnZpY2VQcmluY2lwYWwoJ3Nucy5hbWF6b25hd3MuY29tJyksXG4gICAgICBzb3VyY2VBcm46IGFsZXJ0c1RvcGljLnRvcGljQXJuXG4gICAgfSk7XG5cbiAgICAvLyBDb25kaXRpb25hbCBTTlMgc3Vic2NyaXB0aW9uIHRvIFNsYWNrIG5vdGlmaWVyXG4gICAgY29uc3Qgc2xhY2tTdWJzY3JpcHRpb24gPSBuZXcgc25zLkNmblN1YnNjcmlwdGlvbih0aGlzLCAnU2xhY2tOb3RpZmllclN1YnNjcmlwdGlvbicsIHtcbiAgICAgIHRvcGljQXJuOiBhbGVydHNUb3BpYy50b3BpY0FybixcbiAgICAgIHByb3RvY29sOiAnbGFtYmRhJyxcbiAgICAgIGVuZHBvaW50OiBzbGFja05vdGlmaWVyTGFtYmRhLmZ1bmN0aW9uQXJuXG4gICAgfSk7XG4gICAgc2xhY2tTdWJzY3JpcHRpb24uY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBzbGFja1dlYmhvb2tQcm92aWRlZDtcblxuICAgIC8vIENsb3VkV2F0Y2ggQWxhcm1zXG4gICAgY29uc3QgZGxxRGVwdGhBbGFybSA9IG5ldyBjdy5BbGFybSh0aGlzLCAnRExRRGVwdGhBbGFybScsIHtcbiAgICAgIG1ldHJpYzogZGxxLm1ldHJpY0FwcHJveGltYXRlTnVtYmVyT2ZNZXNzYWdlc1Zpc2libGUoKSxcbiAgICAgIHRocmVzaG9sZDogMSxcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAxLFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuR1JFQVRFUl9USEFOX09SX0VRVUFMX1RPX1RIUkVTSE9MRFxuICAgIH0pO1xuXG4gICAgY29uc3QgcHJvY2Vzc2luZ0Vycm9yc0FsYXJtID0gbmV3IGN3LkFsYXJtKHRoaXMsICdQcm9jZXNzaW5nRXJyb3JzQWxhcm0nLCB7XG4gICAgICBtZXRyaWM6IHByb2Nlc3NpbmdMYW1iZGEubWV0cmljRXJyb3JzKCksXG4gICAgICB0aHJlc2hvbGQ6IDEwLFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDIsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fVEhSRVNIT0xEXG4gICAgfSk7XG5cbiAgICBjb25zdCBxdWV1ZURlcHRoQWxhcm0gPSBuZXcgY3cuQWxhcm0odGhpcywgJ1F1ZXVlRGVwdGhBbGFybScsIHtcbiAgICAgIG1ldHJpYzogdGFza3NRdWV1ZS5tZXRyaWNBcHByb3hpbWF0ZU51bWJlck9mTWVzc2FnZXNWaXNpYmxlKCksXG4gICAgICB0aHJlc2hvbGQ6IDEwMDAsXG4gICAgICBldmFsdWF0aW9uUGVyaW9kczogMyxcbiAgICAgIGNvbXBhcmlzb25PcGVyYXRvcjogY3cuQ29tcGFyaXNvbk9wZXJhdG9yLkdSRUFURVJfVEhBTl9USFJFU0hPTERcbiAgICB9KTtcblxuICAgIGNvbnN0IHF1ZXVlQWdlQWxhcm0gPSBuZXcgY3cuQWxhcm0odGhpcywgJ1F1ZXVlQWdlQWxhcm0nLCB7XG4gICAgICBtZXRyaWM6IHRhc2tzUXVldWUubWV0cmljQXBwcm94aW1hdGVBZ2VPZk9sZGVzdE1lc3NhZ2UoKSxcbiAgICAgIHRocmVzaG9sZDogMzAwLFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDMsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fVEhSRVNIT0xEXG4gICAgfSk7XG5cbiAgICAvLyBKb2IgY29tcGxldGlvbiBkZXRlY3Rpb24gLSBxdWV1ZSBlbXB0eSBBTkQgbm8gYWN0aXZlIExhbWJkYSBleGVjdXRpb25zXG4gICAgY29uc3QgY29uY3VycmVudEV4ZWN1dGlvbnNNZXRyaWMgPSBuZXcgY3cuTWV0cmljKHtcbiAgICAgIG5hbWVzcGFjZTogJ0FXUy9MYW1iZGEnLFxuICAgICAgbWV0cmljTmFtZTogJ0NvbmN1cnJlbnRFeGVjdXRpb25zJyxcbiAgICAgIGRpbWVuc2lvbnNNYXA6IHsgRnVuY3Rpb25OYW1lOiBwcm9jZXNzaW5nTGFtYmRhLmZ1bmN0aW9uTmFtZSB9LFxuICAgICAgc3RhdGlzdGljOiAnQXZlcmFnZScsXG4gICAgICBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDIpXG4gICAgfSk7XG4gICAgY29uc3Qgam9iQ29tcGxldGlvbkV4cHIgPSBuZXcgY3cuTWF0aEV4cHJlc3Npb24oe1xuICAgICAgZXhwcmVzc2lvbjogJ0lGKHF1ZXVlIDwgMSBBTkQgY29uY3VycmVudCA8IDEsIDEsIDApJyxcbiAgICAgIHVzaW5nTWV0cmljczoge1xuICAgICAgICBxdWV1ZTogdGFza3NRdWV1ZS5tZXRyaWNBcHByb3hpbWF0ZU51bWJlck9mTWVzc2FnZXNWaXNpYmxlKHtcbiAgICAgICAgICBzdGF0aXN0aWM6ICdBdmVyYWdlJyxcbiAgICAgICAgICBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDIpXG4gICAgICAgIH0pLFxuICAgICAgICBjb25jdXJyZW50OiBjb25jdXJyZW50RXhlY3V0aW9uc01ldHJpY1xuICAgICAgfVxuICAgIH0pO1xuICAgIGNvbnN0IGpvYkNvbXBsZXRpb25BbGFybSA9IG5ldyBjdy5BbGFybSh0aGlzLCAnSm9iQ29tcGxldGlvbkFsYXJtJywge1xuICAgICAgYWxhcm1OYW1lOiAnSm9iQ29tcGxldGlvbkRldGVjdGVkJyxcbiAgICAgIGFsYXJtRGVzY3JpcHRpb246ICdUcmlnZ2VyZWQgd2hlbiBhbGwgam9icyBhcmUgcHJvY2Vzc2VkIChxdWV1ZSBlbXB0eSBhbmQgbm8gYWN0aXZlIGV4ZWN1dGlvbnMpJyxcbiAgICAgIG1ldHJpYzogam9iQ29tcGxldGlvbkV4cHIsXG4gICAgICB0aHJlc2hvbGQ6IDEsXG4gICAgICBldmFsdWF0aW9uUGVyaW9kczogMSxcbiAgICAgIGNvbXBhcmlzb25PcGVyYXRvcjogY3cuQ29tcGFyaXNvbk9wZXJhdG9yLkdSRUFURVJfVEhBTl9PUl9FUVVBTF9UT19USFJFU0hPTEQsXG4gICAgICB0cmVhdE1pc3NpbmdEYXRhOiBjdy5UcmVhdE1pc3NpbmdEYXRhLk5PVF9CUkVBQ0hJTkdcbiAgICB9KTtcblxuICAgIGNvbnN0IGFsZXJ0QWN0aW9uID0gbmV3IGN3YWN0aW9ucy5TbnNBY3Rpb24oYWxlcnRzVG9waWMpO1xuXG4gICAgZGxxRGVwdGhBbGFybS5hZGRBbGFybUFjdGlvbihhbGVydEFjdGlvbik7XG4gICAgcHJvY2Vzc2luZ0Vycm9yc0FsYXJtLmFkZEFsYXJtQWN0aW9uKGFsZXJ0QWN0aW9uKTtcbiAgICBxdWV1ZURlcHRoQWxhcm0uYWRkQWxhcm1BY3Rpb24oYWxlcnRBY3Rpb24pO1xuICAgIGpvYkNvbXBsZXRpb25BbGFybS5hZGRBbGFybUFjdGlvbihhbGVydEFjdGlvbik7XG4gICAgcXVldWVBZ2VBbGFybS5hZGRBbGFybUFjdGlvbihhbGVydEFjdGlvbik7XG5cbiAgICAvLyBDbG91ZFdhdGNoIERhc2hib2FyZFxuICAgIGNvbnN0IGRhc2hib2FyZCA9IG5ldyBjdy5EYXNoYm9hcmQodGhpcywgJ1BhcmFsbGVsUHJvY2Vzc2luZ0Rhc2hib2FyZCcsIHtcbiAgICAgIGRhc2hib2FyZE5hbWU6ICdBbWlyYUxhbWJkYVBhcmFsbGVsJ1xuICAgIH0pO1xuXG4gICAgZGFzaGJvYXJkLmFkZFdpZGdldHMoXG4gICAgICBuZXcgY3cuR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ1NRUyBRdWV1ZSBNZXRyaWNzJyxcbiAgICAgICAgbGVmdDogW3Rhc2tzUXVldWUubWV0cmljQXBwcm94aW1hdGVOdW1iZXJPZk1lc3NhZ2VzVmlzaWJsZSgpXSxcbiAgICAgICAgcmlnaHQ6IFt0YXNrc1F1ZXVlLm1ldHJpY0FwcHJveGltYXRlQWdlT2ZPbGRlc3RNZXNzYWdlKCldLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3LkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdMYW1iZGEgUHJvY2Vzc2luZyBNZXRyaWNzJyxcbiAgICAgICAgbGVmdDogW3Byb2Nlc3NpbmdMYW1iZGEubWV0cmljSW52b2NhdGlvbnMoKSwgcHJvY2Vzc2luZ0xhbWJkYS5tZXRyaWNEdXJhdGlvbigpXSxcbiAgICAgICAgcmlnaHQ6IFtwcm9jZXNzaW5nTGFtYmRhLm1ldHJpY0Vycm9ycygpLCBwcm9jZXNzaW5nTGFtYmRhLm1ldHJpY1Rocm90dGxlcygpXSxcbiAgICAgICAgd2lkdGg6IDEyXG4gICAgICB9KSxcbiAgICAgIG5ldyBjdy5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnTGFtYmRhIENvbmN1cnJlbmN5JyxcbiAgICAgICAgbGVmdDogW2NvbmN1cnJlbnRFeGVjdXRpb25zTWV0cmljXSxcbiAgICAgICAgd2lkdGg6IDI0XG4gICAgICB9KSxcbiAgICAgIG5ldyBjdy5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnRExRIERlcHRoJyxcbiAgICAgICAgbGVmdDogW2RscS5tZXRyaWNBcHByb3hpbWF0ZU51bWJlck9mTWVzc2FnZXNWaXNpYmxlKCldLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3LkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdKb2IgQ29tcGxldGlvbiBUcmFja2luZycsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHtcbiAgICAgICAgICAgIG5hbWVzcGFjZTogJ0FtaXJhL0pvYnMnLFxuICAgICAgICAgICAgbWV0cmljTmFtZTogJ0pvYnNDb21wbGV0ZWQnLFxuICAgICAgICAgICAgc3RhdGlzdGljOiAnU3VtJ1xuICAgICAgICAgIH0pLFxuICAgICAgICAgIG5ldyBjdy5NZXRyaWMoe1xuICAgICAgICAgICAgbmFtZXNwYWNlOiAnQW1pcmEvSm9icycsXG4gICAgICAgICAgICBtZXRyaWNOYW1lOiAnSm9ic0ZhaWxlZCcsXG4gICAgICAgICAgICBzdGF0aXN0aWM6ICdTdW0nXG4gICAgICAgICAgfSlcbiAgICAgICAgXSxcbiAgICAgICAgd2lkdGg6IDEyXG4gICAgICB9KSxcbiAgICAgIG5ldyBjdy5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnUHJvY2Vzc2luZ1RpbWUgKG1zKScsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQW1pcmEvSm9icycsIG1ldHJpY05hbWU6ICdQcm9jZXNzaW5nVGltZScsIHN0YXRpc3RpYzogJ0F2ZXJhZ2UnIH0pXG4gICAgICAgIF0sXG4gICAgICAgIHJpZ2h0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0FtaXJhL0pvYnMnLCBtZXRyaWNOYW1lOiAnUHJvY2Vzc2luZ1RpbWUnLCBzdGF0aXN0aWM6ICdwOTUnIH0pXG4gICAgICAgIF0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSksXG4gICAgICBuZXcgY3cuR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0luZmVyZW5jZSBUb3RhbCAocDk1IG1zKScsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQW1pcmEvSW5mZXJlbmNlJywgbWV0cmljTmFtZTogJ0luZmVyZW5jZVRvdGFsTXMnLCBzdGF0aXN0aWM6ICdwOTUnIH0pXG4gICAgICAgIF0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSksXG4gICAgICBuZXcgY3cuR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0FjdGl2aXR5IFRvdGFsIChwOTUgbXMpJyxcbiAgICAgICAgbGVmdDogW1xuICAgICAgICAgIG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdBbWlyYS9BY3Rpdml0eScsIG1ldHJpY05hbWU6ICdBY3Rpdml0eVRvdGFsTXMnLCBzdGF0aXN0aWM6ICdwOTUnIH0pXG4gICAgICAgIF0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSlcbiAgICApO1xuXG4gICAgLy8gT3V0cHV0c1xuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdUYXNrc1F1ZXVlVXJsJywge1xuICAgICAgdmFsdWU6IHRhc2tzUXVldWUucXVldWVVcmwsXG4gICAgICBkZXNjcmlwdGlvbjogJ1NRUyBUYXNrcyBRdWV1ZSBVUkwnXG4gICAgfSk7XG5cbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnUHJvY2Vzc2luZ0xhbWJkYUFybicsIHtcbiAgICAgIHZhbHVlOiBwcm9jZXNzaW5nTGFtYmRhLmZ1bmN0aW9uQXJuLFxuICAgICAgZGVzY3JpcHRpb246ICdQcm9jZXNzaW5nIExhbWJkYSBGdW5jdGlvbiBBUk4nXG4gICAgfSk7XG5cbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnUmVzdWx0c0J1Y2tldE5hbWUnLCB7XG4gICAgICB2YWx1ZTogcmVzdWx0c0J1Y2tldC5idWNrZXROYW1lLFxuICAgICAgZGVzY3JpcHRpb246ICdTMyBSZXN1bHRzIEJ1Y2tldCBOYW1lJ1xuICAgIH0pO1xuXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ01hbnVhbFRyaWdnZXJGdW5jdGlvbk5hbWUnLCB7XG4gICAgICB2YWx1ZTogbWFudWFsVHJpZ2dlckxhbWJkYS5mdW5jdGlvbk5hbWUsXG4gICAgICBkZXNjcmlwdGlvbjogJ01hbnVhbCB0cmlnZ2VyIExhbWJkYSBmdW5jdGlvbiBuYW1lICh1c2Ugd2l0aCBBV1MgQ0xJKSdcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdEYXNoYm9hcmRVcmwnLCB7XG4gICAgICB2YWx1ZTogYGh0dHBzOi8vJHt0aGlzLnJlZ2lvbn0uY29uc29sZS5hd3MuYW1hem9uLmNvbS9jbG91ZHdhdGNoL2hvbWU/cmVnaW9uPSR7dGhpcy5yZWdpb259I2Rhc2hib2FyZHM6bmFtZT0ke2Rhc2hib2FyZC5kYXNoYm9hcmROYW1lfWAsXG4gICAgICBkZXNjcmlwdGlvbjogJ0Nsb3VkV2F0Y2ggRGFzaGJvYXJkIFVSTCdcbiAgICB9KTtcbiAgfVxufVxuIl19