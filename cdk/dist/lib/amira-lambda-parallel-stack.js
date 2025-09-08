"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.AmiraLambdaParallelStack = void 0;
const cdk = require("aws-cdk-lib");
const ec2 = require("aws-cdk-lib/aws-ec2");
const s3 = require("aws-cdk-lib/aws-s3");
const aws_cdk_lib_1 = require("aws-cdk-lib");
// Import our new constructs
const storage_1 = require("./constructs/storage");
const queueing_1 = require("./constructs/queueing");
const parallel_processing_lambda_1 = require("./constructs/parallel-processing-lambda");
const observability_1 = require("./constructs/observability");
const cross_stack_linking_1 = require("./constructs/cross-stack-linking");
const security_standards_1 = require("./aspects/security-standards");
const cdk_nag_integration_1 = require("./aspects/cdk-nag-integration");
const stages_1 = require("./config/stages");
class AmiraLambdaParallelStack extends cdk.Stack {
    constructor(scope, id, props = {}) {
        super(scope, id, props);
        // Get stage configuration
        const stage = props.stage || process.env.STAGE || 'dev';
        const stageConfig = props.stageConfig || stages_1.StageConfigs.getConfig(stage);
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
        const storage = new storage_1.StorageConstruct(this, 'Storage', {
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
        const queueing = new queueing_1.QueueingConstruct(this, 'Queueing', {
            kmsKey: storage.kmsKey,
            visibilityTimeout: cdk.Duration.hours(2),
            receiveMessageWaitTimeSeconds: stageConfig.queueing.maxBatchingWindowSeconds
        });
        // Get VPC configuration from cross-stack linking if Triton is enabled
        let vpcConfig;
        if (stageConfig.features.enableTriton) {
            try {
                const crossStackConfig = cross_stack_linking_1.CrossStackLinkingConstruct.getVpcConfigFromSsm(this);
                const vpc = ec2.Vpc.fromLookup(this, 'ImportedVpc', { vpcId: crossStackConfig.vpcId });
                const subnets = crossStackConfig.privateSubnetIds.map((id, index) => ec2.Subnet.fromSubnetId(this, `ImportedSubnet${index}`, id));
                const securityGroup = ec2.SecurityGroup.fromSecurityGroupId(this, 'ImportedSecurityGroup', crossStackConfig.albSecurityGroupId);
                vpcConfig = { vpc, subnets, securityGroup };
            }
            catch (error) {
                const errorMessage = `Failed to resolve VPC configuration from SSM: ${error}`;
                // For production stages, VPC attachment failure should be treated as an error
                if (stage === 'prod' || stage === 'production') {
                    throw new Error(`CRITICAL: ${errorMessage}. Production Lambda functions must be VPC-attached.`);
                }
                // For non-production, log detailed warning
                aws_cdk_lib_1.Annotations.of(this).addWarning(`${errorMessage}. Lambda will run without VPC attachment in ${stage} environment.`);
                // Add metadata for debugging
                this.node.addMetadata('vpc-resolution-error', {
                    error: String(error),
                    stage,
                    timestamp: new Date().toISOString()
                });
            }
        }
        // Resolve Triton URL from SSM (parameters removed to fix token branching)
        let tritonUrl;
        if (stageConfig.features.enableTriton) {
            tritonUrl = cross_stack_linking_1.CrossStackLinkingConstruct.getTritonUrlFromSsm(this);
        }
        // Parallel Processing Lambda construct
        const parallelProcessing = new parallel_processing_lambda_1.ParallelProcessingLambdaConstruct(this, 'ParallelProcessing', {
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
        const observability = new observability_1.ObservabilityConstruct(this, 'Observability', {
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
            new cross_stack_linking_1.CrossStackLinkingConstruct(this, 'CrossStackLinking', {
                enableTritonUrlValidation: true
            });
        }
        // Apply security standards and compliance aspects
        cdk.Aspects.of(this).add(new security_standards_1.SecurityStandardsAspect());
        cdk.Aspects.of(this).add(new cdk_nag_integration_1.CdkNagIntegrationAspect());
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
exports.AmiraLambdaParallelStack = AmiraLambdaParallelStack;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiYW1pcmEtbGFtYmRhLXBhcmFsbGVsLXN0YWNrLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsiLi4vLi4vbGliL2FtaXJhLWxhbWJkYS1wYXJhbGxlbC1zdGFjay50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOzs7QUFBQSxtQ0FBbUM7QUFDbkMsMkNBQTJDO0FBQzNDLHlDQUF5QztBQUV6Qyw2Q0FBMEM7QUFFMUMsNEJBQTRCO0FBQzVCLGtEQUF3RDtBQUN4RCxvREFBMEQ7QUFDMUQsd0ZBQTRGO0FBQzVGLDhEQUFvRTtBQUNwRSwwRUFBOEU7QUFDOUUscUVBQXVFO0FBQ3ZFLHVFQUF3RTtBQUN4RSw0Q0FBNEQ7QUFPNUQsTUFBYSx3QkFBeUIsU0FBUSxHQUFHLENBQUMsS0FBSztJQUNyRCxZQUFZLEtBQWdCLEVBQUUsRUFBVSxFQUFFLFFBQXVDLEVBQUU7UUFDakYsS0FBSyxDQUFDLEtBQUssRUFBRSxFQUFFLEVBQUUsS0FBSyxDQUFDLENBQUM7UUFFeEIsMEJBQTBCO1FBQzFCLE1BQU0sS0FBSyxHQUFHLEtBQUssQ0FBQyxLQUFLLElBQUksT0FBTyxDQUFDLEdBQUcsQ0FBQyxLQUFLLElBQUksS0FBSyxDQUFDO1FBQ3hELE1BQU0sV0FBVyxHQUFHLEtBQUssQ0FBQyxXQUFXLElBQUkscUJBQVksQ0FBQyxTQUFTLENBQUMsS0FBSyxDQUFDLENBQUM7UUFFdkUsOERBQThEO1FBQzlELE1BQU0sY0FBYyxHQUFHLElBQUksR0FBRyxDQUFDLFlBQVksQ0FBQyxJQUFJLEVBQUUsV0FBVyxFQUFFO1lBQzdELElBQUksRUFBRSxRQUFRO1lBQ2QsT0FBTyxFQUFFLDZCQUE2QjtZQUN0QyxXQUFXLEVBQUUsNEJBQTRCO1NBQzFDLENBQUMsQ0FBQztRQUVILE1BQU0sa0JBQWtCLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxlQUFlLEVBQUU7WUFDckUsSUFBSSxFQUFFLFFBQVE7WUFDZCxPQUFPLEVBQUUsVUFBVTtZQUNuQixXQUFX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RSxrQkFBa0IsQ0FBQyxnQkFBZ0IsQ0FBQyxXQUFXO1lBQ3RELFdBQVcsRUFBRSxnQ0FBZ0M7U0FDOUMsQ0FBQyxDQUFDO1FBRUgsSUFBSSxHQUFHLENBQUMsU0FBUyxDQUFDLElBQUksRUFBRSxtQkFBbUIsRUFBRTtZQUMzQyxLQUFLLEVBQUUsT0FBTyxDQUFDLGFBQWEsQ0FBQyxVQUFVO1lBQ3ZDLFdBQVcsRUFBRSx3QkFBd0I7U0FDdEMsQ0FBQyxDQUFDO1FBRUgsSUFBSSxHQUFHLENBQUMsU0FBUyxDQUFDLElBQUksRUFBRSwyQkFBMkIsRUFBRTtZQUNuRCxLQUFLLEVBQUUsa0JBQWtCLENBQUMsbUJBQW1CLENBQUMsWUFBWTtZQUMxRCxXQUFXLEVBQUUsd0RBQXdEO1NBQ3RFLENBQUMsQ0FBQztRQUVILElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsY0FBYyxFQUFFO1lBQ3RDLEtBQUssRUFBRSxXQUFXLElBQUksQ0FBQyxNQUFNLGtEQUFrRCxJQUFJLENBQUMsTUFBTSxvQkFBb0IsYUFBYSxDQUFDLFNBQVMsQ0FBQyxhQUFhLEVBQUU7WUFDckosV0FBVyxFQUFFLDBCQUEwQjtTQUN4QyxDQUFDLENBQUM7SUFDTCxDQUFDO0NBQ0Y7QUFuTUQsNERBbU1DIiwic291cmNlc0NvbnRlbnQiOlsiaW1wb3J0ICogYXMgY2RrIGZyb20gJ2F3cy1jZGstbGliJztcbmltcG9ydCAqIGFzIGVjMiBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZWMyJztcbmltcG9ydCAqIGFzIHMzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zMyc7XG5pbXBvcnQgeyBDb25zdHJ1Y3QgfSBmcm9tICdjb25zdHJ1Y3RzJztcbmltcG9ydCB7IEFubm90YXRpb25zIH0gZnJvbSAnYXdzLWNkay1saWInO1xuXG4vLyBJbXBvcnQgb3VyIG5ldyBjb25zdHJ1Y3RzXG5pbXBvcnQgeyBTdG9yYWdlQ29uc3RydWN0IH0gZnJvbSAnLi9jb25zdHJ1Y3RzL3N0b3JhZ2UnO1xuaW1wb3J0IHsgUXVldWVpbmdDb25zdHJ1Y3QgfSBmcm9tICcuL2NvbnN0cnVjdHMvcXVldWVpbmcnO1xuaW1wb3J0IHsgUGFyYWxsZWxQcm9jZXNzaW5nTGFtYmRhQ29uc3RydWN0IH0gZnJvbSAnLi9jb25zdHJ1Y3RzL3BhcmFsbGVsLXByb2Nlc3NpbmctbGFtYmRhJztcbmltcG9ydCB7IE9ic2VydmFiaWxpdHlDb25zdHJ1Y3QgfSBmcm9tICcuL2NvbnN0cnVjdHMvb2JzZXJ2YWJpbGl0eSc7XG5pbXBvcnQgeyBDcm9zc1N0YWNrTGlua2luZ0NvbnN0cnVjdCB9IGZyb20gJy4vY29uc3RydWN0cy9jcm9zcy1zdGFjay1saW5raW5nJztcbmltcG9ydCB7IFNlY3VyaXR5U3RhbmRhcmRzQXNwZWN0IH0gZnJvbSAnLi9hc3BlY3RzL3NlY3VyaXR5LXN0YW5kYXJkcyc7XG5pbXBvcnQgeyBDZGtOYWdJbnRlZ3JhdGlvbkFzcGVjdCB9IGZyb20gJy4vYXNwZWN0cy9jZGstbmFnLWludGVncmF0aW9uJztcbmltcG9ydCB7IFN0YWdlQ29uZmlncywgU3RhZ2VDb25maWcgfSBmcm9tICcuL2NvbmZpZy9zdGFnZXMnO1xuXG5leHBvcnQgaW50ZXJmYWNlIEFtaXJhTGFtYmRhUGFyYWxsZWxTdGFja1Byb3BzIGV4dGVuZHMgY2RrLlN0YWNrUHJvcHMge1xuICByZWFkb25seSBzdGFnZT86IHN0cmluZztcbiAgcmVhZG9ubHkgc3RhZ2VDb25maWc/OiBTdGFnZUNvbmZpZztcbn1cblxuZXhwb3J0IGNsYXNzIEFtaXJhTGFtYmRhUGFyYWxsZWxTdGFjayBleHRlbmRzIGNkay5TdGFjayB7XG4gIGNvbnN0cnVjdG9yKHNjb3BlOiBDb25zdHJ1Y3QsIGlkOiBzdHJpbmcsIHByb3BzOiBBbWlyYUxhbWJkYVBhcmFsbGVsU3RhY2tQcm9wcyA9IHt9KSB7XG4gICAgc3VwZXIoc2NvcGUsIGlkLCBwcm9wcyk7XG5cbiAgICAvLyBHZXQgc3RhZ2UgY29uZmlndXJhdGlvblxuICAgIGNvbnN0IHN0YWdlID0gcHJvcHMuc3RhZ2UgfHwgcHJvY2Vzcy5lbnYuU1RBR0UgfHwgJ2Rldic7XG4gICAgY29uc3Qgc3RhZ2VDb25maWcgPSBwcm9wcy5zdGFnZUNvbmZpZyB8fCBTdGFnZUNvbmZpZ3MuZ2V0Q29uZmlnKHN0YWdlKTtcblxuICAgIC8vIFBhcmFtZXRlcnMgZm9yIGRlcGxveW1lbnQtdGltZSBjb25maWd1cmF0aW9uIChtdWNoIHJlZHVjZWQpXG4gICAgY29uc3QgbW9kZWxQYXRoUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnTW9kZWxQYXRoJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnZmFjZWJvb2svd2F2MnZlYzItYmFzZS05NjBoJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnSEYgbW9kZWwgcGF0aCBmb3IgV2F2MlZlYzInXG4gICAgfSk7XG5cbiAgICBjb25zdCByZXN1bHRzUHJlZml4UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnUmVzdWx0c1ByZWZpeCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3Jlc3VsdHMvJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnUzMga2V5IHByZWZpeCBmb3IgcmVzdWx0cyB3cml0ZXMnXG4gICAgfSk7XG5cbiAgICAvLyBBdWRpbyBidWNrZXQgY29uZmlndXJhdGlvbiBtb3ZlZCB0byBzdGFnZSBjb25maWcgdG8gZml4IHRva2VuIGJyYW5jaGluZ1xuXG4gICAgY29uc3Qgc2xhY2tXZWJob29rUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnU2xhY2tXZWJob29rVXJsJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnU2xhY2sgd2ViaG9vayBVUkwgZm9yIGpvYiBjb21wbGV0aW9uIGFuZCBlcnJvciBub3RpZmljYXRpb25zJyxcbiAgICAgIG5vRWNobzogdHJ1ZVxuICAgIH0pO1xuXG4gICAgLy8gVHJpdG9uIGNsdXN0ZXIgVVJMIGF1dG8tcmVzb2x2ZWQgZnJvbSBTU00gcGFyYW1ldGVyIC9hbWlyYS90cml0b25fYWxiX3VybCAocmVtb3ZlZCBwYXJhbWV0ZXIgdG8gZml4IHRva2VuIGJyYW5jaGluZylcblxuICAgIC8vIFN0b3JhZ2UgKEtNUyBrZXksIGJ1Y2tldHMgd2l0aCBzZWN1cml0eSBwb2xpY2llcylcbiAgICBjb25zdCBzdG9yYWdlID0gbmV3IFN0b3JhZ2VDb25zdHJ1Y3QodGhpcywgJ1N0b3JhZ2UnLCB7XG4gICAgICBrbXNLZXlBbGlhczogYGFsaWFzL2FtaXJhLWxhbWJkYS1wYXJhbGxlbC0ke3N0YWdlfWAsXG4gICAgICBlbmFibGVWZXJzaW9uaW5nOiBmYWxzZSwgLy8gQ29zdCBvcHRpbWl6YXRpb24gZm9yIExhbWJkYSBzdGFja1xuICAgICAgbGlmZWN5Y2xlUnVsZXM6IFtcbiAgICAgICAge1xuICAgICAgICAgIGlkOiAnSW50ZWxsaWdlbnRUaWVyaW5nTm93JyxcbiAgICAgICAgICBlbmFibGVkOiB0cnVlLFxuICAgICAgICAgIHRyYW5zaXRpb25zOiBbXG4gICAgICAgICAgICB7IHN0b3JhZ2VDbGFzczogczMuU3RvcmFnZUNsYXNzLklOVEVMTElHRU5UX1RJRVJJTkcsIHRyYW5zaXRpb25BZnRlcjogY2RrLkR1cmF0aW9uLmRheXMoMCkgfVxuICAgICAgICAgIF1cbiAgICAgICAgfSxcbiAgICAgICAge1xuICAgICAgICAgIGlkOiAnVHJhbnNpdGlvblRvSUEzMGQnLFxuICAgICAgICAgIGVuYWJsZWQ6IHRydWUsXG4gICAgICAgICAgdHJhbnNpdGlvbnM6IFtcbiAgICAgICAgICAgIHsgc3RvcmFnZUNsYXNzOiBzMy5TdG9yYWdlQ2xhc3MuSU5GUkVRVUVOVF9BQ0NFU1MsIHRyYW5zaXRpb25BZnRlcjogY2RrLkR1cmF0aW9uLmRheXMoMzApIH1cbiAgICAgICAgICBdXG4gICAgICAgIH0sXG4gICAgICAgIHtcbiAgICAgICAgICBpZDogJ1RyYW5zaXRpb25Ub0dsYWNpZXIxMjBkJyxcbiAgICAgICAgICBlbmFibGVkOiB0cnVlLFxuICAgICAgICAgIHRyYW5zaXRpb25zOiBbXG4gICAgICAgICAgICB7IHN0b3JhZ2VDbGFzczogczMuU3RvcmFnZUNsYXNzLkdMQUNJRVJfSU5TVEFOVF9SRVRSSUVWQUwsIHRyYW5zaXRpb25BZnRlcjogY2RrLkR1cmF0aW9uLmRheXMoMTIwKSB9XG4gICAgICAgICAgXVxuICAgICAgICB9XG4gICAgICBdXG4gICAgfSk7XG5cbiAgICAvLyBRdWV1ZWluZyAoU1FTIHdpdGggRExRLCAyLWhvdXIgdmlzaWJpbGl0eSB0aW1lb3V0IGZvciBwcm9jZXNzaW5nKVxuICAgIGNvbnN0IHF1ZXVlaW5nID0gbmV3IFF1ZXVlaW5nQ29uc3RydWN0KHRoaXMsICdRdWV1ZWluZycsIHtcbiAgICAgIGttc0tleTogc3RvcmFnZS5rbXNLZXksXG4gICAgICB2aXNpYmlsaXR5VGltZW91dDogY2RrLkR1cmF0aW9uLmhvdXJzKDIpLFxuICAgICAgcmVjZWl2ZU1lc3NhZ2VXYWl0VGltZVNlY29uZHM6IHN0YWdlQ29uZmlnLnF1ZXVlaW5nLm1heEJhdGNoaW5nV2luZG93U2Vjb25kc1xuICAgIH0pO1xuXG4gICAgLy8gR2V0IFZQQyBjb25maWd1cmF0aW9uIGZyb20gY3Jvc3Mtc3RhY2sgbGlua2luZyBpZiBUcml0b24gaXMgZW5hYmxlZFxuICAgIGxldCB2cGNDb25maWc6IHsgdnBjOiBlYzIuSVZwYzsgc3VibmV0czogZWMyLklTdWJuZXRbXTsgc2VjdXJpdHlHcm91cD86IGVjMi5JU2VjdXJpdHlHcm91cCB9IHwgdW5kZWZpbmVkO1xuXG4gICAgaWYgKHN0YWdlQ29uZmlnLmZlYXR1cmVzLmVuYWJsZVRyaXRvbikge1xuICAgICAgdHJ5IHtcbiAgICAgICAgY29uc3QgY3Jvc3NTdGFja0NvbmZpZyA9IENyb3NzU3RhY2tMaW5raW5nQ29uc3RydWN0LmdldFZwY0NvbmZpZ0Zyb21Tc20odGhpcyk7XG4gICAgICAgIGNvbnN0IHZwYyA9IGVjMi5WcGMuZnJvbUxvb2t1cCh0aGlzLCAnSW1wb3J0ZWRWcGMnLCB7IHZwY0lkOiBjcm9zc1N0YWNrQ29uZmlnLnZwY0lkIH0pO1xuICAgICAgICBjb25zdCBzdWJuZXRzID0gY3Jvc3NTdGFja0NvbmZpZy5wcml2YXRlU3VibmV0SWRzLm1hcCgoaWQsIGluZGV4KSA9PlxuICAgICAgICAgIGVjMi5TdWJuZXQuZnJvbVN1Ym5ldElkKHRoaXMsIGBJbXBvcnRlZFN1Ym5ldCR7aW5kZXh9YCwgaWQpXG4gICAgICAgICk7XG4gICAgICAgIGNvbnN0IHNlY3VyaXR5R3JvdXAgPSBlYzIuU2VjdXJpdHlHcm91cC5mcm9tU2VjdXJpdHlHcm91cElkKFxuICAgICAgICAgIHRoaXMsXG4gICAgICAgICAgJ0ltcG9ydGVkU2VjdXJpdHlHcm91cCcsXG4gICAgICAgICAgY3Jvc3NTdGFja0NvbmZpZy5hbGJTZWN1cml0eUdyb3VwSWRcbiAgICAgICAgKTtcblxuICAgICAgICB2cGNDb25maWcgPSB7IHZwYywgc3VibmV0cywgc2VjdXJpdHlHcm91cCB9O1xuICAgICAgfSBjYXRjaCAoZXJyb3IpIHtcbiAgICAgICAgY29uc3QgZXJyb3JNZXNzYWdlID0gYEZhaWxlZCB0byByZXNvbHZlIFZQQyBjb25maWd1cmF0aW9uIGZyb20gU1NNOiAke2Vycm9yfWA7XG5cbiAgICAgICAgLy8gRm9yIHByb2R1Y3Rpb24gc3RhZ2VzLCBWUEMgYXR0YWNobWVudCBmYWlsdXJlIHNob3VsZCBiZSB0cmVhdGVkIGFzIGFuIGVycm9yXG4gICAgICAgIGlmIChzdGFnZSA9PT0gJ3Byb2QnIHx8IHN0YWdlID09PSAncHJvZHVjdGlvbicpIHtcbiAgICAgICAgICB0aHJvdyBuZXcgRXJyb3IoYENSSVRJQ0FMOiAke2Vycm9yTWVzc2FnZX0uIFByb2R1Y3Rpb24gTGFtYmRhIGZ1bmN0aW9ucyBtdXN0IGJlIFZQQy1hdHRhY2hlZC5gKTtcbiAgICAgICAgfVxuXG4gICAgICAgIC8vIEZvciBub24tcHJvZHVjdGlvbiwgbG9nIGRldGFpbGVkIHdhcm5pbmdcbiAgICAgICAgQW5ub3RhdGlvbnMub2YodGhpcykuYWRkV2FybmluZyhgJHtlcnJvck1lc3NhZ2V9LiBMYW1iZGEgd2lsbCBydW4gd2l0aG91dCBWUEMgYXR0YWNobWVudCBpbiAke3N0YWdlfSBlbnZpcm9ubWVudC5gKTtcblxuICAgICAgICAvLyBBZGQgbWV0YWRhdGEgZm9yIGRlYnVnZ2luZ1xuICAgICAgICB0aGlzLm5vZGUuYWRkTWV0YWRhdGEoJ3ZwYy1yZXNvbHV0aW9uLWVycm9yJywge1xuICAgICAgICAgIGVycm9yOiBTdHJpbmcoZXJyb3IpLFxuICAgICAgICAgIHN0YWdlLFxuICAgICAgICAgIHRpbWVzdGFtcDogbmV3IERhdGUoKS50b0lTT1N0cmluZygpXG4gICAgICAgIH0pO1xuICAgICAgfVxuICAgIH1cblxuICAgIC8vIFJlc29sdmUgVHJpdG9uIFVSTCBmcm9tIFNTTSAocGFyYW1ldGVycyByZW1vdmVkIHRvIGZpeCB0b2tlbiBicmFuY2hpbmcpXG4gICAgbGV0IHRyaXRvblVybDogc3RyaW5nIHwgdW5kZWZpbmVkO1xuICAgIGlmIChzdGFnZUNvbmZpZy5mZWF0dXJlcy5lbmFibGVUcml0b24pIHtcbiAgICAgIHRyaXRvblVybCA9IENyb3NzU3RhY2tMaW5raW5nQ29uc3RydWN0LmdldFRyaXRvblVybEZyb21Tc20odGhpcyk7XG4gICAgfVxuXG4gICAgLy8gUGFyYWxsZWwgUHJvY2Vzc2luZyBMYW1iZGEgY29uc3RydWN0XG4gICAgY29uc3QgcGFyYWxsZWxQcm9jZXNzaW5nID0gbmV3IFBhcmFsbGVsUHJvY2Vzc2luZ0xhbWJkYUNvbnN0cnVjdCh0aGlzLCAnUGFyYWxsZWxQcm9jZXNzaW5nJywge1xuICAgICAgcmVzdWx0c0J1Y2tldDogc3RvcmFnZS5yZXN1bHRzQnVja2V0LFxuICAgICAga21zS2V5OiBzdG9yYWdlLmttc0tleSxcbiAgICAgIHRhc2tzUXVldWU6IHF1ZXVlaW5nLnF1ZXVlLFxuICAgICAgZGxxOiBxdWV1ZWluZy5kbHEsXG4gICAgICB2cGM6IHZwY0NvbmZpZyxcbiAgICAgIGZlYXR1cmVzOiB7XG4gICAgICAgIGVuYWJsZVRyaXRvbjogc3RhZ2VDb25maWcuZmVhdHVyZXMuZW5hYmxlVHJpdG9uLFxuICAgICAgICBlbmFibGVXYXJtaW5nOiBzdGFnZUNvbmZpZy5mZWF0dXJlcy5lbmFibGVXYXJtaW5nLFxuICAgICAgICB0cml0b25VcmwsXG4gICAgICAgIGF0aGVuYUNsZWFudXA6IHN0YWdlQ29uZmlnLmZlYXR1cmVzLmVuYWJsZUF0aGVuYUNsZWFudXAgJiYgc3RhZ2VDb25maWcuYXRoZW5hPy5jbGVhbnVwID8ge1xuICAgICAgICAgIGVuYWJsZWQ6IHRydWUsXG4gICAgICAgICAgLi4uc3RhZ2VDb25maWcuYXRoZW5hLmNsZWFudXBcbiAgICAgICAgfSA6IHVuZGVmaW5lZFxuICAgICAgfSxcbiAgICAgIGNvbmZpZzoge1xuICAgICAgICBtb2RlbFBhdGg6IG1vZGVsUGF0aFBhcmFtLnZhbHVlQXNTdHJpbmcsXG4gICAgICAgIHJlc3VsdHNQcmVmaXg6IHJlc3VsdHNQcmVmaXhQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBhdWRpb0J1Y2tldDogc3RhZ2VDb25maWcuc3RvcmFnZT8uYXVkaW9CdWNrZXQsXG4gICAgICAgIHNsYWNrV2ViaG9va1VybDogc2xhY2tXZWJob29rUGFyYW0udmFsdWVBc1N0cmluZyB8fCB1bmRlZmluZWQsXG4gICAgICAgIG1heENvbmN1cnJlbmN5OiBzdGFnZUNvbmZpZy5jb21wdXRlLmxhbWJkYS5tYXhDb25jdXJyZW5jeSxcbiAgICAgICAgbWF4RXZlbnRTb3VyY2VDb25jdXJyZW5jeTogc3RhZ2VDb25maWcuY29tcHV0ZS5sYW1iZGEubWF4RXZlbnRTb3VyY2VDb25jdXJyZW5jeSxcbiAgICAgICAgbWF4QmF0Y2hpbmdXaW5kb3dTZWNvbmRzOiBzdGFnZUNvbmZpZy5xdWV1ZWluZy5tYXhCYXRjaGluZ1dpbmRvd1NlY29uZHMsXG4gICAgICAgIGF0aGVuYTogc3RhZ2VDb25maWcuYXRoZW5hID8ge1xuICAgICAgICAgIGRhdGFiYXNlOiBzdGFnZUNvbmZpZy5hdGhlbmEuZGF0YWJhc2UsXG4gICAgICAgICAgb3V0cHV0OiBzdGFnZUNvbmZpZy5hdGhlbmEub3V0cHV0LFxuICAgICAgICAgIHF1ZXJ5OiBzdGFnZUNvbmZpZy5hdGhlbmEucXVlcnlcbiAgICAgICAgfSA6IHVuZGVmaW5lZCxcbiAgICAgICAgd2FybVJhdGVNaW51dGVzOiBzdGFnZUNvbmZpZy5vYnNlcnZhYmlsaXR5Lndhcm1SYXRlTWludXRlc1xuICAgICAgfVxuICAgIH0pO1xuXG4gICAgLy8gT2JzZXJ2YWJpbGl0eSAoYWxhcm1zLCBkYXNoYm9hcmRzKVxuICAgIGNvbnN0IG9ic2VydmFiaWxpdHkgPSBuZXcgT2JzZXJ2YWJpbGl0eUNvbnN0cnVjdCh0aGlzLCAnT2JzZXJ2YWJpbGl0eScsIHtcbiAgICAgIGFsYXJtRW1haWw6IHN0YWdlQ29uZmlnLm9ic2VydmFiaWxpdHkuYWxhcm1FbWFpbCxcbiAgICAgIGRhc2hib2FyZE5hbWU6IGBBbWlyYUxhbWJkYVBhcmFsbGVsLSR7c3RhZ2V9YCxcbiAgICAgIG1ldHJpY1NvdXJjZXM6IHtcbiAgICAgICAgcHJvY2Vzc2luZ0xhbWJkYTogcGFyYWxsZWxQcm9jZXNzaW5nLnByb2Nlc3NpbmdMYW1iZGEsXG4gICAgICAgIGVucXVldWVMYW1iZGE6IHBhcmFsbGVsUHJvY2Vzc2luZy5lbnF1ZXVlTGFtYmRhLFxuICAgICAgICBtYW51YWxUcmlnZ2VyTGFtYmRhOiBwYXJhbGxlbFByb2Nlc3NpbmcubWFudWFsVHJpZ2dlckxhbWJkYSxcbiAgICAgICAgc2xhY2tOb3RpZmllckxhbWJkYTogcGFyYWxsZWxQcm9jZXNzaW5nLnNsYWNrTm90aWZpZXJMYW1iZGEsXG4gICAgICAgIHF1ZXVlOiBxdWV1ZWluZy5xdWV1ZSxcbiAgICAgICAgZGxxOiBxdWV1ZWluZy5kbHFcbiAgICAgIH1cbiAgICB9KTtcblxuICAgIC8vIENyb3NzLXN0YWNrIGxpbmtpbmcgdmFsaWRhdGlvbiAoaWYgVHJpdG9uIGlzIGVuYWJsZWQgYnV0IFVSTCBub3QgcHJvdmlkZWQgdmlhIHN0YWdlIGNvbmZpZylcbiAgICBpZiAoc3RhZ2VDb25maWcuZmVhdHVyZXMuZW5hYmxlVHJpdG9uICYmICF0cml0b25VcmwpIHtcbiAgICAgIG5ldyBDcm9zc1N0YWNrTGlua2luZ0NvbnN0cnVjdCh0aGlzLCAnQ3Jvc3NTdGFja0xpbmtpbmcnLCB7XG4gICAgICAgIGVuYWJsZVRyaXRvblVybFZhbGlkYXRpb246IHRydWVcbiAgICAgIH0pO1xuICAgIH1cblxuICAgIC8vIEFwcGx5IHNlY3VyaXR5IHN0YW5kYXJkcyBhbmQgY29tcGxpYW5jZSBhc3BlY3RzXG4gICAgY2RrLkFzcGVjdHMub2YodGhpcykuYWRkKG5ldyBTZWN1cml0eVN0YW5kYXJkc0FzcGVjdCgpKTtcbiAgICBjZGsuQXNwZWN0cy5vZih0aGlzKS5hZGQobmV3IENka05hZ0ludGVncmF0aW9uQXNwZWN0KCkpO1xuXG4gICAgLy8gT3V0cHV0c1xuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdUYXNrc1F1ZXVlVXJsJywge1xuICAgICAgdmFsdWU6IHF1ZXVlaW5nLnF1ZXVlLnF1ZXVlVXJsLFxuICAgICAgZGVzY3JpcHRpb246ICdTUVMgVGFza3MgUXVldWUgVVJMJ1xuICAgIH0pO1xuXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1Byb2Nlc3NpbmdMYW1iZGFBcm4nLCB7XG4gICAgICB2YWx1ZTogcGFyYWxsZWxQcm9jZXNzaW5nLnByb2Nlc3NpbmdMYW1iZGEuZnVuY3Rpb25Bcm4sXG4gICAgICBkZXNjcmlwdGlvbjogJ1Byb2Nlc3NpbmcgTGFtYmRhIEZ1bmN0aW9uIEFSTidcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdSZXN1bHRzQnVja2V0TmFtZScsIHtcbiAgICAgIHZhbHVlOiBzdG9yYWdlLnJlc3VsdHNCdWNrZXQuYnVja2V0TmFtZSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnUzMgUmVzdWx0cyBCdWNrZXQgTmFtZSdcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdNYW51YWxUcmlnZ2VyRnVuY3Rpb25OYW1lJywge1xuICAgICAgdmFsdWU6IHBhcmFsbGVsUHJvY2Vzc2luZy5tYW51YWxUcmlnZ2VyTGFtYmRhLmZ1bmN0aW9uTmFtZSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnTWFudWFsIHRyaWdnZXIgTGFtYmRhIGZ1bmN0aW9uIG5hbWUgKHVzZSB3aXRoIEFXUyBDTEkpJ1xuICAgIH0pO1xuXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ0Rhc2hib2FyZFVybCcsIHtcbiAgICAgIHZhbHVlOiBgaHR0cHM6Ly8ke3RoaXMucmVnaW9ufS5jb25zb2xlLmF3cy5hbWF6b24uY29tL2Nsb3Vkd2F0Y2gvaG9tZT9yZWdpb249JHt0aGlzLnJlZ2lvbn0jZGFzaGJvYXJkczpuYW1lPSR7b2JzZXJ2YWJpbGl0eS5kYXNoYm9hcmQuZGFzaGJvYXJkTmFtZX1gLFxuICAgICAgZGVzY3JpcHRpb246ICdDbG91ZFdhdGNoIERhc2hib2FyZCBVUkwnXG4gICAgfSk7XG4gIH1cbn1cbiJdfQ==