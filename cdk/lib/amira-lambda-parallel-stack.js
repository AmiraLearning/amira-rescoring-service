"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.AmiraLambdaParallelStack = void 0;
const cdk = require("aws-cdk-lib");
const lambda = require("aws-cdk-lib/aws-lambda");
const ec2 = require("aws-cdk-lib/aws-ec2");
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
        tritonUrlSsmCheck.node.defaultChild.cfnOptions.condition = new cdk.CfnCondition(this, 'TritonUrlNotProvided', {
            expression: cdk.Fn.conditionEquals(tritonClusterUrlParam.valueAsString, '')
        });
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
                USE_TRITON: cdk.Token.asString(cdk.Fn.conditionIf(tritonUrlProvided.logicalId, 'true', 'false')),
                TRITON_URL: cdk.Token.asString(cdk.Fn.conditionIf(tritonUrlProvided.logicalId, tritonClusterUrlParam.valueAsString, ssm.StringParameter.valueForStringParameter(this, '/amira/triton_alb_url'))),
                TRITON_MODEL: 'w2v2',
                PYTHONOPTIMIZE: '2',
                TORCH_NUM_THREADS: '6',
                OMP_NUM_THREADS: '6',
                TRANSFORMERS_CACHE: '/tmp/models',
                HF_HUB_CACHE: '/tmp/hf_cache'
            }
        });
        // Conditionally attach Lambda to VPC private subnets
        const subnetsList = privateSubnetIdsCsvParam.valueAsList;
        const vpc = ec2.Vpc.fromVpcAttributes(this, 'ImportedVpc', {
            vpcId: cdk.Token.asString(cdk.Fn.conditionIf(vpcProvided.logicalId, vpcIdParam.valueAsString, cdk.Aws.NO_VALUE)),
            availabilityZones: cdk.Stack.of(this).availabilityZones,
            privateSubnetIds: subnetsList
        });
        const lambdaSg = ec2.SecurityGroup.fromSecurityGroupId(this, 'ImportedLambdaSg', cdk.Token.asString(cdk.Fn.conditionIf(lambdaSgProvided.logicalId, lambdaSecurityGroupIdParam.valueAsString, cdk.Aws.NO_VALUE)), { mutable: false });
        const cfnFunc = processingLambda.node.defaultChild;
        cfnFunc.vpcConfig = cdk.Token.asAny(cdk.Fn.conditionIf(vpcProvided.logicalId, {
            SecurityGroupIds: cdk.Token.asList(cdk.Fn.conditionIf(lambdaSgProvided.logicalId, [lambdaSecurityGroupIdParam.valueAsString], cdk.Aws.NO_VALUE)),
            SubnetIds: subnetsList
        }, cdk.Aws.NO_VALUE));
        // Enforce: if VpcId is provided, LambdaSecurityGroupId must be provided
        new cdk.CfnRule(this, 'VpcRequiresLambdaSg', {
            ruleCondition: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(vpcIdParam.valueAsString, '')),
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
            timeout: cdk.Duration.minutes(5),
            environment: {
                AWS_REGION: cdk.Stack.of(this).region,
            }
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
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiYW1pcmEtbGFtYmRhLXBhcmFsbGVsLXN0YWNrLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsiYW1pcmEtbGFtYmRhLXBhcmFsbGVsLXN0YWNrLnRzIl0sIm5hbWVzIjpbXSwibWFwcGluZ3MiOiI7OztBQUFBLG1DQUFtQztBQUNuQyxpREFBaUQ7QUFDakQsMkNBQTJDO0FBQzNDLDJDQUEyQztBQUMzQyx5Q0FBeUM7QUFDekMsMkNBQTJDO0FBQzNDLGlEQUFpRDtBQUNqRCwwREFBMEQ7QUFDMUQsZ0VBQWdFO0FBQ2hFLDJDQUEyQztBQUMzQyw2Q0FBNkM7QUFDN0MsMkNBQTJDO0FBQzNDLGlEQUFpRDtBQUNqRCwyQ0FBMkM7QUFFM0MsZ0VBQWdFO0FBQ2hFLG1EQUFtRDtBQUduRCxNQUFhLHdCQUF5QixTQUFRLEdBQUcsQ0FBQyxLQUFLO0lBQ3JELFlBQVksS0FBZ0IsRUFBRSxFQUFVLEVBQUUsS0FBc0I7UUFDOUQsS0FBSyxDQUFDLEtBQUssRUFBRSxFQUFFLEVBQUUsS0FBSyxDQUFDLENBQUM7UUFFeEIsK0JBQStCO1FBQy9CLE1BQU0sYUFBYSxHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsZ0JBQWdCLEVBQUU7WUFDakUsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsU0FBUztZQUNsQixXQUFXLEVBQUUsc0JBQXNCO1NBQ3BDLENBQUMsQ0FBQztRQUNILE1BQU0saUJBQWlCLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxjQUFjLEVBQUU7WUFDbkUsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsNEJBQTRCO1lBQ3JDLFdBQVcsRUFBRSw2QkFBNkI7U0FDM0MsQ0FBQyxDQUFDO1FBQ0gsTUFBTSxnQkFBZ0IsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLGFBQWEsRUFBRTtZQUNqRSxJQUFJLEVBQUUsUUFBUTtZQUNkLE9BQU8sRUFBRSwyREFBMkQ7WUFDcEUsV0FBVyxFQUFFLG9DQUFvQztTQUNsRCxDQUFDLENBQUM7UUFDSCxNQUFNLGNBQWMsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLFdBQVcsRUFBRTtZQUM3RCxJQUFJLEVBQUUsUUFBUTtZQUNkLE9BQU8sRUFBRSw2QkFBNkI7WUFDdEMsV0FBVyxFQUFFLDRCQUE0QjtTQUMxQyxDQUFDLENBQUM7UUFDSCxNQUFNLGtCQUFrQixHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsZUFBZSxFQUFFO1lBQ3JFLElBQUksRUFBRSxRQUFRO1lBQ2QsT0FBTyxFQUFFLFVBQVU7WUFDbkIsV0FBVyxFQUFFLGtDQUFrQztTQUNoRCxDQUFDLENBQUM7UUFDSCxNQUFNLG9CQUFvQixHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsaUJBQWlCLEVBQUU7WUFDekUsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsRUFBRTtZQUNYLFdBQVcsRUFBRSwyRUFBMkU7U0FDekYsQ0FBQyxDQUFDO1FBQ0gsTUFBTSxpQkFBaUIsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLGlCQUFpQixFQUFFO1lBQ3RFLElBQUksRUFBRSxRQUFRO1lBQ2QsT0FBTyxFQUFFLEVBQUU7WUFDWCxXQUFXLEVBQUUsOERBQThEO1lBQzNFLE1BQU0sRUFBRSxJQUFJO1NBQ2IsQ0FBQyxDQUFDO1FBQ0gsTUFBTSxxQkFBcUIsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLGtCQUFrQixFQUFFO1lBQzNFLElBQUksRUFBRSxRQUFRO1lBQ2QsT0FBTyxFQUFFLEVBQUU7WUFDWCxXQUFXLEVBQUUsNkhBQTZIO1NBQzNJLENBQUMsQ0FBQztRQUVILE1BQU0saUJBQWlCLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxjQUFjLEVBQUU7WUFDbkUsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsT0FBTztZQUNoQixhQUFhLEVBQUUsQ0FBQyxNQUFNLEVBQUUsT0FBTyxDQUFDO1lBQ2hDLFdBQVcsRUFBRSwyRUFBMkU7U0FDekYsQ0FBQyxDQUFDO1FBRUgsNERBQTREO1FBQzVELE1BQU0sVUFBVSxHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsT0FBTyxFQUFFO1lBQ3JELElBQUksRUFBRSxtQkFBbUI7WUFDekIsT0FBTyxFQUFFLEdBQUcsQ0FBQyxHQUFHLENBQUMsUUFBZTtZQUNoQyxXQUFXLEVBQUUscUVBQXFFO1NBQ25GLENBQUMsQ0FBQztRQUNILE1BQU0sd0JBQXdCLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxxQkFBcUIsRUFBRTtZQUNqRixJQUFJLEVBQUUsNEJBQTRCO1lBQ2xDLFdBQVcsRUFBRSw4Q0FBOEM7U0FDNUQsQ0FBQyxDQUFDO1FBQ0gsTUFBTSwwQkFBMEIsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLHVCQUF1QixFQUFFO1lBQ3JGLElBQUksRUFBRSxRQUFRO1lBQ2QsT0FBTyxFQUFFLEdBQUcsQ0FBQyxHQUFHLENBQUMsUUFBZTtZQUNoQyxXQUFXLEVBQUUsK0RBQStEO1NBQzdFLENBQUMsQ0FBQztRQUNILE1BQU0sV0FBVyxHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsYUFBYSxFQUFFO1lBQzVELFVBQVUsRUFBRSxHQUFHLENBQUMsRUFBRSxDQUFDLFlBQVksQ0FBQyxHQUFHLENBQUMsRUFBRSxDQUFDLGVBQWUsQ0FBQyxVQUFVLENBQUMsYUFBYSxFQUFFLEVBQUUsQ0FBQyxDQUFDO1NBQ3RGLENBQUMsQ0FBQztRQUNILE1BQU0sZ0JBQWdCLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxrQkFBa0IsRUFBRTtZQUN0RSxVQUFVLEVBQUUsR0FBRyxDQUFDLEVBQUUsQ0FBQyxZQUFZLENBQUMsR0FBRyxDQUFDLEVBQUUsQ0FBQyxlQUFlLENBQUMsMEJBQTBCLENBQUMsYUFBYSxFQUFFLEVBQUUsQ0FBQyxDQUFDO1NBQ3RHLENBQUMsQ0FBQztRQUVILHlCQUF5QjtRQUN6QixNQUFNLE1BQU0sR0FBRyxJQUFJLEdBQUcsQ0FBQyxHQUFHLENBQUMsSUFBSSxFQUFFLGtCQUFrQixFQUFFO1lBQ25ELGlCQUFpQixFQUFFLElBQUk7WUFDdkIsS0FBSyxFQUFFLDZCQUE2QjtTQUNyQyxDQUFDLENBQUM7UUFFSCxpQkFBaUI7UUFDakIsTUFBTSxnQkFBZ0IsR0FBRyxJQUFJLEVBQUUsQ0FBQyxNQUFNLENBQUMsSUFBSSxFQUFFLHdCQUF3QixFQUFFO1lBQ3JFLFNBQVMsRUFBRSxLQUFLO1lBQ2hCLGlCQUFpQixFQUFFLEVBQUUsQ0FBQyxpQkFBaUIsQ0FBQyxTQUFTO1lBQ2pELFVBQVUsRUFBRSxFQUFFLENBQUMsZ0JBQWdCLENBQUMsVUFBVTtZQUMxQyxVQUFVLEVBQUUsSUFBSTtZQUNoQixhQUFhLEVBQUUsR0FBRyxDQUFDLGFBQWEsQ0FBQyxNQUFNO1NBQ3hDLENBQUMsQ0FBQztRQUVILE1BQU0sYUFBYSxHQUFHLElBQUksRUFBRSxDQUFDLE1BQU0sQ0FBQyxJQUFJLEVBQUUsZUFBZSxFQUFFO1lBQ3pELFNBQVMsRUFBRSxLQUFLO1lBQ2hCLGlCQUFpQixFQUFFLEVBQUUsQ0FBQyxpQkFBaUIsQ0FBQyxTQUFTO1lBQ2pELFVBQVUsRUFBRSxFQUFFLENBQUMsZ0JBQWdCLENBQUMsR0FBRztZQUNuQyxhQUFhLEVBQUUsTUFBTTtZQUNyQixnQkFBZ0IsRUFBRSxJQUFJO1lBQ3RCLHNCQUFzQixFQUFFLGdCQUFnQjtZQUN4QyxzQkFBc0IsRUFBRSxpQkFBaUI7WUFDekMsVUFBVSxFQUFFLElBQUk7WUFDaEIsY0FBYyxFQUFFO2dCQUNkO29CQUNFLEVBQUUsRUFBRSx1QkFBdUI7b0JBQzNCLE9BQU8sRUFBRSxJQUFJO29CQUNiLFdBQVcsRUFBRTt3QkFDWCxFQUFFLFlBQVksRUFBRSxFQUFFLENBQUMsWUFBWSxDQUFDLG1CQUFtQixFQUFFLGVBQWUsRUFBRSxHQUFHLENBQUMsUUFBUSxDQUFDLElBQUksQ0FBQyxDQUFDLENBQUMsRUFBRTtxQkFDN0Y7aUJBQ0Y7Z0JBQ0Q7b0JBQ0UsRUFBRSxFQUFFLG1CQUFtQjtvQkFDdkIsT0FBTyxFQUFFLElBQUk7b0JBQ2IsV0FBVyxFQUFFO3dCQUNYLEVBQUUsWUFBWSxFQUFFLEVBQUUsQ0FBQyxZQUFZLENBQUMsaUJBQWlCLEVBQUUsZUFBZSxFQUFFLEdBQUcsQ0FBQyxRQUFRLENBQUMsSUFBSSxDQUFDLEVBQUUsQ0FBQyxFQUFFO3FCQUM1RjtpQkFDRjtnQkFDRDtvQkFDRSxFQUFFLEVBQUUseUJBQXlCO29CQUM3QixPQUFPLEVBQUUsSUFBSTtvQkFDYixXQUFXLEVBQUU7d0JBQ1gsRUFBRSxZQUFZLEVBQUUsRUFBRSxDQUFDLFlBQVksQ0FBQyx5QkFBeUIsRUFBRSxlQUFlLEVBQUUsR0FBRyxDQUFDLFFBQVEsQ0FBQyxJQUFJLENBQUMsR0FBRyxDQUFDLEVBQUU7cUJBQ3JHO2lCQUNGO2FBQ0Y7WUFDRCxhQUFhLEVBQUUsR0FBRyxDQUFDLGFBQWEsQ0FBQyxNQUFNO1NBQ3hDLENBQUMsQ0FBQztRQUNILGFBQWEsQ0FBQyxtQkFBbUIsQ0FBQyxJQUFJLEdBQUcsQ0FBQyxlQUFlLENBQUM7WUFDeEQsR0FBRyxFQUFFLHVCQUF1QjtZQUM1QixNQUFNLEVBQUUsR0FBRyxDQUFDLE1BQU0sQ0FBQyxJQUFJO1lBQ3ZCLFVBQVUsRUFBRSxDQUFDLElBQUksR0FBRyxDQUFDLFlBQVksRUFBRSxDQUFDO1lBQ3BDLE9BQU8sRUFBRSxDQUFDLE1BQU0sQ0FBQztZQUNqQixTQUFTLEVBQUUsQ0FBQyxhQUFhLENBQUMsU0FBUyxFQUFFLEdBQUcsYUFBYSxDQUFDLFNBQVMsSUFBSSxDQUFDO1lBQ3BFLFVBQVUsRUFBRSxFQUFFLElBQUksRUFBRSxFQUFFLHFCQUFxQixFQUFFLE9BQU8sRUFBRSxFQUFFO1NBQ3pELENBQUMsQ0FBQyxDQUFDO1FBQ0osYUFBYSxDQUFDLG1CQUFtQixDQUFDLElBQUksR0FBRyxDQUFDLGVBQWUsQ0FBQztZQUN4RCxHQUFHLEVBQUUsOEJBQThCO1lBQ25DLE1BQU0sRUFBRSxHQUFHLENBQUMsTUFBTSxDQUFDLElBQUk7WUFDdkIsVUFBVSxFQUFFLENBQUMsSUFBSSxHQUFHLENBQUMsWUFBWSxFQUFFLENBQUM7WUFDcEMsT0FBTyxFQUFFLENBQUMsY0FBYyxDQUFDO1lBQ3pCLFNBQVMsRUFBRSxDQUFDLEdBQUcsYUFBYSxDQUFDLFNBQVMsSUFBSSxDQUFDO1lBQzNDLFVBQVUsRUFBRSxFQUFFLGVBQWUsRUFBRSxFQUFFLGlDQUFpQyxFQUFFLFNBQVMsRUFBRSxFQUFFO1NBQ2xGLENBQUMsQ0FBQyxDQUFDO1FBRUosd0JBQXdCO1FBQ3hCLE1BQU0sR0FBRyxHQUFHLElBQUksR0FBRyxDQUFDLEtBQUssQ0FBQyxJQUFJLEVBQUUsZUFBZSxFQUFFO1lBQy9DLGVBQWUsRUFBRSxHQUFHLENBQUMsUUFBUSxDQUFDLElBQUksQ0FBQyxFQUFFLENBQUM7WUFDdEMsVUFBVSxFQUFFLEdBQUcsQ0FBQyxlQUFlLENBQUMsR0FBRztZQUNuQyxtQkFBbUIsRUFBRSxNQUFNO1lBQzNCLFVBQVUsRUFBRSxJQUFJO1NBQ2pCLENBQUMsQ0FBQztRQUVILDJCQUEyQjtRQUMzQixNQUFNLFVBQVUsR0FBRyxJQUFJLEdBQUcsQ0FBQyxLQUFLLENBQUMsSUFBSSxFQUFFLFlBQVksRUFBRTtZQUNuRCxpQkFBaUIsRUFBRSxHQUFHLENBQUMsUUFBUSxDQUFDLEtBQUssQ0FBQyxDQUFDLENBQUM7WUFDeEMsZUFBZSxFQUFFLEVBQUUsS0FBSyxFQUFFLEdBQUcsRUFBRSxlQUFlLEVBQUUsQ0FBQyxFQUFFO1lBQ25ELFVBQVUsRUFBRSxHQUFHLENBQUMsZUFBZSxDQUFDLEdBQUc7WUFDbkMsbUJBQW1CLEVBQUUsTUFBTTtZQUMzQixVQUFVLEVBQUUsSUFBSTtZQUNoQixzQkFBc0IsRUFBRSxHQUFHLENBQUMsUUFBUSxDQUFDLE9BQU8sQ0FBQyxDQUFDLENBQUM7U0FDaEQsQ0FBQyxDQUFDO1FBRUgsa0NBQWtDO1FBQ2xDLE1BQU0sUUFBUSxHQUFHLElBQUksSUFBSSxDQUFDLFFBQVEsQ0FBQyxJQUFJLEVBQUUsb0JBQW9CLEVBQUU7WUFDN0QsWUFBWSxFQUFFLHNDQUFzQztZQUNwRCxTQUFTLEVBQUUsSUFBSSxDQUFDLGFBQWEsQ0FBQyxTQUFTO1lBQ3ZDLGFBQWEsRUFBRSxHQUFHLENBQUMsYUFBYSxDQUFDLE9BQU87WUFDeEMsYUFBYSxFQUFFLE1BQU07U0FDdEIsQ0FBQyxDQUFDO1FBRUgsYUFBYTtRQUNiLE1BQU0sYUFBYSxHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUscUJBQXFCLEVBQUU7WUFDdEUsVUFBVSxFQUFFLEdBQUcsQ0FBQyxFQUFFLENBQUMsWUFBWSxDQUFDLEdBQUcsQ0FBQyxFQUFFLENBQUMsZUFBZSxDQUFDLG9CQUFvQixDQUFDLGFBQWEsRUFBRSxFQUFFLENBQUMsQ0FBQztTQUNoRyxDQUFDLENBQUM7UUFDSCxNQUFNLGlCQUFpQixHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsbUJBQW1CLEVBQUU7WUFDeEUsVUFBVSxFQUFFLEdBQUcsQ0FBQyxFQUFFLENBQUMsWUFBWSxDQUFDLEdBQUcsQ0FBQyxFQUFFLENBQUMsZUFBZSxDQUFDLHFCQUFxQixDQUFDLGFBQWEsRUFBRSxFQUFFLENBQUMsQ0FBQztTQUNqRyxDQUFDLENBQUM7UUFDSCxNQUFNLGFBQWEsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLGVBQWUsRUFBRTtZQUNoRSxVQUFVLEVBQUUsR0FBRyxDQUFDLEVBQUUsQ0FBQyxlQUFlLENBQUMsaUJBQWlCLENBQUMsYUFBYSxFQUFFLE1BQU0sQ0FBQztTQUM1RSxDQUFDLENBQUM7UUFFSCxzR0FBc0c7UUFDdEcsTUFBTSxZQUFZLEdBQUcsdUJBQXVCLENBQUM7UUFDN0MsTUFBTSxXQUFXLEdBQUcsR0FBRyxDQUFDLEdBQUcsQ0FBQyxNQUFNLENBQUMsRUFBRSxPQUFPLEVBQUUsS0FBSyxFQUFFLFFBQVEsRUFBRSxXQUFXLEVBQUUsWUFBWSxFQUFFLHNCQUFzQixFQUFFLEVBQUUsSUFBSSxDQUFDLENBQUM7UUFDMUgsTUFBTSxpQkFBaUIsR0FBRyxJQUFJLEVBQUUsQ0FBQyxpQkFBaUIsQ0FBQyxJQUFJLEVBQUUsbUJBQW1CLEVBQUU7WUFDNUUsUUFBUSxFQUFFO2dCQUNSLE9BQU8sRUFBRSxLQUFLO2dCQUNkLE1BQU0sRUFBRSxjQUFjO2dCQUN0QixVQUFVLEVBQUUsRUFBRSxJQUFJLEVBQUUsWUFBWSxFQUFFO2dCQUNsQyxrQkFBa0IsRUFBRSxFQUFFLENBQUMsa0JBQWtCLENBQUMsRUFBRSxDQUFDLG1CQUFtQixDQUFDO2FBQ2xFO1lBQ0QsUUFBUSxFQUFFO2dCQUNSLE9BQU8sRUFBRSxLQUFLO2dCQUNkLE1BQU0sRUFBRSxjQUFjO2dCQUN0QixVQUFVLEVBQUUsRUFBRSxJQUFJLEVBQUUsWUFBWSxFQUFFO2dCQUNsQyxrQkFBa0IsRUFBRSxFQUFFLENBQUMsa0JBQWtCLENBQUMsRUFBRSxDQUFDLG1CQUFtQixDQUFDO2FBQ2xFO1lBQ0QsTUFBTSxFQUFFLEVBQUUsQ0FBQyx1QkFBdUIsQ0FBQyxZQUFZLENBQUMsRUFBRSxTQUFTLEVBQUUsQ0FBQyxXQUFXLENBQUMsRUFBRSxDQUFDO1NBQzlFLENBQUMsQ0FBQztRQUNGLGlCQUFpQixDQUFDLElBQUksQ0FBQyxZQUFzQyxDQUFDLFVBQVUsQ0FBQyxTQUFTLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxzQkFBc0IsRUFBRTtZQUN2SSxVQUFVLEVBQUUsR0FBRyxDQUFDLEVBQUUsQ0FBQyxlQUFlLENBQUMscUJBQXFCLENBQUMsYUFBYSxFQUFFLEVBQUUsQ0FBQztTQUM1RSxDQUFDLENBQUM7UUFFSCxnRUFBZ0U7UUFDaEUsTUFBTSxnQkFBZ0IsR0FBRyxJQUFJLE1BQU0sQ0FBQyxtQkFBbUIsQ0FBQyxJQUFJLEVBQUUsb0JBQW9CLEVBQUU7WUFDbEYsWUFBWSxFQUFFLDBCQUEwQjtZQUN4QyxJQUFJLEVBQUUsTUFBTSxDQUFDLGVBQWUsQ0FBQyxjQUFjLENBQUMsOEJBQThCLENBQUM7WUFDM0UsT0FBTyxFQUFFLEdBQUcsQ0FBQyxRQUFRLENBQUMsT0FBTyxDQUFDLEVBQUUsQ0FBQztZQUNqQyxVQUFVLEVBQUUsS0FBSztZQUNqQiwyRUFBMkU7WUFDM0UsZUFBZSxFQUFFLEdBQUc7WUFDcEIsT0FBTyxFQUFFLE1BQU0sQ0FBQyxPQUFPLENBQUMsTUFBTTtZQUM5QixXQUFXLEVBQUU7Z0JBQ1gsY0FBYyxFQUFFLGFBQWEsQ0FBQyxVQUFVO2dCQUN4QyxjQUFjLEVBQUUsa0JBQWtCLENBQUMsYUFBYTtnQkFDaEQsVUFBVSxFQUFFLGdDQUFnQztnQkFDNUMsWUFBWSxFQUFFLG9CQUFvQixDQUFDLGFBQWE7Z0JBQ2hELFVBQVUsRUFBRSxNQUFNLENBQUMsS0FBSztnQkFDeEIsaUJBQWlCLEVBQUUsaUJBQWlCLENBQUMsYUFBYTtnQkFDbEQsZUFBZSxFQUFFLElBQUk7Z0JBQ3JCLGlCQUFpQixFQUFFLE1BQU07Z0JBQ3pCLFdBQVcsRUFBRSxNQUFNO2dCQUNuQixrQkFBa0IsRUFBRSxNQUFNO2dCQUMxQixTQUFTLEVBQUUsT0FBTztnQkFDbEIsVUFBVSxFQUFFLEdBQUcsQ0FBQyxLQUFLLENBQUMsUUFBUSxDQUFDLEdBQUcsQ0FBQyxFQUFFLENBQUMsV0FBVyxDQUFDLGlCQUFpQixDQUFDLFNBQVMsRUFBRSxNQUFNLEVBQUUsT0FBTyxDQUFDLENBQUM7Z0JBQ2hHLFVBQVUsRUFBRSxHQUFHLENBQUMsS0FBSyxDQUFDLFFBQVEsQ0FBQyxHQUFHLENBQUMsRUFBRSxDQUFDLFdBQVcsQ0FDL0MsaUJBQWlCLENBQUMsU0FBUyxFQUMzQixxQkFBcUIsQ0FBQyxhQUFhLEVBQ25DLEdBQUcsQ0FBQyxlQUFlLENBQUMsdUJBQXVCLENBQUMsSUFBSSxFQUFFLHVCQUF1QixDQUFDLENBQzNFLENBQUM7Z0JBQ0YsWUFBWSxFQUFFLE1BQU07Z0JBQ3BCLGNBQWMsRUFBRSxHQUFHO2dCQUNuQixpQkFBaUIsRUFBRSxHQUFHO2dCQUN0QixlQUFlLEVBQUUsR0FBRztnQkFDcEIsa0JBQWtCLEVBQUUsYUFBYTtnQkFDakMsWUFBWSxFQUFFLGVBQWU7YUFDOUI7U0FDRixDQUFDLENBQUM7UUFFSCxxREFBcUQ7UUFDckQsTUFBTSxXQUFXLEdBQUcsd0JBQXdCLENBQUMsV0FBVyxDQUFDO1FBQ3pELE1BQU0sR0FBRyxHQUFHLEdBQUcsQ0FBQyxHQUFHLENBQUMsaUJBQWlCLENBQUMsSUFBSSxFQUFFLGFBQWEsRUFBRTtZQUN6RCxLQUFLLEVBQUUsR0FBRyxDQUFDLEtBQUssQ0FBQyxRQUFRLENBQUMsR0FBRyxDQUFDLEVBQUUsQ0FBQyxXQUFXLENBQUMsV0FBVyxDQUFDLFNBQVMsRUFBRSxVQUFVLENBQUMsYUFBYSxFQUFFLEdBQUcsQ0FBQyxHQUFHLENBQUMsUUFBZSxDQUFDLENBQUM7WUFDdkgsaUJBQWlCLEVBQUUsR0FBRyxDQUFDLEtBQUssQ0FBQyxFQUFFLENBQUMsSUFBSSxDQUFDLENBQUMsaUJBQWlCO1lBQ3ZELGdCQUFnQixFQUFFLFdBQWtDO1NBQ3JELENBQUMsQ0FBQztRQUNILE1BQU0sUUFBUSxHQUFHLEdBQUcsQ0FBQyxhQUFhLENBQUMsbUJBQW1CLENBQUMsSUFBSSxFQUFFLGtCQUFrQixFQUFFLEdBQUcsQ0FBQyxLQUFLLENBQUMsUUFBUSxDQUFDLEdBQUcsQ0FBQyxFQUFFLENBQUMsV0FBVyxDQUFDLGdCQUFnQixDQUFDLFNBQVMsRUFBRSwwQkFBMEIsQ0FBQyxhQUFhLEVBQUUsR0FBRyxDQUFDLEdBQUcsQ0FBQyxRQUFlLENBQUMsQ0FBQyxFQUFFLEVBQUUsT0FBTyxFQUFFLEtBQUssRUFBRSxDQUFDLENBQUM7UUFDNU8sTUFBTSxPQUFPLEdBQUcsZ0JBQWdCLENBQUMsSUFBSSxDQUFDLFlBQWtDLENBQUM7UUFDekUsT0FBTyxDQUFDLFNBQVMsR0FBRyxHQUFHLENBQUMsS0FBSyxDQUFDLEtBQUssQ0FBQyxHQUFHLENBQUMsRUFBRSxDQUFDLFdBQVcsQ0FDcEQsV0FBVyxDQUFDLFNBQVMsRUFDckI7WUFDRSxnQkFBZ0IsRUFBRSxHQUFHLENBQUMsS0FBSyxDQUFDLE1BQU0sQ0FBQyxHQUFHLENBQUMsRUFBRSxDQUFDLFdBQVcsQ0FBQyxnQkFBZ0IsQ0FBQyxTQUFTLEVBQUUsQ0FBQywwQkFBMEIsQ0FBQyxhQUFhLENBQUMsRUFBRSxHQUFHLENBQUMsR0FBRyxDQUFDLFFBQWUsQ0FBQyxDQUFDO1lBQ3ZKLFNBQVMsRUFBRSxXQUFXO1NBQ3ZCLEVBQ0QsR0FBRyxDQUFDLEdBQUcsQ0FBQyxRQUFRLENBQ2pCLENBQUMsQ0FBQztRQUVILHdFQUF3RTtRQUN4RSxJQUFJLEdBQUcsQ0FBQyxPQUFPLENBQUMsSUFBSSxFQUFFLHFCQUFxQixFQUFFO1lBQzNDLGFBQWEsRUFBRSxHQUFHLENBQUMsRUFBRSxDQUFDLFlBQVksQ0FBQyxHQUFHLENBQUMsRUFBRSxDQUFDLGVBQWUsQ0FBQyxVQUFVLENBQUMsYUFBYSxFQUFFLEVBQUUsQ0FBQyxDQUFDO1lBQ3hGLFVBQVUsRUFBRTtnQkFDVjtvQkFDRSxNQUFNLEVBQUUsR0FBRyxDQUFDLEVBQUUsQ0FBQyxZQUFZLENBQUMsR0FBRyxDQUFDLEVBQUUsQ0FBQyxlQUFlLENBQUMsMEJBQTBCLENBQUMsYUFBYSxFQUFFLEVBQUUsQ0FBQyxDQUFDO29CQUNqRyxpQkFBaUIsRUFBRSxzRUFBc0U7aUJBQzFGO2FBQ0Y7U0FDRixDQUFDLENBQUM7UUFFSCw4QkFBOEI7UUFDOUIsTUFBTSw4QkFBOEIsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLDJCQUEyQixFQUFFO1lBQzdGLElBQUksRUFBRSxRQUFRO1lBQ2QsT0FBTyxFQUFFLEVBQUU7WUFDWCxXQUFXLEVBQUUsNERBQTREO1NBQzFFLENBQUMsQ0FBQztRQUVILE1BQU0sV0FBVyxHQUFHLElBQUksT0FBTyxDQUFDLGNBQWMsQ0FBQyxVQUFVLEVBQUU7WUFDekQsU0FBUyxFQUFFLENBQUM7WUFDWixjQUFjLEVBQUUsOEJBQThCLENBQUMsYUFBYTtZQUM1RCx1QkFBdUIsRUFBRSxJQUFJO1lBQzdCLGlCQUFpQixFQUFFLEdBQUcsQ0FBQyxRQUFRLENBQUMsT0FBTyxDQUFDLENBQUMsQ0FBQztTQUMzQyxDQUFDLENBQUM7UUFDSCxnQkFBZ0IsQ0FBQyxjQUFjLENBQUMsV0FBVyxDQUFDLENBQUM7UUFFN0Msd0JBQXdCO1FBQ3hCLE1BQU0sa0JBQWtCLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxlQUFlLEVBQUU7WUFDckUsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsT0FBTztZQUNoQixhQUFhLEVBQUUsQ0FBQyxNQUFNLEVBQUUsT0FBTyxDQUFDO1lBQ2hDLFdBQVcsRUFBRSx1REFBdUQ7U0FDckUsQ0FBQyxDQUFDO1FBQ0gsTUFBTSxvQkFBb0IsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLGlCQUFpQixFQUFFO1lBQ3pFLElBQUksRUFBRSxRQUFRO1lBQ2QsT0FBTyxFQUFFLEVBQUU7WUFDWCxXQUFXLEVBQUUsbURBQW1EO1NBQ2pFLENBQUMsQ0FBQztRQUNILE1BQU0sV0FBVyxHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsYUFBYSxFQUFFO1lBQzVELFVBQVUsRUFBRSxHQUFHLENBQUMsRUFBRSxDQUFDLGVBQWUsQ0FBQyxrQkFBa0IsQ0FBQyxhQUFhLEVBQUUsTUFBTSxDQUFDO1NBQzdFLENBQUMsQ0FBQztRQUNILE1BQU0sV0FBVyxHQUFHLElBQUksTUFBTSxDQUFDLE9BQU8sQ0FBQyxJQUFJLEVBQUUsb0JBQW9CLEVBQUU7WUFDakUsa0JBQWtCLEVBQUUsR0FBRyxDQUFDLEVBQUUsQ0FBQyxHQUFHLENBQUMsMEJBQTBCLEVBQUUsRUFBRSxPQUFPLEVBQUUsb0JBQW9CLENBQUMsYUFBYSxFQUFFLENBQUM7WUFDM0csS0FBSyxFQUFFLFNBQVM7WUFDaEIsT0FBTyxFQUFFO2dCQUNQO29CQUNFLEVBQUUsRUFBRSxTQUFTO29CQUNiLEdBQUcsRUFBRSxnQkFBZ0IsQ0FBQyxXQUFXO29CQUNqQyxLQUFLLEVBQUUsSUFBSSxDQUFDLFNBQVMsQ0FBQyxFQUFFLElBQUksRUFBRSxJQUFJLEVBQUUsQ0FBQztpQkFDdEM7YUFDRjtTQUNGLENBQUMsQ0FBQztRQUNILFdBQVcsQ0FBQyxVQUFVLENBQUMsU0FBUyxHQUFHLFdBQVcsQ0FBQztRQUMvQyxNQUFNLGNBQWMsR0FBRyxJQUFJLE1BQU0sQ0FBQyxhQUFhLENBQUMsSUFBSSxFQUFFLDRCQUE0QixFQUFFO1lBQ2xGLE1BQU0sRUFBRSx1QkFBdUI7WUFDL0IsWUFBWSxFQUFFLGdCQUFnQixDQUFDLFlBQVk7WUFDM0MsU0FBUyxFQUFFLHNCQUFzQjtZQUNqQyxTQUFTLEVBQUUsV0FBVyxDQUFDLE9BQU87U0FDL0IsQ0FBQyxDQUFDO1FBQ0gsY0FBYyxDQUFDLFVBQVUsQ0FBQyxTQUFTLEdBQUcsV0FBVyxDQUFDO1FBRWxELE1BQU0sY0FBYyxHQUFHLElBQUksR0FBRyxDQUFDLGNBQWMsQ0FBQztZQUM1QyxVQUFVLEVBQUU7Z0JBQ1YsSUFBSSxHQUFHLENBQUMsZUFBZSxDQUFDO29CQUN0QixPQUFPLEVBQUUsQ0FBQyxlQUFlLENBQUM7b0JBQzFCLFNBQVMsRUFBRSxDQUFDLEdBQUcsQ0FBQyxFQUFFLENBQUMsR0FBRyxDQUFDLDRCQUE0QixFQUFFLEVBQUUsVUFBVSxFQUFFLG9CQUFvQixDQUFDLGFBQWEsRUFBRSxDQUFDLENBQUM7aUJBQzFHLENBQUM7Z0JBQ0YsSUFBSSxHQUFHLENBQUMsZUFBZSxDQUFDO29CQUN0QixPQUFPLEVBQUUsQ0FBQyxjQUFjLENBQUM7b0JBQ3pCLFNBQVMsRUFBRSxDQUFDLEdBQUcsQ0FBQyxFQUFFLENBQUMsR0FBRyxDQUFDLDhCQUE4QixFQUFFLEVBQUUsVUFBVSxFQUFFLG9CQUFvQixDQUFDLGFBQWEsRUFBRSxDQUFDLENBQUM7aUJBQzVHLENBQUM7YUFDSDtTQUNGLENBQUMsQ0FBQztRQUVILE1BQU0sY0FBYyxHQUFHLElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsNkJBQTZCLEVBQUU7WUFDNUUsY0FBYyxFQUFFLGNBQWM7WUFDOUIsS0FBSyxFQUFFLENBQUMsZ0JBQWdCLENBQUMsSUFBSyxDQUFDLFFBQVEsQ0FBQztZQUN4QyxVQUFVLEVBQUUsK0JBQStCLEdBQUcsQ0FBQyxLQUFLLENBQUMsRUFBRSxDQUFDLElBQUksQ0FBQyxDQUFDLFNBQVMsRUFBRTtTQUMxRSxDQUFDLENBQUM7UUFDSCxjQUFjLENBQUMsVUFBVSxDQUFDLFNBQVMsR0FBRyxhQUFhLENBQUM7UUFFcEQscUNBQXFDO1FBQ3JDLGFBQWEsQ0FBQyxVQUFVLENBQUMsZ0JBQWdCLENBQUMsQ0FBQztRQUMzQyxNQUFNLENBQUMsbUJBQW1CLENBQUMsZ0JBQWdCLENBQUMsQ0FBQztRQUU3QyxrREFBa0Q7UUFDbEQsZ0JBQWdCLENBQUMsZUFBZSxDQUFDLElBQUksR0FBRyxDQUFDLGVBQWUsQ0FBQztZQUN2RCxPQUFPLEVBQUU7Z0JBQ1AsMEJBQTBCO2FBQzNCO1lBQ0QsU0FBUyxFQUFFLENBQUMsR0FBRyxDQUFDO1NBQ2pCLENBQUMsQ0FBQyxDQUFDO1FBRUosMEJBQTBCO1FBQzFCLE1BQU0sYUFBYSxHQUFHLElBQUksTUFBTSxDQUFDLFFBQVEsQ0FBQyxJQUFJLEVBQUUsaUJBQWlCLEVBQUU7WUFDakUsT0FBTyxFQUFFLE1BQU0sQ0FBQyxPQUFPLENBQUMsV0FBVztZQUNuQyxPQUFPLEVBQUUsc0JBQXNCO1lBQy9CLElBQUksRUFBRSxNQUFNLENBQUMsSUFBSSxDQUFDLFNBQVMsQ0FBQyx3QkFBd0IsQ0FBQztZQUNyRCxPQUFPLEVBQUUsR0FBRyxDQUFDLFFBQVEsQ0FBQyxPQUFPLENBQUMsQ0FBQyxDQUFDO1lBQ2hDLE9BQU8sRUFBRSxNQUFNLENBQUMsT0FBTyxDQUFDLE1BQU07WUFDOUIsV0FBVyxFQUFFO2dCQUNYLGNBQWMsRUFBRSxVQUFVLENBQUMsUUFBUTtnQkFDbkMsZUFBZSxFQUFFLGFBQWEsQ0FBQyxhQUFhO2dCQUM1QyxhQUFhLEVBQUUsaUJBQWlCLENBQUMsYUFBYTtnQkFDOUMsWUFBWSxFQUFFLGdCQUFnQixDQUFDLGFBQWE7YUFDN0M7U0FDRixDQUFDLENBQUM7UUFFSCxxQ0FBcUM7UUFDckMsTUFBTSxrQkFBa0IsR0FBRyxHQUFHLENBQUMsR0FBRyxDQUFDLE1BQU0sQ0FBQztZQUN4QyxPQUFPLEVBQUUsUUFBUTtZQUNqQixRQUFRLEVBQUUsV0FBVztZQUNyQixZQUFZLEVBQUUsU0FBUztTQUN4QixFQUFFLElBQUksQ0FBQyxDQUFDO1FBQ1QsTUFBTSxTQUFTLEdBQUcsR0FBRyxDQUFDLEdBQUcsQ0FBQyxNQUFNLENBQUM7WUFDL0IsT0FBTyxFQUFFLE1BQU07WUFDZixRQUFRLEVBQUUsVUFBVTtZQUNwQixZQUFZLEVBQUUsYUFBYSxDQUFDLGFBQWE7U0FDMUMsRUFBRSxJQUFJLENBQUMsQ0FBQztRQUNULE1BQU0sb0JBQW9CLEdBQUcsR0FBRyxDQUFDLEdBQUcsQ0FBQyxNQUFNLENBQUM7WUFDMUMsT0FBTyxFQUFFLE1BQU07WUFDZixRQUFRLEVBQUUsT0FBTztZQUNqQixZQUFZLEVBQUUsR0FBRyxhQUFhLENBQUMsYUFBYSxJQUFJO1NBQ2pELEVBQUUsSUFBSSxDQUFDLENBQUM7UUFFVCxhQUFhLENBQUMsZUFBZSxDQUFDLElBQUksR0FBRyxDQUFDLGVBQWUsQ0FBQztZQUNwRCxPQUFPLEVBQUUsQ0FBQyw0QkFBNEIsRUFBRSwwQkFBMEIsRUFBRSx3QkFBd0IsQ0FBQztZQUM3RixTQUFTLEVBQUUsQ0FBQyxrQkFBa0IsQ0FBQztTQUNoQyxDQUFDLENBQUMsQ0FBQztRQUNKLGFBQWEsQ0FBQyxlQUFlLENBQUMsSUFBSSxHQUFHLENBQUMsZUFBZSxDQUFDO1lBQ3BELE9BQU8sRUFBRSxDQUFDLGtCQUFrQixDQUFDO1lBQzdCLFNBQVMsRUFBRSxDQUFDLFNBQVMsQ0FBQztTQUN2QixDQUFDLENBQUMsQ0FBQztRQUNKLGFBQWEsQ0FBQyxlQUFlLENBQUMsSUFBSSxHQUFHLENBQUMsZUFBZSxDQUFDO1lBQ3BELE9BQU8sRUFBRSxDQUFDLGVBQWUsQ0FBQztZQUMxQixTQUFTLEVBQUUsQ0FBQyxvQkFBb0IsQ0FBQztTQUNsQyxDQUFDLENBQUMsQ0FBQztRQUVKLG1DQUFtQztRQUNuQyxNQUFNLGtCQUFrQixHQUFHLEdBQUcsQ0FBQyxFQUFFLENBQUMsS0FBSyxDQUFDLEdBQUcsRUFBRSxpQkFBaUIsQ0FBQyxhQUFhLENBQUMsQ0FBQztRQUM5RSxNQUFNLGtCQUFrQixHQUFHLEdBQUcsQ0FBQyxFQUFFLENBQUMsTUFBTSxDQUFDLENBQUMsRUFBRSxrQkFBa0IsQ0FBQyxDQUFDO1FBQ2hFLGFBQWEsQ0FBQyxlQUFlLENBQUMsSUFBSSxHQUFHLENBQUMsZUFBZSxDQUFDO1lBQ3BELE9BQU8sRUFBRSxDQUFDLGVBQWUsQ0FBQztZQUMxQixTQUFTLEVBQUUsQ0FBQyxHQUFHLENBQUMsR0FBRyxDQUFDLE1BQU0sQ0FBQyxFQUFFLE9BQU8sRUFBRSxJQUFJLEVBQUUsUUFBUSxFQUFFLGtCQUFrQixFQUFFLEVBQUUsSUFBSSxDQUFDLENBQUM7U0FDbkYsQ0FBQyxDQUFDLENBQUM7UUFDSixhQUFhLENBQUMsZUFBZSxDQUFDLElBQUksR0FBRyxDQUFDLGVBQWUsQ0FBQztZQUNwRCxPQUFPLEVBQUUsQ0FBQyxjQUFjLEVBQUUsY0FBYyxDQUFDO1lBQ3pDLFNBQVMsRUFBRSxDQUFDLEdBQUcsQ0FBQyxHQUFHLENBQUMsTUFBTSxDQUFDLEVBQUUsT0FBTyxFQUFFLElBQUksRUFBRSxRQUFRLEVBQUUsR0FBRyxrQkFBa0IsSUFBSSxFQUFFLEVBQUUsSUFBSSxDQUFDLENBQUM7U0FDMUYsQ0FBQyxDQUFDLENBQUM7UUFFSixVQUFVLENBQUMsaUJBQWlCLENBQUMsYUFBYSxDQUFDLENBQUM7UUFFNUMsb0NBQW9DO1FBQ3BDLE1BQU0sWUFBWSxHQUFHLElBQUksTUFBTSxDQUFDLElBQUksQ0FBQyxJQUFJLEVBQUUsY0FBYyxFQUFFO1lBQ3pELFdBQVcsRUFBRSxzQ0FBc0M7WUFDbkQsUUFBUSxFQUFFLE1BQU0sQ0FBQyxRQUFRLENBQUMsSUFBSSxDQUFDLEVBQUUsTUFBTSxFQUFFLEdBQUcsRUFBRSxJQUFJLEVBQUUsR0FBRyxFQUFFLENBQUM7U0FDM0QsQ0FBQyxDQUFDO1FBQ0gsWUFBWSxDQUFDLFNBQVMsQ0FBQyxJQUFJLE9BQU8sQ0FBQyxjQUFjLENBQUMsYUFBYSxDQUFDLENBQUMsQ0FBQztRQUVsRSxvREFBb0Q7UUFDcEQsTUFBTSx3QkFBd0IsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLHFCQUFxQixFQUFFO1lBQ2pGLElBQUksRUFBRSxRQUFRO1lBQ2QsT0FBTyxFQUFFLE9BQU87WUFDaEIsYUFBYSxFQUFFLENBQUMsTUFBTSxFQUFFLE9BQU8sQ0FBQztZQUNoQyxXQUFXLEVBQUUsb0RBQW9EO1NBQ2xFLENBQUMsQ0FBQztRQUNILE1BQU0sd0JBQXdCLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxxQkFBcUIsRUFBRTtZQUNqRixJQUFJLEVBQUUsUUFBUTtZQUNkLE9BQU8sRUFBRSxFQUFFO1lBQ1gsV0FBVyxFQUFFLHNDQUFzQztTQUNwRCxDQUFDLENBQUM7UUFDSCxNQUFNLHdCQUF3QixHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUscUJBQXFCLEVBQUU7WUFDakYsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsZ0JBQWdCO1lBQ3pCLFdBQVcsRUFBRSxzQ0FBc0M7U0FDcEQsQ0FBQyxDQUFDO1FBQ0gsTUFBTSx5QkFBeUIsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLHNCQUFzQixFQUFFO1lBQ25GLElBQUksRUFBRSxRQUFRO1lBQ2QsT0FBTyxFQUFFLENBQUM7WUFDVixXQUFXLEVBQUUsMENBQTBDO1NBQ3hELENBQUMsQ0FBQztRQUNILE1BQU0sY0FBYyxHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsc0JBQXNCLEVBQUU7WUFDeEUsVUFBVSxFQUFFLEdBQUcsQ0FBQyxFQUFFLENBQUMsZUFBZSxDQUFDLHdCQUF3QixDQUFDLGFBQWEsRUFBRSxNQUFNLENBQUM7U0FDbkYsQ0FBQyxDQUFDO1FBRUgsTUFBTSxhQUFhLEdBQUcsSUFBSSxNQUFNLENBQUMsUUFBUSxDQUFDLElBQUksRUFBRSxzQkFBc0IsRUFBRTtZQUN0RSxPQUFPLEVBQUUsTUFBTSxDQUFDLE9BQU8sQ0FBQyxXQUFXO1lBQ25DLE9BQU8sRUFBRSw2QkFBNkI7WUFDdEMsSUFBSSxFQUFFLE1BQU0sQ0FBQyxJQUFJLENBQUMsU0FBUyxDQUFDLFlBQVksQ0FBQztZQUN6QyxPQUFPLEVBQUUsR0FBRyxDQUFDLFFBQVEsQ0FBQyxPQUFPLENBQUMsQ0FBQyxDQUFDO1lBQ2hDLFdBQVcsRUFBRTtnQkFDWCxVQUFVLEVBQUUsR0FBRyxDQUFDLEtBQUssQ0FBQyxFQUFFLENBQUMsSUFBSSxDQUFDLENBQUMsTUFBTTthQUN0QztTQUNGLENBQUMsQ0FBQztRQUNGLGFBQWEsQ0FBQyxJQUFJLENBQUMsWUFBbUMsQ0FBQyxVQUFVLENBQUMsU0FBUyxHQUFHLGNBQWMsQ0FBQztRQUU5RixhQUFhLENBQUMsZUFBZSxDQUFDLElBQUksR0FBRyxDQUFDLGVBQWUsQ0FBQztZQUNwRCxPQUFPLEVBQUUsQ0FBQyxlQUFlLEVBQUUsaUJBQWlCLEVBQUUsd0JBQXdCLENBQUM7WUFDdkUsU0FBUyxFQUFFO2dCQUNULEdBQUcsQ0FBQyxHQUFHLENBQUMsTUFBTSxDQUFDLEVBQUUsT0FBTyxFQUFFLElBQUksRUFBRSxRQUFRLEVBQUUsd0JBQXdCLENBQUMsYUFBYSxFQUFFLEVBQUUsSUFBSSxDQUFDO2dCQUN6RixHQUFHLENBQUMsR0FBRyxDQUFDLE1BQU0sQ0FBQyxFQUFFLE9BQU8sRUFBRSxJQUFJLEVBQUUsUUFBUSxFQUFFLEdBQUcsd0JBQXdCLENBQUMsYUFBYSxJQUFJLEVBQUUsRUFBRSxJQUFJLENBQUM7YUFDakc7U0FDRixDQUFDLENBQUMsQ0FBQztRQUVKLE1BQU0sV0FBVyxHQUFHLElBQUksTUFBTSxDQUFDLElBQUksQ0FBQyxJQUFJLEVBQUUsdUJBQXVCLEVBQUU7WUFDakUsV0FBVyxFQUFFLGtDQUFrQztZQUMvQyxRQUFRLEVBQUUsTUFBTSxDQUFDLFFBQVEsQ0FBQyxJQUFJLENBQUMsRUFBRSxNQUFNLEVBQUUsR0FBRyxFQUFFLElBQUksRUFBRSxHQUFHLEVBQUUsQ0FBQztTQUMzRCxDQUFDLENBQUM7UUFDRixXQUFXLENBQUMsSUFBSSxDQUFDLFlBQStCLENBQUMsVUFBVSxDQUFDLFNBQVMsR0FBRyxjQUFjLENBQUM7UUFDeEYsV0FBVyxDQUFDLFNBQVMsQ0FBQyxJQUFJLE9BQU8sQ0FBQyxjQUFjLENBQUMsYUFBYSxFQUFFO1lBQzlELEtBQUssRUFBRSxNQUFNLENBQUMsZUFBZSxDQUFDLFVBQVUsQ0FBQztnQkFDdkMsTUFBTSxFQUFFLHdCQUF3QixDQUFDLGFBQWE7Z0JBQzlDLE1BQU0sRUFBRSx3QkFBd0IsQ0FBQyxhQUFhO2dCQUM5QyxRQUFRLEVBQUUseUJBQXlCLENBQUMsYUFBYTthQUNsRCxDQUFDO1NBQ0gsQ0FBQyxDQUFDLENBQUM7UUFFSix3QkFBd0I7UUFDeEIsTUFBTSxtQkFBbUIsR0FBRyxJQUFJLE1BQU0sQ0FBQyxRQUFRLENBQUMsSUFBSSxFQUFFLHVCQUF1QixFQUFFO1lBQzdFLE9BQU8sRUFBRSxNQUFNLENBQUMsT0FBTyxDQUFDLFdBQVc7WUFDbkMsT0FBTyxFQUFFLHNCQUFzQjtZQUMvQixJQUFJLEVBQUUsTUFBTSxDQUFDLElBQUksQ0FBQyxTQUFTLENBQUMsMEJBQTBCLENBQUM7WUFDdkQsT0FBTyxFQUFFLEdBQUcsQ0FBQyxRQUFRLENBQUMsT0FBTyxDQUFDLENBQUMsQ0FBQztZQUNoQyxPQUFPLEVBQUUsTUFBTSxDQUFDLE9BQU8sQ0FBQyxNQUFNO1lBQzlCLFdBQVcsRUFBRSxFQUFFLGNBQWMsRUFBRSxVQUFVLENBQUMsUUFBUSxFQUFFO1NBQ3JELENBQUMsQ0FBQztRQUNILFVBQVUsQ0FBQyxpQkFBaUIsQ0FBQyxtQkFBbUIsQ0FBQyxDQUFDO1FBRWxELHVCQUF1QjtRQUN2QixNQUFNLFdBQVcsR0FBRyxJQUFJLEdBQUcsQ0FBQyxLQUFLLENBQUMsSUFBSSxFQUFFLGFBQWEsRUFBRTtZQUNyRCxXQUFXLEVBQUUsOEJBQThCO1NBQzVDLENBQUMsQ0FBQztRQUVILDRCQUE0QjtRQUM1QixNQUFNLG1CQUFtQixHQUFHLElBQUksTUFBTSxDQUFDLFFBQVEsQ0FBQyxJQUFJLEVBQUUsdUJBQXVCLEVBQUU7WUFDN0UsT0FBTyxFQUFFLE1BQU0sQ0FBQyxPQUFPLENBQUMsV0FBVztZQUNuQyxPQUFPLEVBQUUsc0JBQXNCO1lBQy9CLElBQUksRUFBRSxNQUFNLENBQUMsSUFBSSxDQUFDLFNBQVMsQ0FBQywwQkFBMEIsQ0FBQztZQUN2RCxPQUFPLEVBQUUsR0FBRyxDQUFDLFFBQVEsQ0FBQyxPQUFPLENBQUMsRUFBRSxDQUFDO1lBQ2pDLE9BQU8sRUFBRSxNQUFNLENBQUMsT0FBTyxDQUFDLE1BQU07WUFDOUIsV0FBVyxFQUFFO2dCQUNYLGlCQUFpQixFQUFFLGlCQUFpQixDQUFDLGFBQWE7YUFDbkQ7U0FDRixDQUFDLENBQUM7UUFFSCx5Q0FBeUM7UUFDekMsTUFBTSxvQkFBb0IsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxFQUFFLHNCQUFzQixFQUFFO1lBQzlFLFVBQVUsRUFBRSxHQUFHLENBQUMsRUFBRSxDQUFDLFlBQVksQ0FBQyxHQUFHLENBQUMsRUFBRSxDQUFDLGVBQWUsQ0FBQyxpQkFBaUIsQ0FBQyxhQUFhLEVBQUUsRUFBRSxDQUFDLENBQUM7U0FDN0YsQ0FBQyxDQUFDO1FBRUgscURBQXFEO1FBQ3JELG1CQUFtQixDQUFDLGFBQWEsQ0FBQyxnQkFBZ0IsRUFBRTtZQUNsRCxTQUFTLEVBQUUsSUFBSSxHQUFHLENBQUMsZ0JBQWdCLENBQUMsbUJBQW1CLENBQUM7WUFDeEQsU0FBUyxFQUFFLFdBQVcsQ0FBQyxRQUFRO1NBQ2hDLENBQUMsQ0FBQztRQUVILGlEQUFpRDtRQUNqRCxNQUFNLGlCQUFpQixHQUFHLElBQUksR0FBRyxDQUFDLGVBQWUsQ0FBQyxJQUFJLEVBQUUsMkJBQTJCLEVBQUU7WUFDbkYsUUFBUSxFQUFFLFdBQVcsQ0FBQyxRQUFRO1lBQzlCLFFBQVEsRUFBRSxRQUFRO1lBQ2xCLFFBQVEsRUFBRSxtQkFBbUIsQ0FBQyxXQUFXO1NBQzFDLENBQUMsQ0FBQztRQUNILGlCQUFpQixDQUFDLFVBQVUsQ0FBQyxTQUFTLEdBQUcsb0JBQW9CLENBQUM7UUFFOUQsb0JBQW9CO1FBQ3BCLE1BQU0sYUFBYSxHQUFHLElBQUksRUFBRSxDQUFDLEtBQUssQ0FBQyxJQUFJLEVBQUUsZUFBZSxFQUFFO1lBQ3hELE1BQU0sRUFBRSxHQUFHLENBQUMsd0NBQXdDLEVBQUU7WUFDdEQsU0FBUyxFQUFFLENBQUM7WUFDWixpQkFBaUIsRUFBRSxDQUFDO1lBQ3BCLGtCQUFrQixFQUFFLEVBQUUsQ0FBQyxrQkFBa0IsQ0FBQyxrQ0FBa0M7U0FDN0UsQ0FBQyxDQUFDO1FBRUgsTUFBTSxxQkFBcUIsR0FBRyxJQUFJLEVBQUUsQ0FBQyxLQUFLLENBQUMsSUFBSSxFQUFFLHVCQUF1QixFQUFFO1lBQ3hFLE1BQU0sRUFBRSxnQkFBZ0IsQ0FBQyxZQUFZLEVBQUU7WUFDdkMsU0FBUyxFQUFFLEVBQUU7WUFDYixpQkFBaUIsRUFBRSxDQUFDO1lBQ3BCLGtCQUFrQixFQUFFLEVBQUUsQ0FBQyxrQkFBa0IsQ0FBQyxzQkFBc0I7U0FDakUsQ0FBQyxDQUFDO1FBRUgsTUFBTSxlQUFlLEdBQUcsSUFBSSxFQUFFLENBQUMsS0FBSyxDQUFDLElBQUksRUFBRSxpQkFBaUIsRUFBRTtZQUM1RCxNQUFNLEVBQUUsVUFBVSxDQUFDLHdDQUF3QyxFQUFFO1lBQzdELFNBQVMsRUFBRSxJQUFJO1lBQ2YsaUJBQWlCLEVBQUUsQ0FBQztZQUNwQixrQkFBa0IsRUFBRSxFQUFFLENBQUMsa0JBQWtCLENBQUMsc0JBQXNCO1NBQ2pFLENBQUMsQ0FBQztRQUVILE1BQU0sYUFBYSxHQUFHLElBQUksRUFBRSxDQUFDLEtBQUssQ0FBQyxJQUFJLEVBQUUsZUFBZSxFQUFFO1lBQ3hELE1BQU0sRUFBRSxVQUFVLENBQUMsbUNBQW1DLEVBQUU7WUFDeEQsU0FBUyxFQUFFLEdBQUc7WUFDZCxpQkFBaUIsRUFBRSxDQUFDO1lBQ3BCLGtCQUFrQixFQUFFLEVBQUUsQ0FBQyxrQkFBa0IsQ0FBQyxzQkFBc0I7U0FDakUsQ0FBQyxDQUFDO1FBRUgseUVBQXlFO1FBQ3pFLE1BQU0sMEJBQTBCLEdBQUcsSUFBSSxFQUFFLENBQUMsTUFBTSxDQUFDO1lBQy9DLFNBQVMsRUFBRSxZQUFZO1lBQ3ZCLFVBQVUsRUFBRSxzQkFBc0I7WUFDbEMsYUFBYSxFQUFFLEVBQUUsWUFBWSxFQUFFLGdCQUFnQixDQUFDLFlBQVksRUFBRTtZQUM5RCxTQUFTLEVBQUUsU0FBUztZQUNwQixNQUFNLEVBQUUsR0FBRyxDQUFDLFFBQVEsQ0FBQyxPQUFPLENBQUMsQ0FBQyxDQUFDO1NBQ2hDLENBQUMsQ0FBQztRQUNILE1BQU0saUJBQWlCLEdBQUcsSUFBSSxFQUFFLENBQUMsY0FBYyxDQUFDO1lBQzlDLFVBQVUsRUFBRSx3Q0FBd0M7WUFDcEQsWUFBWSxFQUFFO2dCQUNaLEtBQUssRUFBRSxVQUFVLENBQUMsd0NBQXdDLENBQUM7b0JBQ3pELFNBQVMsRUFBRSxTQUFTO29CQUNwQixNQUFNLEVBQUUsR0FBRyxDQUFDLFFBQVEsQ0FBQyxPQUFPLENBQUMsQ0FBQyxDQUFDO2lCQUNoQyxDQUFDO2dCQUNGLFVBQVUsRUFBRSwwQkFBMEI7YUFDdkM7U0FDRixDQUFDLENBQUM7UUFDSCxNQUFNLGtCQUFrQixHQUFHLElBQUksRUFBRSxDQUFDLEtBQUssQ0FBQyxJQUFJLEVBQUUsb0JBQW9CLEVBQUU7WUFDbEUsU0FBUyxFQUFFLHVCQUF1QjtZQUNsQyxnQkFBZ0IsRUFBRSw4RUFBOEU7WUFDaEcsTUFBTSxFQUFFLGlCQUFpQjtZQUN6QixTQUFTLEVBQUUsQ0FBQztZQUNaLGlCQUFpQixFQUFFLENBQUM7WUFDcEIsa0JBQWtCLEVBQUUsRUFBRSxDQUFDLGtCQUFrQixDQUFDLGtDQUFrQztZQUM1RSxnQkFBZ0IsRUFBRSxFQUFFLENBQUMsZ0JBQWdCLENBQUMsYUFBYTtTQUNwRCxDQUFDLENBQUM7UUFFSCxNQUFNLFdBQVcsR0FBRyxJQUFJLFNBQVMsQ0FBQyxTQUFTLENBQUMsV0FBVyxDQUFDLENBQUM7UUFFekQsYUFBYSxDQUFDLGNBQWMsQ0FBQyxXQUFXLENBQUMsQ0FBQztRQUMxQyxxQkFBcUIsQ0FBQyxjQUFjLENBQUMsV0FBVyxDQUFDLENBQUM7UUFDbEQsZUFBZSxDQUFDLGNBQWMsQ0FBQyxXQUFXLENBQUMsQ0FBQztRQUM1QyxrQkFBa0IsQ0FBQyxjQUFjLENBQUMsV0FBVyxDQUFDLENBQUM7UUFDL0MsYUFBYSxDQUFDLGNBQWMsQ0FBQyxXQUFXLENBQUMsQ0FBQztRQUUxQyx1QkFBdUI7UUFDdkIsTUFBTSxTQUFTLEdBQUcsSUFBSSxFQUFFLENBQUMsU0FBUyxDQUFDLElBQUksRUFBRSw2QkFBNkIsRUFBRTtZQUN0RSxhQUFhLEVBQUUscUJBQXFCO1NBQ3JDLENBQUMsQ0FBQztRQUVILFNBQVMsQ0FBQyxVQUFVLENBQ2xCLElBQUksRUFBRSxDQUFDLFdBQVcsQ0FBQztZQUNqQixLQUFLLEVBQUUsbUJBQW1CO1lBQzFCLElBQUksRUFBRSxDQUFDLFVBQVUsQ0FBQyx3Q0FBd0MsRUFBRSxDQUFDO1lBQzdELEtBQUssRUFBRSxDQUFDLFVBQVUsQ0FBQyxtQ0FBbUMsRUFBRSxDQUFDO1lBQ3pELEtBQUssRUFBRSxFQUFFO1NBQ1YsQ0FBQyxFQUNGLElBQUksRUFBRSxDQUFDLFdBQVcsQ0FBQztZQUNqQixLQUFLLEVBQUUsMkJBQTJCO1lBQ2xDLElBQUksRUFBRSxDQUFDLGdCQUFnQixDQUFDLGlCQUFpQixFQUFFLEVBQUUsZ0JBQWdCLENBQUMsY0FBYyxFQUFFLENBQUM7WUFDL0UsS0FBSyxFQUFFLENBQUMsZ0JBQWdCLENBQUMsWUFBWSxFQUFFLEVBQUUsZ0JBQWdCLENBQUMsZUFBZSxFQUFFLENBQUM7WUFDNUUsS0FBSyxFQUFFLEVBQUU7U0FDVixDQUFDLEVBQ0YsSUFBSSxFQUFFLENBQUMsV0FBVyxDQUFDO1lBQ2pCLEtBQUssRUFBRSxvQkFBb0I7WUFDM0IsSUFBSSxFQUFFLENBQUMsMEJBQTBCLENBQUM7WUFDbEMsS0FBSyxFQUFFLEVBQUU7U0FDVixDQUFDLEVBQ0YsSUFBSSxFQUFFLENBQUMsV0FBVyxDQUFDO1lBQ2pCLEtBQUssRUFBRSxXQUFXO1lBQ2xCLElBQUksRUFBRSxDQUFDLEdBQUcsQ0FBQyx3Q0FBd0MsRUFBRSxDQUFDO1lBQ3RELEtBQUssRUFBRSxFQUFFO1NBQ1YsQ0FBQyxFQUNGLElBQUksRUFBRSxDQUFDLFdBQVcsQ0FBQztZQUNqQixLQUFLLEVBQUUseUJBQXlCO1lBQ2hDLElBQUksRUFBRTtnQkFDSixJQUFJLEVBQUUsQ0FBQyxNQUFNLENBQUM7b0JBQ1osU0FBUyxFQUFFLFlBQVk7b0JBQ3ZCLFVBQVUsRUFBRSxlQUFlO29CQUMzQixTQUFTLEVBQUUsS0FBSztpQkFDakIsQ0FBQztnQkFDRixJQUFJLEVBQUUsQ0FBQyxNQUFNLENBQUM7b0JBQ1osU0FBUyxFQUFFLFlBQVk7b0JBQ3ZCLFVBQVUsRUFBRSxZQUFZO29CQUN4QixTQUFTLEVBQUUsS0FBSztpQkFDakIsQ0FBQzthQUNIO1lBQ0QsS0FBSyxFQUFFLEVBQUU7U0FDVixDQUFDLEVBQ0YsSUFBSSxFQUFFLENBQUMsV0FBVyxDQUFDO1lBQ2pCLEtBQUssRUFBRSxxQkFBcUI7WUFDNUIsSUFBSSxFQUFFO2dCQUNKLElBQUksRUFBRSxDQUFDLE1BQU0sQ0FBQyxFQUFFLFNBQVMsRUFBRSxZQUFZLEVBQUUsVUFBVSxFQUFFLGdCQUFnQixFQUFFLFNBQVMsRUFBRSxTQUFTLEVBQUUsQ0FBQzthQUMvRjtZQUNELEtBQUssRUFBRTtnQkFDTCxJQUFJLEVBQUUsQ0FBQyxNQUFNLENBQUMsRUFBRSxTQUFTLEVBQUUsWUFBWSxFQUFFLFVBQVUsRUFBRSxnQkFBZ0IsRUFBRSxTQUFTLEVBQUUsS0FBSyxFQUFFLENBQUM7YUFDM0Y7WUFDRCxLQUFLLEVBQUUsRUFBRTtTQUNWLENBQUMsRUFDRixJQUFJLEVBQUUsQ0FBQyxXQUFXLENBQUM7WUFDakIsS0FBSyxFQUFFLDBCQUEwQjtZQUNqQyxJQUFJLEVBQUU7Z0JBQ0osSUFBSSxFQUFFLENBQUMsTUFBTSxDQUFDLEVBQUUsU0FBUyxFQUFFLGlCQUFpQixFQUFFLFVBQVUsRUFBRSxrQkFBa0IsRUFBRSxTQUFTLEVBQUUsS0FBSyxFQUFFLENBQUM7YUFDbEc7WUFDRCxLQUFLLEVBQUUsRUFBRTtTQUNWLENBQUMsRUFDRixJQUFJLEVBQUUsQ0FBQyxXQUFXLENBQUM7WUFDakIsS0FBSyxFQUFFLHlCQUF5QjtZQUNoQyxJQUFJLEVBQUU7Z0JBQ0osSUFBSSxFQUFFLENBQUMsTUFBTSxDQUFDLEVBQUUsU0FBUyxFQUFFLGdCQUFnQixFQUFFLFVBQVUsRUFBRSxpQkFBaUIsRUFBRSxTQUFTLEVBQUUsS0FBSyxFQUFFLENBQUM7YUFDaEc7WUFDRCxLQUFLLEVBQUUsRUFBRTtTQUNWLENBQUMsQ0FDSCxDQUFDO1FBRUYsVUFBVTtRQUNWLElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsZUFBZSxFQUFFO1lBQ3ZDLEtBQUssRUFBRSxVQUFVLENBQUMsUUFBUTtZQUMxQixXQUFXLEVBQUUscUJBQXFCO1NBQ25DLENBQUMsQ0FBQztRQUVILElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUscUJBQXFCLEVBQUU7WUFDN0MsS0FBSyxFQUFFLGdCQUFnQixDQUFDLFdBQVc7WUFDbkMsV0FBVyxFQUFFLGdDQUFnQztTQUM5QyxDQUFDLENBQUM7UUFFSCxJQUFJLEdBQUcsQ0FBQyxTQUFTLENBQUMsSUFBSSxFQUFFLG1CQUFtQixFQUFFO1lBQzNDLEtBQUssRUFBRSxhQUFhLENBQUMsVUFBVTtZQUMvQixXQUFXLEVBQUUsd0JBQXdCO1NBQ3RDLENBQUMsQ0FBQztRQUVILElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsMkJBQTJCLEVBQUU7WUFDbkQsS0FBSyxFQUFFLG1CQUFtQixDQUFDLFlBQVk7WUFDdkMsV0FBVyxFQUFFLHdEQUF3RDtTQUN0RSxDQUFDLENBQUM7UUFFSCxJQUFJLEdBQUcsQ0FBQyxTQUFTLENBQUMsSUFBSSxFQUFFLGNBQWMsRUFBRTtZQUN0QyxLQUFLLEVBQUUsV0FBVyxJQUFJLENBQUMsTUFBTSxrREFBa0QsSUFBSSxDQUFDLE1BQU0sb0JBQW9CLFNBQVMsQ0FBQyxhQUFhLEVBQUU7WUFDdkksV0FBVyxFQUFFLDBCQUEwQjtTQUN4QyxDQUFDLENBQUM7SUFDTCxDQUFDO0NBQ0Y7QUF4cUJELDREQXdxQkMiLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgKiBhcyBjZGsgZnJvbSAnYXdzLWNkay1saWInO1xuaW1wb3J0ICogYXMgbGFtYmRhIGZyb20gJ2F3cy1jZGstbGliL2F3cy1sYW1iZGEnO1xuaW1wb3J0ICogYXMgZWMyIGZyb20gJ2F3cy1jZGstbGliL2F3cy1lYzInO1xuaW1wb3J0ICogYXMgc3FzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zcXMnO1xuaW1wb3J0ICogYXMgczMgZnJvbSAnYXdzLWNkay1saWIvYXdzLXMzJztcbmltcG9ydCAqIGFzIGlhbSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtaWFtJztcbmltcG9ydCAqIGFzIGV2ZW50cyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZXZlbnRzJztcbmltcG9ydCAqIGFzIHRhcmdldHMgZnJvbSAnYXdzLWNkay1saWIvYXdzLWV2ZW50cy10YXJnZXRzJztcbmltcG9ydCAqIGFzIHNvdXJjZXMgZnJvbSAnYXdzLWNkay1saWIvYXdzLWxhbWJkYS1ldmVudC1zb3VyY2VzJztcbmltcG9ydCAqIGFzIGttcyBmcm9tICdhd3MtY2RrLWxpYi9hd3Mta21zJztcbmltcG9ydCAqIGFzIGxvZ3MgZnJvbSAnYXdzLWNkay1saWIvYXdzLWxvZ3MnO1xuaW1wb3J0ICogYXMgc3NtIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zc20nO1xuaW1wb3J0ICogYXMgY3cgZnJvbSAnYXdzLWNkay1saWIvYXdzLWNsb3Vkd2F0Y2gnO1xuaW1wb3J0ICogYXMgc25zIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zbnMnO1xuaW1wb3J0ICogYXMgc25zX3N1YnNjcmlwdGlvbnMgZnJvbSAnYXdzLWNkay1saWIvYXdzLXNucy1zdWJzY3JpcHRpb25zJztcbmltcG9ydCAqIGFzIGN3YWN0aW9ucyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtY2xvdWR3YXRjaC1hY3Rpb25zJztcbmltcG9ydCAqIGFzIGNyIGZyb20gJ2F3cy1jZGstbGliL2N1c3RvbS1yZXNvdXJjZXMnO1xuaW1wb3J0IHsgQ29uc3RydWN0IH0gZnJvbSAnY29uc3RydWN0cyc7XG5cbmV4cG9ydCBjbGFzcyBBbWlyYUxhbWJkYVBhcmFsbGVsU3RhY2sgZXh0ZW5kcyBjZGsuU3RhY2sge1xuICBjb25zdHJ1Y3RvcihzY29wZTogQ29uc3RydWN0LCBpZDogc3RyaW5nLCBwcm9wcz86IGNkay5TdGFja1Byb3BzKSB7XG4gICAgc3VwZXIoc2NvcGUsIGlkLCBwcm9wcyk7XG5cbiAgICAvLyBQYXJhbWV0ZXJzIGZvciBjb25maWd1cmF0aW9uXG4gICAgY29uc3QgYXRoZW5hRGJQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFEYXRhYmFzZScsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2RlZmF1bHQnLFxuICAgICAgZGVzY3JpcHRpb246ICdBdGhlbmEgZGF0YWJhc2UgbmFtZSdcbiAgICB9KTtcbiAgICBjb25zdCBhdGhlbmFPdXRwdXRQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFPdXRwdXQnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdzMzovL2F0aGVuYS1xdWVyeS1yZXN1bHRzLycsXG4gICAgICBkZXNjcmlwdGlvbjogJ0F0aGVuYSBxdWVyeSBvdXRwdXQgUzMgcGF0aCdcbiAgICB9KTtcbiAgICBjb25zdCBhdGhlbmFRdWVyeVBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYVF1ZXJ5Jywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnU0VMRUNUIGFjdGl2aXR5X2lkIEZST00gYWN0aXZpdGllcyBXSEVSRSBwcm9jZXNzX2ZsYWcgPSAxJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnQXRoZW5hIFNRTCB0byBwcm9kdWNlIGFjdGl2aXR5IElEcydcbiAgICB9KTtcbiAgICBjb25zdCBtb2RlbFBhdGhQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdNb2RlbFBhdGgnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdmYWNlYm9vay93YXYydmVjMi1iYXNlLTk2MGgnLFxuICAgICAgZGVzY3JpcHRpb246ICdIRiBtb2RlbCBwYXRoIGZvciBXYXYyVmVjMidcbiAgICB9KTtcbiAgICBjb25zdCByZXN1bHRzUHJlZml4UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnUmVzdWx0c1ByZWZpeCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3Jlc3VsdHMvJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnUzMga2V5IHByZWZpeCBmb3IgcmVzdWx0cyB3cml0ZXMnXG4gICAgfSk7XG4gICAgY29uc3QgYXVkaW9CdWNrZXROYW1lUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXVkaW9CdWNrZXROYW1lJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnT3B0aW9uYWwgUzMgYnVja2V0IG5hbWUgZm9yIGlucHV0IGF1ZGlvIChyZWFkLW9ubHkpLiBMZWF2ZSBibGFuayB0byBza2lwLidcbiAgICB9KTtcbiAgICBjb25zdCBzbGFja1dlYmhvb2tQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdTbGFja1dlYmhvb2tVcmwnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdTbGFjayB3ZWJob29rIFVSTCBmb3Igam9iIGNvbXBsZXRpb24gYW5kIGVycm9yIG5vdGlmaWNhdGlvbnMnLFxuICAgICAgbm9FY2hvOiB0cnVlXG4gICAgfSk7XG4gICAgY29uc3QgdHJpdG9uQ2x1c3RlclVybFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1RyaXRvbkNsdXN0ZXJVcmwnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBUcml0b24gR1BVIGNsdXN0ZXIgVVJMIGZvciByZW1vdGUgaW5mZXJlbmNlLiBMZWF2ZSBibGFuayB0byBhdXRvLXJlc29sdmUgZnJvbSBTU00gcGFyYW1ldGVyIC9hbWlyYS90cml0b25fYWxiX3VybC4nXG4gICAgfSk7XG5cbiAgICBjb25zdCBlbmFibGVUcml0b25QYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdFbmFibGVUcml0b24nLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdmYWxzZScsXG4gICAgICBhbGxvd2VkVmFsdWVzOiBbJ3RydWUnLCAnZmFsc2UnXSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRW5hYmxlIGNhbGxpbmcgYSBUcml0b24gY2x1c3RlciAoaW50ZXJuYWwgQUxCKSBmcm9tIHRoZSBwcm9jZXNzaW5nIExhbWJkYSdcbiAgICB9KTtcblxuICAgIC8vIE9wdGlvbmFsIFZQQyBhdHRhY2htZW50IGZvciBjYWxsaW5nIGludGVybmFsIEFMQiBkaXJlY3RseVxuICAgIGNvbnN0IHZwY0lkUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnVnBjSWQnLCB7XG4gICAgICB0eXBlOiAnQVdTOjpFQzI6OlZQQzo6SWQnLFxuICAgICAgZGVmYXVsdDogY2RrLkF3cy5OT19WQUxVRSBhcyBhbnksXG4gICAgICBkZXNjcmlwdGlvbjogJ1ZQQyBJRCB0byBhdHRhY2ggdGhlIHByb2Nlc3NpbmcgTGFtYmRhIHRvIChmb3IgaW50ZXJuYWwgQUxCIGFjY2VzcyknXG4gICAgfSk7XG4gICAgY29uc3QgcHJpdmF0ZVN1Ym5ldElkc0NzdlBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1ByaXZhdGVTdWJuZXRJZHNDc3YnLCB7XG4gICAgICB0eXBlOiAnTGlzdDxBV1M6OkVDMjo6U3VibmV0OjpJZD4nLFxuICAgICAgZGVzY3JpcHRpb246ICdQcml2YXRlIHN1Ym5ldCBJRHMgZm9yIHRoZSBMYW1iZGEgVlBDIGNvbmZpZydcbiAgICB9KTtcbiAgICBjb25zdCBsYW1iZGFTZWN1cml0eUdyb3VwSWRQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdMYW1iZGFTZWN1cml0eUdyb3VwSWQnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6IGNkay5Bd3MuTk9fVkFMVUUgYXMgYW55LFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBleGlzdGluZyBTZWN1cml0eSBHcm91cCBJRCBmb3IgdGhlIHByb2Nlc3NpbmcgTGFtYmRhJ1xuICAgIH0pO1xuICAgIGNvbnN0IHZwY1Byb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ1ZwY1Byb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHZwY0lkUGFyYW0udmFsdWVBc1N0cmluZywgJycpKVxuICAgIH0pO1xuICAgIGNvbnN0IGxhbWJkYVNnUHJvdmlkZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnTGFtYmRhU2dQcm92aWRlZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25Ob3QoY2RrLkZuLmNvbmRpdGlvbkVxdWFscyhsYW1iZGFTZWN1cml0eUdyb3VwSWRQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpXG4gICAgfSk7XG5cbiAgICAvLyBLTVMga2V5IGZvciBlbmNyeXB0aW9uXG4gICAgY29uc3Qga21zS2V5ID0gbmV3IGttcy5LZXkodGhpcywgJ0FtaXJhUGFyYWxsZWxLZXknLCB7XG4gICAgICBlbmFibGVLZXlSb3RhdGlvbjogdHJ1ZSxcbiAgICAgIGFsaWFzOiAnYWxpYXMvYW1pcmEtbGFtYmRhLXBhcmFsbGVsJ1xuICAgIH0pO1xuXG4gICAgLy8gUmVzdWx0cyBidWNrZXRcbiAgICBjb25zdCBhY2Nlc3NMb2dzQnVja2V0ID0gbmV3IHMzLkJ1Y2tldCh0aGlzLCAnTGFtYmRhQWNjZXNzTG9nc0J1Y2tldCcsIHtcbiAgICAgIHZlcnNpb25lZDogZmFsc2UsXG4gICAgICBibG9ja1B1YmxpY0FjY2VzczogczMuQmxvY2tQdWJsaWNBY2Nlc3MuQkxPQ0tfQUxMLFxuICAgICAgZW5jcnlwdGlvbjogczMuQnVja2V0RW5jcnlwdGlvbi5TM19NQU5BR0VELFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZSxcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNkay5SZW1vdmFsUG9saWN5LlJFVEFJTlxuICAgIH0pO1xuXG4gICAgY29uc3QgcmVzdWx0c0J1Y2tldCA9IG5ldyBzMy5CdWNrZXQodGhpcywgJ1Jlc3VsdHNCdWNrZXQnLCB7XG4gICAgICB2ZXJzaW9uZWQ6IGZhbHNlLFxuICAgICAgYmxvY2tQdWJsaWNBY2Nlc3M6IHMzLkJsb2NrUHVibGljQWNjZXNzLkJMT0NLX0FMTCxcbiAgICAgIGVuY3J5cHRpb246IHMzLkJ1Y2tldEVuY3J5cHRpb24uS01TLFxuICAgICAgZW5jcnlwdGlvbktleToga21zS2V5LFxuICAgICAgYnVja2V0S2V5RW5hYmxlZDogdHJ1ZSxcbiAgICAgIHNlcnZlckFjY2Vzc0xvZ3NCdWNrZXQ6IGFjY2Vzc0xvZ3NCdWNrZXQsXG4gICAgICBzZXJ2ZXJBY2Nlc3NMb2dzUHJlZml4OiAnczMtYWNjZXNzLWxvZ3MvJyxcbiAgICAgIGVuZm9yY2VTU0w6IHRydWUsXG4gICAgICBsaWZlY3ljbGVSdWxlczogW1xuICAgICAgICB7XG4gICAgICAgICAgaWQ6ICdJbnRlbGxpZ2VudFRpZXJpbmdOb3cnLFxuICAgICAgICAgIGVuYWJsZWQ6IHRydWUsXG4gICAgICAgICAgdHJhbnNpdGlvbnM6IFtcbiAgICAgICAgICAgIHsgc3RvcmFnZUNsYXNzOiBzMy5TdG9yYWdlQ2xhc3MuSU5URUxMSUdFTlRfVElFUklORywgdHJhbnNpdGlvbkFmdGVyOiBjZGsuRHVyYXRpb24uZGF5cygwKSB9XG4gICAgICAgICAgXVxuICAgICAgICB9LFxuICAgICAgICB7XG4gICAgICAgICAgaWQ6ICdUcmFuc2l0aW9uVG9JQTMwZCcsXG4gICAgICAgICAgZW5hYmxlZDogdHJ1ZSxcbiAgICAgICAgICB0cmFuc2l0aW9uczogW1xuICAgICAgICAgICAgeyBzdG9yYWdlQ2xhc3M6IHMzLlN0b3JhZ2VDbGFzcy5JTkZSRVFVRU5UX0FDQ0VTUywgdHJhbnNpdGlvbkFmdGVyOiBjZGsuRHVyYXRpb24uZGF5cygzMCkgfVxuICAgICAgICAgIF1cbiAgICAgICAgfSxcbiAgICAgICAge1xuICAgICAgICAgIGlkOiAnVHJhbnNpdGlvblRvR2xhY2llcjEyMGQnLFxuICAgICAgICAgIGVuYWJsZWQ6IHRydWUsXG4gICAgICAgICAgdHJhbnNpdGlvbnM6IFtcbiAgICAgICAgICAgIHsgc3RvcmFnZUNsYXNzOiBzMy5TdG9yYWdlQ2xhc3MuR0xBQ0lFUl9JTlNUQU5UX1JFVFJJRVZBTCwgdHJhbnNpdGlvbkFmdGVyOiBjZGsuRHVyYXRpb24uZGF5cygxMjApIH1cbiAgICAgICAgICBdXG4gICAgICAgIH1cbiAgICAgIF0sXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5SRVRBSU5cbiAgICB9KTtcbiAgICByZXN1bHRzQnVja2V0LmFkZFRvUmVzb3VyY2VQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgc2lkOiAnRGVueUluc2VjdXJlVHJhbnNwb3J0JyxcbiAgICAgIGVmZmVjdDogaWFtLkVmZmVjdC5ERU5ZLFxuICAgICAgcHJpbmNpcGFsczogW25ldyBpYW0uQW55UHJpbmNpcGFsKCldLFxuICAgICAgYWN0aW9uczogWydzMzoqJ10sXG4gICAgICByZXNvdXJjZXM6IFtyZXN1bHRzQnVja2V0LmJ1Y2tldEFybiwgYCR7cmVzdWx0c0J1Y2tldC5idWNrZXRBcm59LypgXSxcbiAgICAgIGNvbmRpdGlvbnM6IHsgQm9vbDogeyAnYXdzOlNlY3VyZVRyYW5zcG9ydCc6ICdmYWxzZScgfSB9XG4gICAgfSkpO1xuICAgIHJlc3VsdHNCdWNrZXQuYWRkVG9SZXNvdXJjZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBzaWQ6ICdEZW55VW5FbmNyeXB0ZWRPYmplY3RVcGxvYWRzJyxcbiAgICAgIGVmZmVjdDogaWFtLkVmZmVjdC5ERU5ZLFxuICAgICAgcHJpbmNpcGFsczogW25ldyBpYW0uQW55UHJpbmNpcGFsKCldLFxuICAgICAgYWN0aW9uczogWydzMzpQdXRPYmplY3QnXSxcbiAgICAgIHJlc291cmNlczogW2Ake3Jlc3VsdHNCdWNrZXQuYnVja2V0QXJufS8qYF0sXG4gICAgICBjb25kaXRpb25zOiB7IFN0cmluZ05vdEVxdWFsczogeyAnczM6eC1hbXotc2VydmVyLXNpZGUtZW5jcnlwdGlvbic6ICdhd3M6a21zJyB9IH1cbiAgICB9KSk7XG5cbiAgICAvLyBTUVMgRGVhZCBMZXR0ZXIgUXVldWVcbiAgICBjb25zdCBkbHEgPSBuZXcgc3FzLlF1ZXVlKHRoaXMsICdQcm9jZXNzaW5nRExRJywge1xuICAgICAgcmV0ZW50aW9uUGVyaW9kOiBjZGsuRHVyYXRpb24uZGF5cygxNCksXG4gICAgICBlbmNyeXB0aW9uOiBzcXMuUXVldWVFbmNyeXB0aW9uLktNUyxcbiAgICAgIGVuY3J5cHRpb25NYXN0ZXJLZXk6IGttc0tleSxcbiAgICAgIGVuZm9yY2VTU0w6IHRydWVcbiAgICB9KTtcblxuICAgIC8vIE1haW4gU1FTIHF1ZXVlIGZvciB0YXNrc1xuICAgIGNvbnN0IHRhc2tzUXVldWUgPSBuZXcgc3FzLlF1ZXVlKHRoaXMsICdUYXNrc1F1ZXVlJywge1xuICAgICAgdmlzaWJpbGl0eVRpbWVvdXQ6IGNkay5EdXJhdGlvbi5ob3VycygyKSxcbiAgICAgIGRlYWRMZXR0ZXJRdWV1ZTogeyBxdWV1ZTogZGxxLCBtYXhSZWNlaXZlQ291bnQ6IDMgfSxcbiAgICAgIGVuY3J5cHRpb246IHNxcy5RdWV1ZUVuY3J5cHRpb24uS01TLFxuICAgICAgZW5jcnlwdGlvbk1hc3RlcktleToga21zS2V5LFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZSxcbiAgICAgIHJlY2VpdmVNZXNzYWdlV2FpdFRpbWU6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDApLFxuICAgIH0pO1xuXG4gICAgLy8gQ2xvdWRXYXRjaCBMb2cgR3JvdXAgZm9yIExhbWJkYVxuICAgIGNvbnN0IGxvZ0dyb3VwID0gbmV3IGxvZ3MuTG9nR3JvdXAodGhpcywgJ1Byb2Nlc3NpbmdMb2dHcm91cCcsIHtcbiAgICAgIGxvZ0dyb3VwTmFtZTogJy9hd3MvbGFtYmRhL2FtaXJhLXBhcmFsbGVsLXByb2Nlc3NvcicsXG4gICAgICByZXRlbnRpb246IGxvZ3MuUmV0ZW50aW9uRGF5cy5PTkVfTU9OVEgsXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5ERVNUUk9ZLFxuICAgICAgZW5jcnlwdGlvbktleToga21zS2V5XG4gICAgfSk7XG5cbiAgICAvLyBDb25kaXRpb25zXG4gICAgY29uc3QgYXVkaW9Qcm92aWRlZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdBdWRpb0J1Y2tldFByb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGF1ZGlvQnVja2V0TmFtZVBhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSlcbiAgICB9KTtcbiAgICBjb25zdCB0cml0b25VcmxQcm92aWRlZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdUcml0b25VcmxQcm92aWRlZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25Ob3QoY2RrLkZuLmNvbmRpdGlvbkVxdWFscyh0cml0b25DbHVzdGVyVXJsUGFyYW0udmFsdWVBc1N0cmluZywgJycpKVxuICAgIH0pO1xuICAgIGNvbnN0IHVzZVRyaXRvbkNvbmQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnVXNlVHJpdG9uQ29uZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25FcXVhbHMoZW5hYmxlVHJpdG9uUGFyYW0udmFsdWVBc1N0cmluZywgJ3RydWUnKVxuICAgIH0pO1xuXG4gICAgLy8gQ3VzdG9tIHJlc291cmNlIHRvIGZhaWwgZmFzdCBpZiBTU00gL2FtaXJhL3RyaXRvbl9hbGJfdXJsIGlzIG1pc3Npbmcgd2hlbiBUcml0b24gVVJMIHBhcmFtIGlzIGJsYW5rXG4gICAgY29uc3Qgc3NtUGFyYW1OYW1lID0gJy9hbWlyYS90cml0b25fYWxiX3VybCc7XG4gICAgY29uc3Qgc3NtUGFyYW1Bcm4gPSBjZGsuQXJuLmZvcm1hdCh7IHNlcnZpY2U6ICdzc20nLCByZXNvdXJjZTogJ3BhcmFtZXRlcicsIHJlc291cmNlTmFtZTogJ2FtaXJhL3RyaXRvbl9hbGJfdXJsJyB9LCB0aGlzKTtcbiAgICBjb25zdCB0cml0b25VcmxTc21DaGVjayA9IG5ldyBjci5Bd3NDdXN0b21SZXNvdXJjZSh0aGlzLCAnVHJpdG9uVXJsU3NtQ2hlY2snLCB7XG4gICAgICBvbkNyZWF0ZToge1xuICAgICAgICBzZXJ2aWNlOiAnU1NNJyxcbiAgICAgICAgYWN0aW9uOiAnZ2V0UGFyYW1ldGVyJyxcbiAgICAgICAgcGFyYW1ldGVyczogeyBOYW1lOiBzc21QYXJhbU5hbWUgfSxcbiAgICAgICAgcGh5c2ljYWxSZXNvdXJjZUlkOiBjci5QaHlzaWNhbFJlc291cmNlSWQub2YoJ1RyaXRvblVybFNzbUNoZWNrJylcbiAgICAgIH0sXG4gICAgICBvblVwZGF0ZToge1xuICAgICAgICBzZXJ2aWNlOiAnU1NNJyxcbiAgICAgICAgYWN0aW9uOiAnZ2V0UGFyYW1ldGVyJyxcbiAgICAgICAgcGFyYW1ldGVyczogeyBOYW1lOiBzc21QYXJhbU5hbWUgfSxcbiAgICAgICAgcGh5c2ljYWxSZXNvdXJjZUlkOiBjci5QaHlzaWNhbFJlc291cmNlSWQub2YoJ1RyaXRvblVybFNzbUNoZWNrJylcbiAgICAgIH0sXG4gICAgICBwb2xpY3k6IGNyLkF3c0N1c3RvbVJlc291cmNlUG9saWN5LmZyb21TZGtDYWxscyh7IHJlc291cmNlczogW3NzbVBhcmFtQXJuXSB9KVxuICAgIH0pO1xuICAgICh0cml0b25VcmxTc21DaGVjay5ub2RlLmRlZmF1bHRDaGlsZCBhcyBjZGsuQ2ZuQ3VzdG9tUmVzb3VyY2UpLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ1RyaXRvblVybE5vdFByb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbkVxdWFscyh0cml0b25DbHVzdGVyVXJsUGFyYW0udmFsdWVBc1N0cmluZywgJycpXG4gICAgfSk7XG5cbiAgICAvLyBQcm9jZXNzaW5nIExhbWJkYSBmdW5jdGlvbiBhcyBEb2NrZXIgaW1hZ2UgKHByZS1jYWNoZWQgbW9kZWwpXG4gICAgY29uc3QgcHJvY2Vzc2luZ0xhbWJkYSA9IG5ldyBsYW1iZGEuRG9ja2VySW1hZ2VGdW5jdGlvbih0aGlzLCAnUHJvY2Vzc2luZ0Z1bmN0aW9uJywge1xuICAgICAgZnVuY3Rpb25OYW1lOiAnYW1pcmEtcGFyYWxsZWwtcHJvY2Vzc29yJyxcbiAgICAgIGNvZGU6IGxhbWJkYS5Eb2NrZXJJbWFnZUNvZGUuZnJvbUltYWdlQXNzZXQoJy4uL2xhbWJkYS9wYXJhbGxlbF9wcm9jZXNzb3InKSxcbiAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5taW51dGVzKDE1KSxcbiAgICAgIG1lbW9yeVNpemU6IDEwMjQwLFxuICAgICAgLy8gUmVtb3ZlZCByZXNlcnZlZCBjb25jdXJyZW5jeSB0byBhdm9pZCB1bmludGVuZGVkIHRocm90dGxpbmcgZHVyaW5nIHRlc3RzXG4gICAgICBkZWFkTGV0dGVyUXVldWU6IGRscSxcbiAgICAgIHRyYWNpbmc6IGxhbWJkYS5UcmFjaW5nLkFDVElWRSxcbiAgICAgIGVudmlyb25tZW50OiB7XG4gICAgICAgIFJFU1VMVFNfQlVDS0VUOiByZXN1bHRzQnVja2V0LmJ1Y2tldE5hbWUsXG4gICAgICAgIFJFU1VMVFNfUFJFRklYOiByZXN1bHRzUHJlZml4UGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgTU9ERUxfUEFUSDogJy9vcHQvbW9kZWxzL3dhdjJ2ZWMyLW9wdGltaXplZCcsXG4gICAgICAgIEFVRElPX0JVQ0tFVDogYXVkaW9CdWNrZXROYW1lUGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgS01TX0tFWV9JRDoga21zS2V5LmtleUlkLFxuICAgICAgICBTTEFDS19XRUJIT09LX1VSTDogc2xhY2tXZWJob29rUGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgTUFYX0NPTkNVUlJFTkNZOiAnMTAnLCAvLyBUdW5lZCBmb3IgaW5pdGlhbCBhbGlnbm1lbnQ7IGFkanVzdCB2aWEgZW52IGlmIG5lZWRlZFxuICAgICAgICBCQVRDSF9BTExfUEhSQVNFUzogJ3RydWUnLFxuICAgICAgICBVU0VfRkxPQVQxNjogJ3RydWUnLFxuICAgICAgICBJTkNMVURFX0NPTkZJREVOQ0U6ICd0cnVlJyxcbiAgICAgICAgVEVTVF9NT0RFOiAnZmFsc2UnLFxuICAgICAgICBVU0VfVFJJVE9OOiBjZGsuVG9rZW4uYXNTdHJpbmcoY2RrLkZuLmNvbmRpdGlvbklmKHRyaXRvblVybFByb3ZpZGVkLmxvZ2ljYWxJZCwgJ3RydWUnLCAnZmFsc2UnKSksXG4gICAgICAgIFRSSVRPTl9VUkw6IGNkay5Ub2tlbi5hc1N0cmluZyhjZGsuRm4uY29uZGl0aW9uSWYoXG4gICAgICAgICAgdHJpdG9uVXJsUHJvdmlkZWQubG9naWNhbElkLFxuICAgICAgICAgIHRyaXRvbkNsdXN0ZXJVcmxQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICAgIHNzbS5TdHJpbmdQYXJhbWV0ZXIudmFsdWVGb3JTdHJpbmdQYXJhbWV0ZXIodGhpcywgJy9hbWlyYS90cml0b25fYWxiX3VybCcpXG4gICAgICAgICkpLFxuICAgICAgICBUUklUT05fTU9ERUw6ICd3MnYyJyxcbiAgICAgICAgUFlUSE9OT1BUSU1JWkU6ICcyJyxcbiAgICAgICAgVE9SQ0hfTlVNX1RIUkVBRFM6ICc2JyxcbiAgICAgICAgT01QX05VTV9USFJFQURTOiAnNicsXG4gICAgICAgIFRSQU5TRk9STUVSU19DQUNIRTogJy90bXAvbW9kZWxzJyxcbiAgICAgICAgSEZfSFVCX0NBQ0hFOiAnL3RtcC9oZl9jYWNoZSdcbiAgICAgIH1cbiAgICB9KTtcblxuICAgIC8vIENvbmRpdGlvbmFsbHkgYXR0YWNoIExhbWJkYSB0byBWUEMgcHJpdmF0ZSBzdWJuZXRzXG4gICAgY29uc3Qgc3VibmV0c0xpc3QgPSBwcml2YXRlU3VibmV0SWRzQ3N2UGFyYW0udmFsdWVBc0xpc3Q7XG4gICAgY29uc3QgdnBjID0gZWMyLlZwYy5mcm9tVnBjQXR0cmlidXRlcyh0aGlzLCAnSW1wb3J0ZWRWcGMnLCB7XG4gICAgICB2cGNJZDogY2RrLlRva2VuLmFzU3RyaW5nKGNkay5Gbi5jb25kaXRpb25JZih2cGNQcm92aWRlZC5sb2dpY2FsSWQsIHZwY0lkUGFyYW0udmFsdWVBc1N0cmluZywgY2RrLkF3cy5OT19WQUxVRSBhcyBhbnkpKSxcbiAgICAgIGF2YWlsYWJpbGl0eVpvbmVzOiBjZGsuU3RhY2sub2YodGhpcykuYXZhaWxhYmlsaXR5Wm9uZXMsXG4gICAgICBwcml2YXRlU3VibmV0SWRzOiBzdWJuZXRzTGlzdCBhcyB1bmtub3duIGFzIHN0cmluZ1tdXG4gICAgfSk7XG4gICAgY29uc3QgbGFtYmRhU2cgPSBlYzIuU2VjdXJpdHlHcm91cC5mcm9tU2VjdXJpdHlHcm91cElkKHRoaXMsICdJbXBvcnRlZExhbWJkYVNnJywgY2RrLlRva2VuLmFzU3RyaW5nKGNkay5Gbi5jb25kaXRpb25JZihsYW1iZGFTZ1Byb3ZpZGVkLmxvZ2ljYWxJZCwgbGFtYmRhU2VjdXJpdHlHcm91cElkUGFyYW0udmFsdWVBc1N0cmluZywgY2RrLkF3cy5OT19WQUxVRSBhcyBhbnkpKSwgeyBtdXRhYmxlOiBmYWxzZSB9KTtcbiAgICBjb25zdCBjZm5GdW5jID0gcHJvY2Vzc2luZ0xhbWJkYS5ub2RlLmRlZmF1bHRDaGlsZCBhcyBsYW1iZGEuQ2ZuRnVuY3Rpb247XG4gICAgY2ZuRnVuYy52cGNDb25maWcgPSBjZGsuVG9rZW4uYXNBbnkoY2RrLkZuLmNvbmRpdGlvbklmKFxuICAgICAgdnBjUHJvdmlkZWQubG9naWNhbElkLFxuICAgICAge1xuICAgICAgICBTZWN1cml0eUdyb3VwSWRzOiBjZGsuVG9rZW4uYXNMaXN0KGNkay5Gbi5jb25kaXRpb25JZihsYW1iZGFTZ1Byb3ZpZGVkLmxvZ2ljYWxJZCwgW2xhbWJkYVNlY3VyaXR5R3JvdXBJZFBhcmFtLnZhbHVlQXNTdHJpbmddLCBjZGsuQXdzLk5PX1ZBTFVFIGFzIGFueSkpLFxuICAgICAgICBTdWJuZXRJZHM6IHN1Ym5ldHNMaXN0XG4gICAgICB9LFxuICAgICAgY2RrLkF3cy5OT19WQUxVRVxuICAgICkpO1xuXG4gICAgLy8gRW5mb3JjZTogaWYgVnBjSWQgaXMgcHJvdmlkZWQsIExhbWJkYVNlY3VyaXR5R3JvdXBJZCBtdXN0IGJlIHByb3ZpZGVkXG4gICAgbmV3IGNkay5DZm5SdWxlKHRoaXMsICdWcGNSZXF1aXJlc0xhbWJkYVNnJywge1xuICAgICAgcnVsZUNvbmRpdGlvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHZwY0lkUGFyYW0udmFsdWVBc1N0cmluZywgJycpKSxcbiAgICAgIGFzc2VydGlvbnM6IFtcbiAgICAgICAge1xuICAgICAgICAgIGFzc2VydDogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGxhbWJkYVNlY3VyaXR5R3JvdXBJZFBhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSksXG4gICAgICAgICAgYXNzZXJ0RGVzY3JpcHRpb246ICdXaGVuIFZwY0lkIGlzIHByb3ZpZGVkLCBMYW1iZGFTZWN1cml0eUdyb3VwSWQgbXVzdCBhbHNvIGJlIHByb3ZpZGVkLidcbiAgICAgICAgfVxuICAgICAgXVxuICAgIH0pO1xuXG4gICAgLy8gU1FTIEV2ZW50IFNvdXJjZSBmb3IgTGFtYmRhXG4gICAgY29uc3QgbWF4RXZlbnRTb3VyY2VDb25jdXJyZW5jeVBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ01heEV2ZW50U291cmNlQ29uY3VycmVuY3knLCB7XG4gICAgICB0eXBlOiAnTnVtYmVyJyxcbiAgICAgIGRlZmF1bHQ6IDEwLFxuICAgICAgZGVzY3JpcHRpb246ICdTUVMgZXZlbnQgc291cmNlIG1heCBjb25jdXJyZW5jeSBmb3IgdGhlIHByb2Nlc3NpbmcgTGFtYmRhJ1xuICAgIH0pO1xuXG4gICAgY29uc3QgZXZlbnRTb3VyY2UgPSBuZXcgc291cmNlcy5TcXNFdmVudFNvdXJjZSh0YXNrc1F1ZXVlLCB7XG4gICAgICBiYXRjaFNpemU6IDEsXG4gICAgICBtYXhDb25jdXJyZW5jeTogbWF4RXZlbnRTb3VyY2VDb25jdXJyZW5jeVBhcmFtLnZhbHVlQXNOdW1iZXIsXG4gICAgICByZXBvcnRCYXRjaEl0ZW1GYWlsdXJlczogdHJ1ZSxcbiAgICAgIG1heEJhdGNoaW5nV2luZG93OiBjZGsuRHVyYXRpb24uc2Vjb25kcygwKSxcbiAgICB9KTtcbiAgICBwcm9jZXNzaW5nTGFtYmRhLmFkZEV2ZW50U291cmNlKGV2ZW50U291cmNlKTtcblxuICAgIC8vIE9wdGlvbmFsIHdhcm1pbmcgcnVsZVxuICAgIGNvbnN0IGVuYWJsZVdhcm1pbmdQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdFbmFibGVXYXJtaW5nJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnZmFsc2UnLFxuICAgICAgYWxsb3dlZFZhbHVlczogWyd0cnVlJywgJ2ZhbHNlJ10sXG4gICAgICBkZXNjcmlwdGlvbjogJ0VuYWJsZSBwZXJpb2RpYyB3YXJtIGludm9jYXRpb24gdG8gcmVkdWNlIGNvbGQgc3RhcnRzJ1xuICAgIH0pO1xuICAgIGNvbnN0IHdhcm1SYXRlTWludXRlc1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1dhcm1SYXRlTWludXRlcycsIHtcbiAgICAgIHR5cGU6ICdOdW1iZXInLFxuICAgICAgZGVmYXVsdDogMTUsXG4gICAgICBkZXNjcmlwdGlvbjogJ1dhcm0gcGluZyByYXRlIGluIG1pbnV0ZXMgd2hlbiBFbmFibGVXYXJtaW5nPXRydWUnXG4gICAgfSk7XG4gICAgY29uc3Qgd2FybUVuYWJsZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnV2FybUVuYWJsZWQnLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGVuYWJsZVdhcm1pbmdQYXJhbS52YWx1ZUFzU3RyaW5nLCAndHJ1ZScpXG4gICAgfSk7XG4gICAgY29uc3Qgd2FybWluZ1J1bGUgPSBuZXcgZXZlbnRzLkNmblJ1bGUodGhpcywgJ1Byb2Nlc3NpbmdXYXJtUnVsZScsIHtcbiAgICAgIHNjaGVkdWxlRXhwcmVzc2lvbjogY2RrLkZuLnN1YigncmF0ZSgke01pbnV0ZXN9IG1pbnV0ZXMpJywgeyBNaW51dGVzOiB3YXJtUmF0ZU1pbnV0ZXNQYXJhbS52YWx1ZUFzU3RyaW5nIH0pLFxuICAgICAgc3RhdGU6ICdFTkFCTEVEJyxcbiAgICAgIHRhcmdldHM6IFtcbiAgICAgICAge1xuICAgICAgICAgIGlkOiAnVGFyZ2V0MCcsXG4gICAgICAgICAgYXJuOiBwcm9jZXNzaW5nTGFtYmRhLmZ1bmN0aW9uQXJuLFxuICAgICAgICAgIGlucHV0OiBKU09OLnN0cmluZ2lmeSh7IHdhcm06IHRydWUgfSlcbiAgICAgICAgfVxuICAgICAgXVxuICAgIH0pO1xuICAgIHdhcm1pbmdSdWxlLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gd2FybUVuYWJsZWQ7XG4gICAgY29uc3Qgd2FybVBlcm1pc3Npb24gPSBuZXcgbGFtYmRhLkNmblBlcm1pc3Npb24odGhpcywgJ0FsbG93RXZlbnRCcmlkZ2VJbnZva2VXYXJtJywge1xuICAgICAgYWN0aW9uOiAnbGFtYmRhOkludm9rZUZ1bmN0aW9uJyxcbiAgICAgIGZ1bmN0aW9uTmFtZTogcHJvY2Vzc2luZ0xhbWJkYS5mdW5jdGlvbk5hbWUsXG4gICAgICBwcmluY2lwYWw6ICdldmVudHMuYW1hem9uYXdzLmNvbScsXG4gICAgICBzb3VyY2VBcm46IHdhcm1pbmdSdWxlLmF0dHJBcm5cbiAgICB9KTtcbiAgICB3YXJtUGVybWlzc2lvbi5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IHdhcm1FbmFibGVkO1xuXG4gICAgY29uc3QgYXVkaW9Qb2xpY3lEb2MgPSBuZXcgaWFtLlBvbGljeURvY3VtZW50KHtcbiAgICAgIHN0YXRlbWVudHM6IFtcbiAgICAgICAgbmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgICAgIGFjdGlvbnM6IFsnczM6TGlzdEJ1Y2tldCddLFxuICAgICAgICAgIHJlc291cmNlczogW2Nkay5Gbi5zdWIoJ2Fybjphd3M6czM6Ojoke0J1Y2tldE5hbWV9JywgeyBCdWNrZXROYW1lOiBhdWRpb0J1Y2tldE5hbWVQYXJhbS52YWx1ZUFzU3RyaW5nIH0pXVxuICAgICAgICB9KSxcbiAgICAgICAgbmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgICAgIGFjdGlvbnM6IFsnczM6R2V0T2JqZWN0J10sXG4gICAgICAgICAgcmVzb3VyY2VzOiBbY2RrLkZuLnN1YignYXJuOmF3czpzMzo6OiR7QnVja2V0TmFtZX0vKicsIHsgQnVja2V0TmFtZTogYXVkaW9CdWNrZXROYW1lUGFyYW0udmFsdWVBc1N0cmluZyB9KV1cbiAgICAgICAgfSlcbiAgICAgIF1cbiAgICB9KTtcblxuICAgIGNvbnN0IGF1ZGlvQ2ZuUG9saWN5ID0gbmV3IGlhbS5DZm5Qb2xpY3kodGhpcywgJ1Byb2Nlc3NpbmdMYW1iZGFBdWRpb1BvbGljeScsIHtcbiAgICAgIHBvbGljeURvY3VtZW50OiBhdWRpb1BvbGljeURvYyxcbiAgICAgIHJvbGVzOiBbcHJvY2Vzc2luZ0xhbWJkYS5yb2xlIS5yb2xlTmFtZV0sXG4gICAgICBwb2xpY3lOYW1lOiBgUHJvY2Vzc2luZ0xhbWJkYUF1ZGlvUG9saWN5LSR7Y2RrLlN0YWNrLm9mKHRoaXMpLnN0YWNrTmFtZX1gXG4gICAgfSk7XG4gICAgYXVkaW9DZm5Qb2xpY3kuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBhdWRpb1Byb3ZpZGVkO1xuXG4gICAgLy8gUmVzdWx0cyBidWNrZXQgYW5kIEtNUyBwZXJtaXNzaW9uc1xuICAgIHJlc3VsdHNCdWNrZXQuZ3JhbnRXcml0ZShwcm9jZXNzaW5nTGFtYmRhKTtcbiAgICBrbXNLZXkuZ3JhbnRFbmNyeXB0RGVjcnlwdChwcm9jZXNzaW5nTGFtYmRhKTtcblxuICAgIC8vIENsb3VkV2F0Y2ggbWV0cmljcyBwZXJtaXNzaW9ucyBmb3Igam9iIHRyYWNraW5nXG4gICAgcHJvY2Vzc2luZ0xhbWJkYS5hZGRUb1JvbGVQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgYWN0aW9uczogW1xuICAgICAgICAnY2xvdWR3YXRjaDpQdXRNZXRyaWNEYXRhJ1xuICAgICAgXSxcbiAgICAgIHJlc291cmNlczogWycqJ11cbiAgICB9KSk7XG5cbiAgICAvLyBFbnF1ZXVlIExhbWJkYSBmdW5jdGlvblxuICAgIGNvbnN0IGVucXVldWVMYW1iZGEgPSBuZXcgbGFtYmRhLkZ1bmN0aW9uKHRoaXMsICdFbnF1ZXVlRnVuY3Rpb24nLCB7XG4gICAgICBydW50aW1lOiBsYW1iZGEuUnVudGltZS5QWVRIT05fM18xMixcbiAgICAgIGhhbmRsZXI6ICdpbmRleC5sYW1iZGFfaGFuZGxlcicsXG4gICAgICBjb2RlOiBsYW1iZGEuQ29kZS5mcm9tQXNzZXQoJy4uL2xhbWJkYS9lbnF1ZXVlX2pvYnMnKSxcbiAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5taW51dGVzKDUpLFxuICAgICAgdHJhY2luZzogbGFtYmRhLlRyYWNpbmcuQUNUSVZFLFxuICAgICAgZW52aXJvbm1lbnQ6IHtcbiAgICAgICAgSk9CU19RVUVVRV9VUkw6IHRhc2tzUXVldWUucXVldWVVcmwsXG4gICAgICAgIEFUSEVOQV9EQVRBQkFTRTogYXRoZW5hRGJQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBBVEhFTkFfT1VUUFVUOiBhdGhlbmFPdXRwdXRQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBBVEhFTkFfUVVFUlk6IGF0aGVuYVF1ZXJ5UGFyYW0udmFsdWVBc1N0cmluZ1xuICAgICAgfVxuICAgIH0pO1xuXG4gICAgLy8gSUFNIHBlcm1pc3Npb25zIGZvciBlbnF1ZXVlIExhbWJkYVxuICAgIGNvbnN0IGF0aGVuYVdvcmtncm91cEFybiA9IGNkay5Bcm4uZm9ybWF0KHtcbiAgICAgIHNlcnZpY2U6ICdhdGhlbmEnLFxuICAgICAgcmVzb3VyY2U6ICd3b3JrZ3JvdXAnLFxuICAgICAgcmVzb3VyY2VOYW1lOiAncHJpbWFyeSdcbiAgICB9LCB0aGlzKTtcbiAgICBjb25zdCBnbHVlRGJBcm4gPSBjZGsuQXJuLmZvcm1hdCh7XG4gICAgICBzZXJ2aWNlOiAnZ2x1ZScsXG4gICAgICByZXNvdXJjZTogJ2RhdGFiYXNlJyxcbiAgICAgIHJlc291cmNlTmFtZTogYXRoZW5hRGJQYXJhbS52YWx1ZUFzU3RyaW5nXG4gICAgfSwgdGhpcyk7XG4gICAgY29uc3QgZ2x1ZVRhYmxlV2lsZGNhcmRBcm4gPSBjZGsuQXJuLmZvcm1hdCh7XG4gICAgICBzZXJ2aWNlOiAnZ2x1ZScsXG4gICAgICByZXNvdXJjZTogJ3RhYmxlJyxcbiAgICAgIHJlc291cmNlTmFtZTogYCR7YXRoZW5hRGJQYXJhbS52YWx1ZUFzU3RyaW5nfS8qYFxuICAgIH0sIHRoaXMpO1xuXG4gICAgZW5xdWV1ZUxhbWJkYS5hZGRUb1JvbGVQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgYWN0aW9uczogWydhdGhlbmE6U3RhcnRRdWVyeUV4ZWN1dGlvbicsICdhdGhlbmE6R2V0UXVlcnlFeGVjdXRpb24nLCAnYXRoZW5hOkdldFF1ZXJ5UmVzdWx0cyddLFxuICAgICAgcmVzb3VyY2VzOiBbYXRoZW5hV29ya2dyb3VwQXJuXVxuICAgIH0pKTtcbiAgICBlbnF1ZXVlTGFtYmRhLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbJ2dsdWU6R2V0RGF0YWJhc2UnXSxcbiAgICAgIHJlc291cmNlczogW2dsdWVEYkFybl1cbiAgICB9KSk7XG4gICAgZW5xdWV1ZUxhbWJkYS5hZGRUb1JvbGVQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgYWN0aW9uczogWydnbHVlOkdldFRhYmxlJ10sXG4gICAgICByZXNvdXJjZXM6IFtnbHVlVGFibGVXaWxkY2FyZEFybl1cbiAgICB9KSk7XG5cbiAgICAvLyBBdGhlbmEgb3V0cHV0IGJ1Y2tldCBwZXJtaXNzaW9uc1xuICAgIGNvbnN0IGF0aGVuYU91dHB1dFBhcnNlZCA9IGNkay5Gbi5zcGxpdCgnLycsIGF0aGVuYU91dHB1dFBhcmFtLnZhbHVlQXNTdHJpbmcpO1xuICAgIGNvbnN0IGF0aGVuYU91dHB1dEJ1Y2tldCA9IGNkay5Gbi5zZWxlY3QoMiwgYXRoZW5hT3V0cHV0UGFyc2VkKTtcbiAgICBlbnF1ZXVlTGFtYmRhLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbJ3MzOkxpc3RCdWNrZXQnXSxcbiAgICAgIHJlc291cmNlczogW2Nkay5Bcm4uZm9ybWF0KHsgc2VydmljZTogJ3MzJywgcmVzb3VyY2U6IGF0aGVuYU91dHB1dEJ1Y2tldCB9LCB0aGlzKV1cbiAgICB9KSk7XG4gICAgZW5xdWV1ZUxhbWJkYS5hZGRUb1JvbGVQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgYWN0aW9uczogWydzMzpHZXRPYmplY3QnLCAnczM6UHV0T2JqZWN0J10sXG4gICAgICByZXNvdXJjZXM6IFtjZGsuQXJuLmZvcm1hdCh7IHNlcnZpY2U6ICdzMycsIHJlc291cmNlOiBgJHthdGhlbmFPdXRwdXRCdWNrZXR9LypgIH0sIHRoaXMpXVxuICAgIH0pKTtcblxuICAgIHRhc2tzUXVldWUuZ3JhbnRTZW5kTWVzc2FnZXMoZW5xdWV1ZUxhbWJkYSk7XG5cbiAgICAvLyBTY2hlZHVsZSBmb3IgYXV0b21hdGljIGVucXVldWVpbmdcbiAgICBjb25zdCBzY2hlZHVsZVJ1bGUgPSBuZXcgZXZlbnRzLlJ1bGUodGhpcywgJ1NjaGVkdWxlUnVsZScsIHtcbiAgICAgIGRlc2NyaXB0aW9uOiAnVHJpZ2dlciBwYXJhbGxlbCBwcm9jZXNzaW5nIHBpcGVsaW5lJyxcbiAgICAgIHNjaGVkdWxlOiBldmVudHMuU2NoZWR1bGUuY3Jvbih7IG1pbnV0ZTogJzAnLCBob3VyOiAnMicgfSlcbiAgICB9KTtcbiAgICBzY2hlZHVsZVJ1bGUuYWRkVGFyZ2V0KG5ldyB0YXJnZXRzLkxhbWJkYUZ1bmN0aW9uKGVucXVldWVMYW1iZGEpKTtcblxuICAgIC8vIE9wdGlvbmFsIEF0aGVuYSBzdGFnaW5nIGNsZWFudXAgTGFtYmRhICsgc2NoZWR1bGVcbiAgICBjb25zdCBlbmFibGVBdGhlbmFDbGVhbnVwUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnRW5hYmxlQXRoZW5hQ2xlYW51cCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2ZhbHNlJyxcbiAgICAgIGFsbG93ZWRWYWx1ZXM6IFsndHJ1ZScsICdmYWxzZSddLFxuICAgICAgZGVzY3JpcHRpb246ICdFbmFibGUgc2NoZWR1bGVkIEF0aGVuYSBzdGFnaW5nIGNsZWFudXAgKG9wdGlvbmFsKSdcbiAgICB9KTtcbiAgICBjb25zdCBhdGhlbmFDbGVhbnVwQnVja2V0UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXRoZW5hQ2xlYW51cEJ1Y2tldCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ1MzIGJ1Y2tldCBmb3IgQXRoZW5hIHN0YWdpbmcgcmVzdWx0cydcbiAgICB9KTtcbiAgICBjb25zdCBhdGhlbmFDbGVhbnVwUHJlZml4UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXRoZW5hQ2xlYW51cFByZWZpeCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2F0aGVuYV9zdGFnaW5nJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnUzMgcHJlZml4IGZvciBBdGhlbmEgc3RhZ2luZyByZXN1bHRzJ1xuICAgIH0pO1xuICAgIGNvbnN0IGF0aGVuYUNsZWFudXBBZ2VEYXlzUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXRoZW5hQ2xlYW51cEFnZURheXMnLCB7XG4gICAgICB0eXBlOiAnTnVtYmVyJyxcbiAgICAgIGRlZmF1bHQ6IDcsXG4gICAgICBkZXNjcmlwdGlvbjogJ0RlbGV0ZSBzdGFnaW5nIG9iamVjdHMgb2xkZXIgdGhhbiBOIGRheXMnXG4gICAgfSk7XG4gICAgY29uc3QgY2xlYW51cEVuYWJsZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnQXRoZW5hQ2xlYW51cEVuYWJsZWQnLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGVuYWJsZUF0aGVuYUNsZWFudXBQYXJhbS52YWx1ZUFzU3RyaW5nLCAndHJ1ZScpXG4gICAgfSk7XG5cbiAgICBjb25zdCBjbGVhbnVwTGFtYmRhID0gbmV3IGxhbWJkYS5GdW5jdGlvbih0aGlzLCAnQXRoZW5hU3RhZ2luZ0NsZWFudXAnLCB7XG4gICAgICBydW50aW1lOiBsYW1iZGEuUnVudGltZS5QWVRIT05fM18xMixcbiAgICAgIGhhbmRsZXI6ICdhdGhlbmFfc3RhZ2luZ19jbGVhbnVwLm1haW4nLFxuICAgICAgY29kZTogbGFtYmRhLkNvZGUuZnJvbUFzc2V0KCcuLi9zY3JpcHRzJyksXG4gICAgICB0aW1lb3V0OiBjZGsuRHVyYXRpb24ubWludXRlcyg1KSxcbiAgICAgIGVudmlyb25tZW50OiB7XG4gICAgICAgIEFXU19SRUdJT046IGNkay5TdGFjay5vZih0aGlzKS5yZWdpb24sXG4gICAgICB9XG4gICAgfSk7XG4gICAgKGNsZWFudXBMYW1iZGEubm9kZS5kZWZhdWx0Q2hpbGQgYXMgbGFtYmRhLkNmbkZ1bmN0aW9uKS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGNsZWFudXBFbmFibGVkO1xuXG4gICAgY2xlYW51cExhbWJkYS5hZGRUb1JvbGVQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgYWN0aW9uczogWydzMzpMaXN0QnVja2V0JywgJ3MzOkRlbGV0ZU9iamVjdCcsICdzMzpEZWxldGVPYmplY3RWZXJzaW9uJ10sXG4gICAgICByZXNvdXJjZXM6IFtcbiAgICAgICAgY2RrLkFybi5mb3JtYXQoeyBzZXJ2aWNlOiAnczMnLCByZXNvdXJjZTogYXRoZW5hQ2xlYW51cEJ1Y2tldFBhcmFtLnZhbHVlQXNTdHJpbmcgfSwgdGhpcyksXG4gICAgICAgIGNkay5Bcm4uZm9ybWF0KHsgc2VydmljZTogJ3MzJywgcmVzb3VyY2U6IGAke2F0aGVuYUNsZWFudXBCdWNrZXRQYXJhbS52YWx1ZUFzU3RyaW5nfS8qYCB9LCB0aGlzKVxuICAgICAgXVxuICAgIH0pKTtcblxuICAgIGNvbnN0IGNsZWFudXBSdWxlID0gbmV3IGV2ZW50cy5SdWxlKHRoaXMsICdBdGhlbmFDbGVhbnVwU2NoZWR1bGUnLCB7XG4gICAgICBkZXNjcmlwdGlvbjogJ1NjaGVkdWxlZCBBdGhlbmEgc3RhZ2luZyBjbGVhbnVwJyxcbiAgICAgIHNjaGVkdWxlOiBldmVudHMuU2NoZWR1bGUuY3Jvbih7IG1pbnV0ZTogJzAnLCBob3VyOiAnMycgfSlcbiAgICB9KTtcbiAgICAoY2xlYW51cFJ1bGUubm9kZS5kZWZhdWx0Q2hpbGQgYXMgZXZlbnRzLkNmblJ1bGUpLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gY2xlYW51cEVuYWJsZWQ7XG4gICAgY2xlYW51cFJ1bGUuYWRkVGFyZ2V0KG5ldyB0YXJnZXRzLkxhbWJkYUZ1bmN0aW9uKGNsZWFudXBMYW1iZGEsIHtcbiAgICAgIGV2ZW50OiBldmVudHMuUnVsZVRhcmdldElucHV0LmZyb21PYmplY3Qoe1xuICAgICAgICBidWNrZXQ6IGF0aGVuYUNsZWFudXBCdWNrZXRQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBwcmVmaXg6IGF0aGVuYUNsZWFudXBQcmVmaXhQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBhZ2VfZGF5czogYXRoZW5hQ2xlYW51cEFnZURheXNQYXJhbS52YWx1ZUFzTnVtYmVyLFxuICAgICAgfSlcbiAgICB9KSk7XG5cbiAgICAvLyBNYW51YWwgdHJpZ2dlciBMYW1iZGFcbiAgICBjb25zdCBtYW51YWxUcmlnZ2VyTGFtYmRhID0gbmV3IGxhbWJkYS5GdW5jdGlvbih0aGlzLCAnTWFudWFsVHJpZ2dlckZ1bmN0aW9uJywge1xuICAgICAgcnVudGltZTogbGFtYmRhLlJ1bnRpbWUuUFlUSE9OXzNfMTIsXG4gICAgICBoYW5kbGVyOiAnaW5kZXgubGFtYmRhX2hhbmRsZXInLFxuICAgICAgY29kZTogbGFtYmRhLkNvZGUuZnJvbUFzc2V0KCcuLi9sYW1iZGEvbWFudWFsX2VucXVldWUnKSxcbiAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5taW51dGVzKDEpLFxuICAgICAgdHJhY2luZzogbGFtYmRhLlRyYWNpbmcuQUNUSVZFLFxuICAgICAgZW52aXJvbm1lbnQ6IHsgSk9CU19RVUVVRV9VUkw6IHRhc2tzUXVldWUucXVldWVVcmwgfVxuICAgIH0pO1xuICAgIHRhc2tzUXVldWUuZ3JhbnRTZW5kTWVzc2FnZXMobWFudWFsVHJpZ2dlckxhbWJkYSk7XG5cbiAgICAvLyBTTlMgdG9waWMgZm9yIGFsZXJ0c1xuICAgIGNvbnN0IGFsZXJ0c1RvcGljID0gbmV3IHNucy5Ub3BpYyh0aGlzLCAnQWxlcnRzVG9waWMnLCB7XG4gICAgICBkaXNwbGF5TmFtZTogJ0FtaXJhIExhbWJkYSBQYXJhbGxlbCBBbGVydHMnXG4gICAgfSk7XG5cbiAgICAvLyBTbGFjayBub3RpZmljYXRpb24gTGFtYmRhXG4gICAgY29uc3Qgc2xhY2tOb3RpZmllckxhbWJkYSA9IG5ldyBsYW1iZGEuRnVuY3Rpb24odGhpcywgJ1NsYWNrTm90aWZpZXJGdW5jdGlvbicsIHtcbiAgICAgIHJ1bnRpbWU6IGxhbWJkYS5SdW50aW1lLlBZVEhPTl8zXzEyLFxuICAgICAgaGFuZGxlcjogJ2luZGV4LmxhbWJkYV9oYW5kbGVyJyxcbiAgICAgIGNvZGU6IGxhbWJkYS5Db2RlLmZyb21Bc3NldCgnLi4vbGFtYmRhL3NsYWNrX25vdGlmaWVyJyksXG4gICAgICB0aW1lb3V0OiBjZGsuRHVyYXRpb24uc2Vjb25kcygzMCksXG4gICAgICB0cmFjaW5nOiBsYW1iZGEuVHJhY2luZy5BQ1RJVkUsXG4gICAgICBlbnZpcm9ubWVudDoge1xuICAgICAgICBTTEFDS19XRUJIT09LX1VSTDogc2xhY2tXZWJob29rUGFyYW0udmFsdWVBc1N0cmluZ1xuICAgICAgfVxuICAgIH0pO1xuXG4gICAgLy8gU3Vic2NyaWJlIFNsYWNrIG5vdGlmaWVyIHRvIFNOUyBhbGVydHNcbiAgICBjb25zdCBzbGFja1dlYmhvb2tQcm92aWRlZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdTbGFja1dlYmhvb2tQcm92aWRlZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25Ob3QoY2RrLkZuLmNvbmRpdGlvbkVxdWFscyhzbGFja1dlYmhvb2tQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpXG4gICAgfSk7XG5cbiAgICAvLyBMYW1iZGEgcGVybWlzc2lvbiBmb3IgU05TIHRvIGludm9rZSBTbGFjayBub3RpZmllclxuICAgIHNsYWNrTm90aWZpZXJMYW1iZGEuYWRkUGVybWlzc2lvbignQWxsb3dTTlNJbnZva2UnLCB7XG4gICAgICBwcmluY2lwYWw6IG5ldyBpYW0uU2VydmljZVByaW5jaXBhbCgnc25zLmFtYXpvbmF3cy5jb20nKSxcbiAgICAgIHNvdXJjZUFybjogYWxlcnRzVG9waWMudG9waWNBcm5cbiAgICB9KTtcblxuICAgIC8vIENvbmRpdGlvbmFsIFNOUyBzdWJzY3JpcHRpb24gdG8gU2xhY2sgbm90aWZpZXJcbiAgICBjb25zdCBzbGFja1N1YnNjcmlwdGlvbiA9IG5ldyBzbnMuQ2ZuU3Vic2NyaXB0aW9uKHRoaXMsICdTbGFja05vdGlmaWVyU3Vic2NyaXB0aW9uJywge1xuICAgICAgdG9waWNBcm46IGFsZXJ0c1RvcGljLnRvcGljQXJuLFxuICAgICAgcHJvdG9jb2w6ICdsYW1iZGEnLFxuICAgICAgZW5kcG9pbnQ6IHNsYWNrTm90aWZpZXJMYW1iZGEuZnVuY3Rpb25Bcm5cbiAgICB9KTtcbiAgICBzbGFja1N1YnNjcmlwdGlvbi5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IHNsYWNrV2ViaG9va1Byb3ZpZGVkO1xuXG4gICAgLy8gQ2xvdWRXYXRjaCBBbGFybXNcbiAgICBjb25zdCBkbHFEZXB0aEFsYXJtID0gbmV3IGN3LkFsYXJtKHRoaXMsICdETFFEZXB0aEFsYXJtJywge1xuICAgICAgbWV0cmljOiBkbHEubWV0cmljQXBwcm94aW1hdGVOdW1iZXJPZk1lc3NhZ2VzVmlzaWJsZSgpLFxuICAgICAgdGhyZXNob2xkOiAxLFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDEsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fT1JfRVFVQUxfVE9fVEhSRVNIT0xEXG4gICAgfSk7XG5cbiAgICBjb25zdCBwcm9jZXNzaW5nRXJyb3JzQWxhcm0gPSBuZXcgY3cuQWxhcm0odGhpcywgJ1Byb2Nlc3NpbmdFcnJvcnNBbGFybScsIHtcbiAgICAgIG1ldHJpYzogcHJvY2Vzc2luZ0xhbWJkYS5tZXRyaWNFcnJvcnMoKSxcbiAgICAgIHRocmVzaG9sZDogMTAsXG4gICAgICBldmFsdWF0aW9uUGVyaW9kczogMixcbiAgICAgIGNvbXBhcmlzb25PcGVyYXRvcjogY3cuQ29tcGFyaXNvbk9wZXJhdG9yLkdSRUFURVJfVEhBTl9USFJFU0hPTERcbiAgICB9KTtcblxuICAgIGNvbnN0IHF1ZXVlRGVwdGhBbGFybSA9IG5ldyBjdy5BbGFybSh0aGlzLCAnUXVldWVEZXB0aEFsYXJtJywge1xuICAgICAgbWV0cmljOiB0YXNrc1F1ZXVlLm1ldHJpY0FwcHJveGltYXRlTnVtYmVyT2ZNZXNzYWdlc1Zpc2libGUoKSxcbiAgICAgIHRocmVzaG9sZDogMTAwMCxcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAzLFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuR1JFQVRFUl9USEFOX1RIUkVTSE9MRFxuICAgIH0pO1xuXG4gICAgY29uc3QgcXVldWVBZ2VBbGFybSA9IG5ldyBjdy5BbGFybSh0aGlzLCAnUXVldWVBZ2VBbGFybScsIHtcbiAgICAgIG1ldHJpYzogdGFza3NRdWV1ZS5tZXRyaWNBcHByb3hpbWF0ZUFnZU9mT2xkZXN0TWVzc2FnZSgpLFxuICAgICAgdGhyZXNob2xkOiAzMDAsXG4gICAgICBldmFsdWF0aW9uUGVyaW9kczogMyxcbiAgICAgIGNvbXBhcmlzb25PcGVyYXRvcjogY3cuQ29tcGFyaXNvbk9wZXJhdG9yLkdSRUFURVJfVEhBTl9USFJFU0hPTERcbiAgICB9KTtcblxuICAgIC8vIEpvYiBjb21wbGV0aW9uIGRldGVjdGlvbiAtIHF1ZXVlIGVtcHR5IEFORCBubyBhY3RpdmUgTGFtYmRhIGV4ZWN1dGlvbnNcbiAgICBjb25zdCBjb25jdXJyZW50RXhlY3V0aW9uc01ldHJpYyA9IG5ldyBjdy5NZXRyaWMoe1xuICAgICAgbmFtZXNwYWNlOiAnQVdTL0xhbWJkYScsXG4gICAgICBtZXRyaWNOYW1lOiAnQ29uY3VycmVudEV4ZWN1dGlvbnMnLFxuICAgICAgZGltZW5zaW9uc01hcDogeyBGdW5jdGlvbk5hbWU6IHByb2Nlc3NpbmdMYW1iZGEuZnVuY3Rpb25OYW1lIH0sXG4gICAgICBzdGF0aXN0aWM6ICdBdmVyYWdlJyxcbiAgICAgIHBlcmlvZDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMilcbiAgICB9KTtcbiAgICBjb25zdCBqb2JDb21wbGV0aW9uRXhwciA9IG5ldyBjdy5NYXRoRXhwcmVzc2lvbih7XG4gICAgICBleHByZXNzaW9uOiAnSUYocXVldWUgPCAxIEFORCBjb25jdXJyZW50IDwgMSwgMSwgMCknLFxuICAgICAgdXNpbmdNZXRyaWNzOiB7XG4gICAgICAgIHF1ZXVlOiB0YXNrc1F1ZXVlLm1ldHJpY0FwcHJveGltYXRlTnVtYmVyT2ZNZXNzYWdlc1Zpc2libGUoe1xuICAgICAgICAgIHN0YXRpc3RpYzogJ0F2ZXJhZ2UnLFxuICAgICAgICAgIHBlcmlvZDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMilcbiAgICAgICAgfSksXG4gICAgICAgIGNvbmN1cnJlbnQ6IGNvbmN1cnJlbnRFeGVjdXRpb25zTWV0cmljXG4gICAgICB9XG4gICAgfSk7XG4gICAgY29uc3Qgam9iQ29tcGxldGlvbkFsYXJtID0gbmV3IGN3LkFsYXJtKHRoaXMsICdKb2JDb21wbGV0aW9uQWxhcm0nLCB7XG4gICAgICBhbGFybU5hbWU6ICdKb2JDb21wbGV0aW9uRGV0ZWN0ZWQnLFxuICAgICAgYWxhcm1EZXNjcmlwdGlvbjogJ1RyaWdnZXJlZCB3aGVuIGFsbCBqb2JzIGFyZSBwcm9jZXNzZWQgKHF1ZXVlIGVtcHR5IGFuZCBubyBhY3RpdmUgZXhlY3V0aW9ucyknLFxuICAgICAgbWV0cmljOiBqb2JDb21wbGV0aW9uRXhwcixcbiAgICAgIHRocmVzaG9sZDogMSxcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAxLFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuR1JFQVRFUl9USEFOX09SX0VRVUFMX1RPX1RIUkVTSE9MRCxcbiAgICAgIHRyZWF0TWlzc2luZ0RhdGE6IGN3LlRyZWF0TWlzc2luZ0RhdGEuTk9UX0JSRUFDSElOR1xuICAgIH0pO1xuXG4gICAgY29uc3QgYWxlcnRBY3Rpb24gPSBuZXcgY3dhY3Rpb25zLlNuc0FjdGlvbihhbGVydHNUb3BpYyk7XG5cbiAgICBkbHFEZXB0aEFsYXJtLmFkZEFsYXJtQWN0aW9uKGFsZXJ0QWN0aW9uKTtcbiAgICBwcm9jZXNzaW5nRXJyb3JzQWxhcm0uYWRkQWxhcm1BY3Rpb24oYWxlcnRBY3Rpb24pO1xuICAgIHF1ZXVlRGVwdGhBbGFybS5hZGRBbGFybUFjdGlvbihhbGVydEFjdGlvbik7XG4gICAgam9iQ29tcGxldGlvbkFsYXJtLmFkZEFsYXJtQWN0aW9uKGFsZXJ0QWN0aW9uKTtcbiAgICBxdWV1ZUFnZUFsYXJtLmFkZEFsYXJtQWN0aW9uKGFsZXJ0QWN0aW9uKTtcblxuICAgIC8vIENsb3VkV2F0Y2ggRGFzaGJvYXJkXG4gICAgY29uc3QgZGFzaGJvYXJkID0gbmV3IGN3LkRhc2hib2FyZCh0aGlzLCAnUGFyYWxsZWxQcm9jZXNzaW5nRGFzaGJvYXJkJywge1xuICAgICAgZGFzaGJvYXJkTmFtZTogJ0FtaXJhTGFtYmRhUGFyYWxsZWwnXG4gICAgfSk7XG5cbiAgICBkYXNoYm9hcmQuYWRkV2lkZ2V0cyhcbiAgICAgIG5ldyBjdy5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnU1FTIFF1ZXVlIE1ldHJpY3MnLFxuICAgICAgICBsZWZ0OiBbdGFza3NRdWV1ZS5tZXRyaWNBcHByb3hpbWF0ZU51bWJlck9mTWVzc2FnZXNWaXNpYmxlKCldLFxuICAgICAgICByaWdodDogW3Rhc2tzUXVldWUubWV0cmljQXBwcm94aW1hdGVBZ2VPZk9sZGVzdE1lc3NhZ2UoKV0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSksXG4gICAgICBuZXcgY3cuR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0xhbWJkYSBQcm9jZXNzaW5nIE1ldHJpY3MnLFxuICAgICAgICBsZWZ0OiBbcHJvY2Vzc2luZ0xhbWJkYS5tZXRyaWNJbnZvY2F0aW9ucygpLCBwcm9jZXNzaW5nTGFtYmRhLm1ldHJpY0R1cmF0aW9uKCldLFxuICAgICAgICByaWdodDogW3Byb2Nlc3NpbmdMYW1iZGEubWV0cmljRXJyb3JzKCksIHByb2Nlc3NpbmdMYW1iZGEubWV0cmljVGhyb3R0bGVzKCldLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3LkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdMYW1iZGEgQ29uY3VycmVuY3knLFxuICAgICAgICBsZWZ0OiBbY29uY3VycmVudEV4ZWN1dGlvbnNNZXRyaWNdLFxuICAgICAgICB3aWR0aDogMjRcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3LkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdETFEgRGVwdGgnLFxuICAgICAgICBsZWZ0OiBbZGxxLm1ldHJpY0FwcHJveGltYXRlTnVtYmVyT2ZNZXNzYWdlc1Zpc2libGUoKV0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSksXG4gICAgICBuZXcgY3cuR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0pvYiBDb21wbGV0aW9uIFRyYWNraW5nJyxcbiAgICAgICAgbGVmdDogW1xuICAgICAgICAgIG5ldyBjdy5NZXRyaWMoe1xuICAgICAgICAgICAgbmFtZXNwYWNlOiAnQW1pcmEvSm9icycsXG4gICAgICAgICAgICBtZXRyaWNOYW1lOiAnSm9ic0NvbXBsZXRlZCcsXG4gICAgICAgICAgICBzdGF0aXN0aWM6ICdTdW0nXG4gICAgICAgICAgfSksXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7XG4gICAgICAgICAgICBuYW1lc3BhY2U6ICdBbWlyYS9Kb2JzJyxcbiAgICAgICAgICAgIG1ldHJpY05hbWU6ICdKb2JzRmFpbGVkJyxcbiAgICAgICAgICAgIHN0YXRpc3RpYzogJ1N1bSdcbiAgICAgICAgICB9KVxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3LkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdQcm9jZXNzaW5nVGltZSAobXMpJyxcbiAgICAgICAgbGVmdDogW1xuICAgICAgICAgIG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdBbWlyYS9Kb2JzJywgbWV0cmljTmFtZTogJ1Byb2Nlc3NpbmdUaW1lJywgc3RhdGlzdGljOiAnQXZlcmFnZScgfSlcbiAgICAgICAgXSxcbiAgICAgICAgcmlnaHQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQW1pcmEvSm9icycsIG1ldHJpY05hbWU6ICdQcm9jZXNzaW5nVGltZScsIHN0YXRpc3RpYzogJ3A5NScgfSlcbiAgICAgICAgXSxcbiAgICAgICAgd2lkdGg6IDEyXG4gICAgICB9KSxcbiAgICAgIG5ldyBjdy5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnSW5mZXJlbmNlIFRvdGFsIChwOTUgbXMpJyxcbiAgICAgICAgbGVmdDogW1xuICAgICAgICAgIG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdBbWlyYS9JbmZlcmVuY2UnLCBtZXRyaWNOYW1lOiAnSW5mZXJlbmNlVG90YWxNcycsIHN0YXRpc3RpYzogJ3A5NScgfSlcbiAgICAgICAgXSxcbiAgICAgICAgd2lkdGg6IDEyXG4gICAgICB9KSxcbiAgICAgIG5ldyBjdy5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnQWN0aXZpdHkgVG90YWwgKHA5NSBtcyknLFxuICAgICAgICBsZWZ0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0FtaXJhL0FjdGl2aXR5JywgbWV0cmljTmFtZTogJ0FjdGl2aXR5VG90YWxNcycsIHN0YXRpc3RpYzogJ3A5NScgfSlcbiAgICAgICAgXSxcbiAgICAgICAgd2lkdGg6IDEyXG4gICAgICB9KVxuICAgICk7XG5cbiAgICAvLyBPdXRwdXRzXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1Rhc2tzUXVldWVVcmwnLCB7XG4gICAgICB2YWx1ZTogdGFza3NRdWV1ZS5xdWV1ZVVybCxcbiAgICAgIGRlc2NyaXB0aW9uOiAnU1FTIFRhc2tzIFF1ZXVlIFVSTCdcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdQcm9jZXNzaW5nTGFtYmRhQXJuJywge1xuICAgICAgdmFsdWU6IHByb2Nlc3NpbmdMYW1iZGEuZnVuY3Rpb25Bcm4sXG4gICAgICBkZXNjcmlwdGlvbjogJ1Byb2Nlc3NpbmcgTGFtYmRhIEZ1bmN0aW9uIEFSTidcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdSZXN1bHRzQnVja2V0TmFtZScsIHtcbiAgICAgIHZhbHVlOiByZXN1bHRzQnVja2V0LmJ1Y2tldE5hbWUsXG4gICAgICBkZXNjcmlwdGlvbjogJ1MzIFJlc3VsdHMgQnVja2V0IE5hbWUnXG4gICAgfSk7XG5cbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnTWFudWFsVHJpZ2dlckZ1bmN0aW9uTmFtZScsIHtcbiAgICAgIHZhbHVlOiBtYW51YWxUcmlnZ2VyTGFtYmRhLmZ1bmN0aW9uTmFtZSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnTWFudWFsIHRyaWdnZXIgTGFtYmRhIGZ1bmN0aW9uIG5hbWUgKHVzZSB3aXRoIEFXUyBDTEkpJ1xuICAgIH0pO1xuXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ0Rhc2hib2FyZFVybCcsIHtcbiAgICAgIHZhbHVlOiBgaHR0cHM6Ly8ke3RoaXMucmVnaW9ufS5jb25zb2xlLmF3cy5hbWF6b24uY29tL2Nsb3Vkd2F0Y2gvaG9tZT9yZWdpb249JHt0aGlzLnJlZ2lvbn0jZGFzaGJvYXJkczpuYW1lPSR7ZGFzaGJvYXJkLmRhc2hib2FyZE5hbWV9YCxcbiAgICAgIGRlc2NyaXB0aW9uOiAnQ2xvdWRXYXRjaCBEYXNoYm9hcmQgVVJMJ1xuICAgIH0pO1xuICB9XG59XG4iXX0=
