"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.AmiraLetterScoringStack = void 0;
const cdk = require("aws-cdk-lib");
const ecr = require("aws-cdk-lib/aws-ecr");
const lambda = require("aws-cdk-lib/aws-lambda");
const logs = require("aws-cdk-lib/aws-logs");
const events = require("aws-cdk-lib/aws-events");
const targets = require("aws-cdk-lib/aws-events-targets");
const iam = require("aws-cdk-lib/aws-iam");
// Import our new constructs
const storage_1 = require("./constructs/storage");
const queueing_1 = require("./constructs/queueing");
const networking_1 = require("./constructs/networking");
const ecs_cluster_1 = require("./constructs/ecs-cluster");
const triton_service_1 = require("./constructs/triton-service");
const observability_1 = require("./constructs/observability");
const cross_stack_linking_1 = require("./constructs/cross-stack-linking");
const security_standards_1 = require("./aspects/security-standards");
const cdk_nag_integration_1 = require("./aspects/cdk-nag-integration");
const stages_1 = require("./config/stages");
class AmiraLetterScoringStack extends cdk.Stack {
    constructor(scope, id, props = {}) {
        super(scope, id, props);
        // Get stage configuration
        const stage = props.stage || process.env.STAGE || 'dev';
        const stageConfig = props.stageConfig || stages_1.StageConfigs.getConfig(stage);
        // Parameters for runtime configuration (reduced from original)
        const tritonImageTagParam = new cdk.CfnParameter(this, 'TritonImageTag', {
            type: 'String',
            default: 'v0.0.0',
            description: 'ECR image tag for Triton container'
        });
        const cwAgentImageTagParam = new cdk.CfnParameter(this, 'CwAgentImageTag', {
            type: 'String',
            default: 'latest',
            description: 'ECR image tag for CloudWatch Agent container'
        });
        const dcgmImageTagParam = new cdk.CfnParameter(this, 'DcgmImageTag', {
            type: 'String',
            default: 'latest',
            description: 'ECR image tag for DCGM exporter container'
        });
        // Parameters replaced with stage configuration for better type safety and token-branching fix
        // ECR Repositories
        const repositories = this.createEcrRepositories();
        // Storage (KMS key, buckets with security policies)
        const storage = new storage_1.StorageConstruct(this, 'Storage', {
            kmsKeyAlias: `alias/amira-letter-scoring-${stage}`,
            enableVersioning: stageConfig.security.enableVersioning
        });
        // Queueing (SQS with DLQ)
        const queueing = new queueing_1.QueueingConstruct(this, 'Queueing', {
            kmsKey: storage.kmsKey
        });
        // Networking (VPC, endpoints, security groups)
        const networking = new networking_1.NetworkingConstruct(this, 'Networking', {
            natGatewayCount: stageConfig.networking.natGatewayCount,
            enableInterfaceEndpoints: stageConfig.features.enableInterfaceEndpoints,
            albClientSecurityGroupId: stageConfig.networking.albClientSecurityGroupId,
            stageName: stageConfig.stageName,
            encryptionKey: storage.kmsKey,
            useNatInstances: stageConfig.networking.useNatInstances
        });
        // ECS Cluster with GPU instances
        const cluster = new ecs_cluster_1.EcsClusterConstruct(this, 'EcsCluster', {
            vpc: networking.vpc,
            instanceTypes: stageConfig.compute.instanceTypes,
            securityGroup: networking.ecsSecurityGroup,
            clusterName: `amira-letter-scoring-${stage}`,
            spotMaxPrice: stageConfig.compute.spotMaxPrice
        });
        // Triton Service (if enabled and certificate ARN is configured)
        let tritonService;
        if (stageConfig.features.enableTriton && stageConfig.security.tritonCertArn) {
            const logGroup = new logs.LogGroup(this, 'TritonLogGroup', {
                logGroupName: `/ecs/amira-letter-scoring-${stage}`,
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY,
                encryptionKey: storage.kmsKey
            });
            tritonService = new triton_service_1.TritonServiceConstruct(this, 'TritonService', {
                cluster: cluster.cluster,
                capacityProviders: cluster.capacityProviders,
                logGroup,
                vpc: networking.vpc,
                albSecurityGroup: networking.albSecurityGroup,
                ecsSecurityGroup: networking.ecsSecurityGroup,
                accessLogsBucket: storage.accessLogsBucket,
                certArn: stageConfig.security.tritonCertArn,
                tls: {
                    enableHttp2: stageConfig.tls?.enableHttp2 ?? true,
                    ciphers: stageConfig.tls?.ciphers,
                    secretArn: stageConfig.tls?.secretArn,
                    requireSecret: stageConfig.tls?.requireSecret ?? false
                },
                repositories: {
                    triton: repositories.triton,
                    cwAgent: repositories.cwAgent,
                    dcgmExporter: repositories.dcgmExporter
                },
                imageTags: {
                    triton: tritonImageTagParam.valueAsString,
                    cwAgent: cwAgentImageTagParam.valueAsString,
                    dcgmExporter: dcgmImageTagParam.valueAsString
                }
            });
        }
        // Manual enqueue Lambda for testing
        const manualEnqueueFn = new lambda.Function(this, 'ManualEnqueueFunction', {
            runtime: lambda.Runtime.PYTHON_3_12,
            handler: 'index.handler',
            code: lambda.Code.fromAsset('../lambda/manual_enqueue'),
            timeout: cdk.Duration.minutes(1),
            tracing: lambda.Tracing.ACTIVE,
            environment: { JOBS_QUEUE_URL: queueing.queue.queueUrl }
        });
        queueing.queue.grantSendMessages(manualEnqueueFn);
        const manualUrl = manualEnqueueFn.addFunctionUrl({
            authType: lambda.FunctionUrlAuthType.AWS_IAM,
            cors: {
                allowedOrigins: ['*'],
                allowedMethods: [lambda.HttpMethod.POST, lambda.HttpMethod.OPTIONS]
            }
        });
        // Spot interruption drain Lambda
        const drainFn = new lambda.Function(this, 'EcsDrainOnSpotFn', {
            runtime: lambda.Runtime.PYTHON_3_12,
            handler: 'index.handler',
            code: lambda.Code.fromAsset('../lambda/ecs_drain_on_spot'),
            timeout: cdk.Duration.seconds(60),
            environment: { CLUSTER_ARN: cluster.cluster.clusterArn }
        });
        drainFn.addToRolePolicy(new iam.PolicyStatement({
            actions: ['ecs:ListContainerInstances', 'ecs:DescribeContainerInstances'],
            resources: [cluster.cluster.clusterArn]
        }));
        drainFn.addToRolePolicy(new iam.PolicyStatement({
            actions: ['ecs:UpdateContainerInstancesState'],
            resources: ['*'],
            conditions: { StringEquals: { 'ecs:cluster': cluster.cluster.clusterArn } }
        }));
        new events.Rule(this, 'SpotInterruptionDrainRule', {
            description: 'Drain ECS container instances on Spot interruption warnings',
            eventPattern: {
                source: ['aws.ec2'],
                detailType: ['EC2 Spot Instance Interruption Warning', 'EC2 Instance Rebalance Recommendation']
            },
            targets: [new targets.LambdaFunction(drainFn)]
        });
        // Observability (alarms, dashboards)
        const observability = new observability_1.ObservabilityConstruct(this, 'Observability', {
            alarmEmail: stageConfig.observability.alarmEmail,
            dashboardName: `AmiraGpuTriton-${stage}`,
            metricSources: {
                manualTriggerLambda: manualEnqueueFn,
                ecsService: tritonService?.service,
                ecsCluster: cluster.cluster,
                queue: queueing.queue,
                dlq: queueing.dlq,
                alb: tritonService?.alb,
                targetGroup: tritonService?.targetGroup,
                asgs: cluster.asgs
            }
        });
        // Cross-stack linking (SSM parameters)
        new cross_stack_linking_1.CrossStackLinkingConstruct(this, 'CrossStackLinking', {
            vpc: networking.vpc,
            albSecurityGroup: networking.albSecurityGroup,
            tritonAlb: tritonService?.alb
        });
        // Apply security standards and compliance aspects
        cdk.Aspects.of(this).add(new security_standards_1.SecurityStandardsAspect());
        cdk.Aspects.of(this).add(new cdk_nag_integration_1.CdkNagIntegrationAspect());
        // Outputs
        new cdk.CfnOutput(this, 'TritonRepositoryUri', {
            value: repositories.triton.repositoryUri,
            description: 'Triton ECR Repository URI'
        });
        new cdk.CfnOutput(this, 'GpuClusterName', {
            value: cluster.cluster.clusterName,
            description: 'GPU ECS Cluster Name'
        });
        if (tritonService) {
            new cdk.CfnOutput(this, 'TritonClusterUrl', {
                value: `https://${tritonService.alb.loadBalancerDnsName}`,
                description: 'HTTPS URL for Triton GPU inference cluster'
            });
            new cdk.CfnOutput(this, 'TritonServiceName', {
                value: tritonService.service.serviceName,
                description: 'Triton ECS Service Name'
            });
        }
        new cdk.CfnOutput(this, 'GpuDashboardUrl', {
            value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=${observability.dashboard.dashboardName}`,
            description: 'CloudWatch GPU/Triton Dashboard URL'
        });
        new cdk.CfnOutput(this, 'ManualEnqueueUrl', {
            value: manualUrl.url,
            description: 'Manual enqueue function URL'
        });
    }
    createEcrRepositories() {
        const app = new ecr.Repository(this, 'AmiraLetterScoringRepo', {
            repositoryName: 'amira-letter-scoring',
            imageScanOnPush: true,
            imageTagMutability: ecr.TagMutability.IMMUTABLE,
            lifecycleRules: [{
                    maxImageCount: 10,
                    description: 'Keep only 10 most recent images'
                }]
        });
        const triton = new ecr.Repository(this, 'TritonServerRepo', {
            repositoryName: 'triton-server',
            imageScanOnPush: true,
            imageTagMutability: ecr.TagMutability.IMMUTABLE,
            lifecycleRules: [{
                    maxImageCount: 5,
                    description: 'Keep only 5 most recent Triton images'
                }]
        });
        const cwAgent = new ecr.Repository(this, 'CloudWatchAgentRepo', {
            repositoryName: 'cloudwatch-agent',
            imageScanOnPush: true,
            imageTagMutability: ecr.TagMutability.IMMUTABLE,
            lifecycleRules: [{
                    maxImageCount: 5,
                    description: 'Keep only 5 most recent CloudWatch Agent images'
                }]
        });
        const dcgmExporter = new ecr.Repository(this, 'DcgmExporterRepo', {
            repositoryName: 'dcgm-exporter',
            imageScanOnPush: true,
            imageTagMutability: ecr.TagMutability.IMMUTABLE,
            lifecycleRules: [{
                    maxImageCount: 5,
                    description: 'Keep only 5 most recent DCGM exporter images'
                }]
        });
        return { app, triton, cwAgent, dcgmExporter };
    }
}
exports.AmiraLetterScoringStack = AmiraLetterScoringStack;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiYW1pcmEtbGV0dGVyLXNjb3Jpbmctc3RhY2suanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi9saWIvYW1pcmEtbGV0dGVyLXNjb3Jpbmctc3RhY2sudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6Ijs7O0FBQUEsbUNBQW1DO0FBQ25DLDJDQUEyQztBQUMzQyxpREFBaUQ7QUFDakQsNkNBQTZDO0FBQzdDLGlEQUFpRDtBQUNqRCwwREFBMEQ7QUFDMUQsMkNBQTJDO0FBRzNDLDRCQUE0QjtBQUM1QixrREFBd0Q7QUFDeEQsb0RBQTBEO0FBQzFELHdEQUE4RDtBQUM5RCwwREFBK0Q7QUFDL0QsZ0VBQXFFO0FBQ3JFLDhEQUFvRTtBQUNwRSwwRUFBOEU7QUFDOUUscUVBQXVFO0FBQ3ZFLHVFQUF3RTtBQUN4RSw0Q0FBNEQ7QUFPNUQsTUFBYSx1QkFBd0IsU0FBUSxHQUFHLENBQUMsS0FBSztJQUNwRCxZQUFZLEtBQWdCLEVBQUUsRUFBVSxFQUFFLFFBQXNDLEVBQUU7UUFDaEYsS0FBSyxDQUFDLEtBQUssRUFBRSxFQUFFLEVBQUUsS0FBSyxDQUFDLENBQUM7UUFFeEIsMEJBQTBCO1FBQzFCLE1BQU0sS0FBSyxHQUFHLEtBQUssQ0FBQyxLQUFLLElBQUksT0FBTyxDQUFDLEdBQUcsQ0FBQyxLQUFLLElBQUksS0FBSyxDQUFDO1FBQ3hELE1BQU0sV0FBVyxHQUFHLEtBQUssQ0FBQyxXQUFXLElBQUkscUJBQVksQ0FBQyxTQUFTLENBQUMsS0FBSyxDQUFDLENBQUM7UUFFdkUsK0RBQStEO1FBQy9ELE1BQU0sbUJBQW1CLEdBQUcsSUFBSSxHQUFHLENBQUMsWUFBWSxDQUFDLElBQUksRUFBRSxnQkFBZ0IsRUFBRTtZQUN2RSxJQUFJLEVBQUUsUUFBUTtZQUNkLE9BQU8sRUFBRSxRQUFRO1lBQ2pCLFdBQVcsRUFBRSxvQ0FBb0M7U0FDbEQsQ0FBQyxDQUFDO1FBRUgsTUFBTSxvQkFBb0IsR0FBRyxJQUFJLEdBQUcsQ0FBQyxZQUFZLENBQUMsSUFBSSxF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Qy9DLGNBQWMsRUFBRSxDQUFDO29CQUNmLGFBQWEsRUFBRSxDQUFDO29CQUNoQixXQUFXLEVBQUUsaURBQWlEO2lCQUMvRCxDQUFDO1NBQ0gsQ0FBQyxDQUFDO1FBRUgsTUFBTSxZQUFZLEdBQUcsSUFBSSxHQUFHLENBQUMsVUFBVSxDQUFDLElBQUksRUFBRSxrQkFBa0IsRUFBRTtZQUNoRSxjQUFjLEVBQUUsZUFBZTtZQUMvQixlQUFlLEVBQUUsSUFBSTtZQUNyQixrQkFBa0IsRUFBRSxHQUFHLENBQUMsYUFBYSxDQUFDLFNBQVM7WUFDL0MsY0FBYyxFQUFFLENBQUM7b0JBQ2YsYUFBYSxFQUFFLENBQUM7b0JBQ2hCLFdBQVcsRUFBRSw4Q0FBOEM7aUJBQzVELENBQUM7U0FDSCxDQUFDLENBQUM7UUFFSCxPQUFPLEVBQUUsR0FBRyxFQUFFLE1BQU0sRUFBRSxPQUFPLEVBQUUsWUFBWSxFQUFFLENBQUM7SUFDaEQsQ0FBQztDQUNGO0FBalFELDBEQWlRQyIsInNvdXJjZXNDb250ZW50IjpbImltcG9ydCAqIGFzIGNkayBmcm9tICdhd3MtY2RrLWxpYic7XG5pbXBvcnQgKiBhcyBlY3IgZnJvbSAnYXdzLWNkay1saWIvYXdzLWVjcic7XG5pbXBvcnQgKiBhcyBsYW1iZGEgZnJvbSAnYXdzLWNkay1saWIvYXdzLWxhbWJkYSc7XG5pbXBvcnQgKiBhcyBsb2dzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1sb2dzJztcbmltcG9ydCAqIGFzIGV2ZW50cyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZXZlbnRzJztcbmltcG9ydCAqIGFzIHRhcmdldHMgZnJvbSAnYXdzLWNkay1saWIvYXdzLWV2ZW50cy10YXJnZXRzJztcbmltcG9ydCAqIGFzIGlhbSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtaWFtJztcbmltcG9ydCB7IENvbnN0cnVjdCB9IGZyb20gJ2NvbnN0cnVjdHMnO1xuXG4vLyBJbXBvcnQgb3VyIG5ldyBjb25zdHJ1Y3RzXG5pbXBvcnQgeyBTdG9yYWdlQ29uc3RydWN0IH0gZnJvbSAnLi9jb25zdHJ1Y3RzL3N0b3JhZ2UnO1xuaW1wb3J0IHsgUXVldWVpbmdDb25zdHJ1Y3QgfSBmcm9tICcuL2NvbnN0cnVjdHMvcXVldWVpbmcnO1xuaW1wb3J0IHsgTmV0d29ya2luZ0NvbnN0cnVjdCB9IGZyb20gJy4vY29uc3RydWN0cy9uZXR3b3JraW5nJztcbmltcG9ydCB7IEVjc0NsdXN0ZXJDb25zdHJ1Y3QgfSBmcm9tICcuL2NvbnN0cnVjdHMvZWNzLWNsdXN0ZXInO1xuaW1wb3J0IHsgVHJpdG9uU2VydmljZUNvbnN0cnVjdCB9IGZyb20gJy4vY29uc3RydWN0cy90cml0b24tc2VydmljZSc7XG5pbXBvcnQgeyBPYnNlcnZhYmlsaXR5Q29uc3RydWN0IH0gZnJvbSAnLi9jb25zdHJ1Y3RzL29ic2VydmFiaWxpdHknO1xuaW1wb3J0IHsgQ3Jvc3NTdGFja0xpbmtpbmdDb25zdHJ1Y3QgfSBmcm9tICcuL2NvbnN0cnVjdHMvY3Jvc3Mtc3RhY2stbGlua2luZyc7XG5pbXBvcnQgeyBTZWN1cml0eVN0YW5kYXJkc0FzcGVjdCB9IGZyb20gJy4vYXNwZWN0cy9zZWN1cml0eS1zdGFuZGFyZHMnO1xuaW1wb3J0IHsgQ2RrTmFnSW50ZWdyYXRpb25Bc3BlY3QgfSBmcm9tICcuL2FzcGVjdHMvY2RrLW5hZy1pbnRlZ3JhdGlvbic7XG5pbXBvcnQgeyBTdGFnZUNvbmZpZ3MsIFN0YWdlQ29uZmlnIH0gZnJvbSAnLi9jb25maWcvc3RhZ2VzJztcblxuZXhwb3J0IGludGVyZmFjZSBBbWlyYUxldHRlclNjb3JpbmdTdGFja1Byb3BzIGV4dGVuZHMgY2RrLlN0YWNrUHJvcHMge1xuICByZWFkb25seSBzdGFnZT86IHN0cmluZztcbiAgcmVhZG9ubHkgc3RhZ2VDb25maWc/OiBTdGFnZUNvbmZpZztcbn1cblxuZXhwb3J0IGNsYXNzIEFtaXJhTGV0dGVyU2NvcmluZ1N0YWNrIGV4dGVuZHMgY2RrLlN0YWNrIHtcbiAgY29uc3RydWN0b3Ioc2NvcGU6IENvbnN0cnVjdCwgaWQ6IHN0cmluZywgcHJvcHM6IEFtaXJhTGV0dGVyU2NvcmluZ1N0YWNrUHJvcHMgPSB7fSkge1xuICAgIHN1cGVyKHNjb3BlLCBpZCwgcHJvcHMpO1xuXG4gICAgLy8gR2V0IHN0YWdlIGNvbmZpZ3VyYXRpb25cbiAgICBjb25zdCBzdGFnZSA9IHByb3BzLnN0YWdlIHx8IHByb2Nlc3MuZW52LlNUQUdFIHx8ICdkZXYnO1xuICAgIGNvbnN0IHN0YWdlQ29uZmlnID0gcHJvcHMuc3RhZ2VDb25maWcgfHwgU3RhZ2VDb25maWdzLmdldENvbmZpZyhzdGFnZSk7XG5cbiAgICAvLyBQYXJhbWV0ZXJzIGZvciBydW50aW1lIGNvbmZpZ3VyYXRpb24gKHJlZHVjZWQgZnJvbSBvcmlnaW5hbClcbiAgICBjb25zdCB0cml0b25JbWFnZVRhZ1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1RyaXRvbkltYWdlVGFnJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAndjAuMC4wJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRUNSIGltYWdlIHRhZyBmb3IgVHJpdG9uIGNvbnRhaW5lcidcbiAgICB9KTtcblxuICAgIGNvbnN0IGN3QWdlbnRJbWFnZVRhZ1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0N3QWdlbnRJbWFnZVRhZycsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2xhdGVzdCcsXG4gICAgICBkZXNjcmlwdGlvbjogJ0VDUiBpbWFnZSB0YWcgZm9yIENsb3VkV2F0Y2ggQWdlbnQgY29udGFpbmVyJ1xuICAgIH0pO1xuXG4gICAgY29uc3QgZGNnbUltYWdlVGFnUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnRGNnbUltYWdlVGFnJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnbGF0ZXN0JyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRUNSIGltYWdlIHRhZyBmb3IgRENHTSBleHBvcnRlciBjb250YWluZXInXG4gICAgfSk7XG5cbiAgICAvLyBQYXJhbWV0ZXJzIHJlcGxhY2VkIHdpdGggc3RhZ2UgY29uZmlndXJhdGlvbiBmb3IgYmV0dGVyIHR5cGUgc2FmZXR5IGFuZCB0b2tlbi1icmFuY2hpbmcgZml4XG5cbiAgICAvLyBFQ1IgUmVwb3NpdG9yaWVzXG4gICAgY29uc3QgcmVwb3NpdG9yaWVzID0gdGhpcy5jcmVhdGVFY3JSZXBvc2l0b3JpZXMoKTtcblxuICAgIC8vIFN0b3JhZ2UgKEtNUyBrZXksIGJ1Y2tldHMgd2l0aCBzZWN1cml0eSBwb2xpY2llcylcbiAgICBjb25zdCBzdG9yYWdlID0gbmV3IFN0b3JhZ2VDb25zdHJ1Y3QodGhpcywgJ1N0b3JhZ2UnLCB7XG4gICAgICBrbXNLZXlBbGlhczogYGFsaWFzL2FtaXJhLWxldHRlci1zY29yaW5nLSR7c3RhZ2V9YCxcbiAgICAgIGVuYWJsZVZlcnNpb25pbmc6IHN0YWdlQ29uZmlnLnNlY3VyaXR5LmVuYWJsZVZlcnNpb25pbmdcbiAgICB9KTtcblxuICAgIC8vIFF1ZXVlaW5nIChTUVMgd2l0aCBETFEpXG4gICAgY29uc3QgcXVldWVpbmcgPSBuZXcgUXVldWVpbmdDb25zdHJ1Y3QodGhpcywgJ1F1ZXVlaW5nJywge1xuICAgICAga21zS2V5OiBzdG9yYWdlLmttc0tleVxuICAgIH0pO1xuXG4gICAgLy8gTmV0d29ya2luZyAoVlBDLCBlbmRwb2ludHMsIHNlY3VyaXR5IGdyb3VwcylcbiAgICBjb25zdCBuZXR3b3JraW5nID0gbmV3IE5ldHdvcmtpbmdDb25zdHJ1Y3QodGhpcywgJ05ldHdvcmtpbmcnLCB7XG4gICAgICBuYXRHYXRld2F5Q291bnQ6IHN0YWdlQ29uZmlnLm5ldHdvcmtpbmcubmF0R2F0ZXdheUNvdW50LFxuICAgICAgZW5hYmxlSW50ZXJmYWNlRW5kcG9pbnRzOiBzdGFnZUNvbmZpZy5mZWF0dXJlcy5lbmFibGVJbnRlcmZhY2VFbmRwb2ludHMsXG4gICAgICBhbGJDbGllbnRTZWN1cml0eUdyb3VwSWQ6IHN0YWdlQ29uZmlnLm5ldHdvcmtpbmcuYWxiQ2xpZW50U2VjdXJpdHlHcm91cElkLFxuICAgICAgc3RhZ2VOYW1lOiBzdGFnZUNvbmZpZy5zdGFnZU5hbWUsXG4gICAgICBlbmNyeXB0aW9uS2V5OiBzdG9yYWdlLmttc0tleSxcbiAgICAgIHVzZU5hdEluc3RhbmNlczogc3RhZ2VDb25maWcubmV0d29ya2luZy51c2VOYXRJbnN0YW5jZXNcbiAgICB9KTtcblxuICAgIC8vIEVDUyBDbHVzdGVyIHdpdGggR1BVIGluc3RhbmNlc1xuICAgIGNvbnN0IGNsdXN0ZXIgPSBuZXcgRWNzQ2x1c3RlckNvbnN0cnVjdCh0aGlzLCAnRWNzQ2x1c3RlcicsIHtcbiAgICAgIHZwYzogbmV0d29ya2luZy52cGMsXG4gICAgICBpbnN0YW5jZVR5cGVzOiBzdGFnZUNvbmZpZy5jb21wdXRlLmluc3RhbmNlVHlwZXMsXG4gICAgICBzZWN1cml0eUdyb3VwOiBuZXR3b3JraW5nLmVjc1NlY3VyaXR5R3JvdXAsXG4gICAgICBjbHVzdGVyTmFtZTogYGFtaXJhLWxldHRlci1zY29yaW5nLSR7c3RhZ2V9YCxcbiAgICAgIHNwb3RNYXhQcmljZTogc3RhZ2VDb25maWcuY29tcHV0ZS5zcG90TWF4UHJpY2VcbiAgICB9KTtcblxuICAgIC8vIFRyaXRvbiBTZXJ2aWNlIChpZiBlbmFibGVkIGFuZCBjZXJ0aWZpY2F0ZSBBUk4gaXMgY29uZmlndXJlZClcbiAgICBsZXQgdHJpdG9uU2VydmljZTogVHJpdG9uU2VydmljZUNvbnN0cnVjdCB8IHVuZGVmaW5lZDtcbiAgICBpZiAoc3RhZ2VDb25maWcuZmVhdHVyZXMuZW5hYmxlVHJpdG9uICYmIHN0YWdlQ29uZmlnLnNlY3VyaXR5LnRyaXRvbkNlcnRBcm4pIHtcbiAgICAgIGNvbnN0IGxvZ0dyb3VwID0gbmV3IGxvZ3MuTG9nR3JvdXAodGhpcywgJ1RyaXRvbkxvZ0dyb3VwJywge1xuICAgICAgICBsb2dHcm91cE5hbWU6IGAvZWNzL2FtaXJhLWxldHRlci1zY29yaW5nLSR7c3RhZ2V9YCxcbiAgICAgICAgcmV0ZW50aW9uOiBsb2dzLlJldGVudGlvbkRheXMuT05FX01PTlRILFxuICAgICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5ERVNUUk9ZLFxuICAgICAgICBlbmNyeXB0aW9uS2V5OiBzdG9yYWdlLmttc0tleVxuICAgICAgfSk7XG5cbiAgICAgIHRyaXRvblNlcnZpY2UgPSBuZXcgVHJpdG9uU2VydmljZUNvbnN0cnVjdCh0aGlzLCAnVHJpdG9uU2VydmljZScsIHtcbiAgICAgICAgY2x1c3RlcjogY2x1c3Rlci5jbHVzdGVyLFxuICAgICAgICBjYXBhY2l0eVByb3ZpZGVyczogY2x1c3Rlci5jYXBhY2l0eVByb3ZpZGVycyxcbiAgICAgICAgbG9nR3JvdXAsXG4gICAgICAgIHZwYzogbmV0d29ya2luZy52cGMsXG4gICAgICAgIGFsYlNlY3VyaXR5R3JvdXA6IG5ldHdvcmtpbmcuYWxiU2VjdXJpdHlHcm91cCxcbiAgICAgICAgZWNzU2VjdXJpdHlHcm91cDogbmV0d29ya2luZy5lY3NTZWN1cml0eUdyb3VwLFxuICAgICAgICBhY2Nlc3NMb2dzQnVja2V0OiBzdG9yYWdlLmFjY2Vzc0xvZ3NCdWNrZXQsXG4gICAgICAgIGNlcnRBcm46IHN0YWdlQ29uZmlnLnNlY3VyaXR5LnRyaXRvbkNlcnRBcm4sXG4gICAgICAgIHRsczoge1xuICAgICAgICAgIGVuYWJsZUh0dHAyOiBzdGFnZUNvbmZpZy50bHM/LmVuYWJsZUh0dHAyID8/IHRydWUsXG4gICAgICAgICAgY2lwaGVyczogc3RhZ2VDb25maWcudGxzPy5jaXBoZXJzLFxuICAgICAgICAgIHNlY3JldEFybjogc3RhZ2VDb25maWcudGxzPy5zZWNyZXRBcm4sXG4gICAgICAgICAgcmVxdWlyZVNlY3JldDogc3RhZ2VDb25maWcudGxzPy5yZXF1aXJlU2VjcmV0ID8/IGZhbHNlXG4gICAgICAgIH0sXG4gICAgICAgIHJlcG9zaXRvcmllczoge1xuICAgICAgICAgIHRyaXRvbjogcmVwb3NpdG9yaWVzLnRyaXRvbixcbiAgICAgICAgICBjd0FnZW50OiByZXBvc2l0b3JpZXMuY3dBZ2VudCxcbiAgICAgICAgICBkY2dtRXhwb3J0ZXI6IHJlcG9zaXRvcmllcy5kY2dtRXhwb3J0ZXJcbiAgICAgICAgfSxcbiAgICAgICAgaW1hZ2VUYWdzOiB7XG4gICAgICAgICAgdHJpdG9uOiB0cml0b25JbWFnZVRhZ1BhcmFtLnZhbHVlQXNTdHJpbmcsXG4gICAgICAgICAgY3dBZ2VudDogY3dBZ2VudEltYWdlVGFnUGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgICBkY2dtRXhwb3J0ZXI6IGRjZ21JbWFnZVRhZ1BhcmFtLnZhbHVlQXNTdHJpbmdcbiAgICAgICAgfVxuICAgICAgfSk7XG4gICAgfVxuXG4gICAgLy8gTWFudWFsIGVucXVldWUgTGFtYmRhIGZvciB0ZXN0aW5nXG4gICAgY29uc3QgbWFudWFsRW5xdWV1ZUZuID0gbmV3IGxhbWJkYS5GdW5jdGlvbih0aGlzLCAnTWFudWFsRW5xdWV1ZUZ1bmN0aW9uJywge1xuICAgICAgcnVudGltZTogbGFtYmRhLlJ1bnRpbWUuUFlUSE9OXzNfMTIsXG4gICAgICBoYW5kbGVyOiAnaW5kZXguaGFuZGxlcicsXG4gICAgICBjb2RlOiBsYW1iZGEuQ29kZS5mcm9tQXNzZXQoJy4uL2xhbWJkYS9tYW51YWxfZW5xdWV1ZScpLFxuICAgICAgdGltZW91dDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMSksXG4gICAgICB0cmFjaW5nOiBsYW1iZGEuVHJhY2luZy5BQ1RJVkUsXG4gICAgICBlbnZpcm9ubWVudDogeyBKT0JTX1FVRVVFX1VSTDogcXVldWVpbmcucXVldWUucXVldWVVcmwgfVxuICAgIH0pO1xuICAgIHF1ZXVlaW5nLnF1ZXVlLmdyYW50U2VuZE1lc3NhZ2VzKG1hbnVhbEVucXVldWVGbik7XG5cbiAgICBjb25zdCBtYW51YWxVcmwgPSBtYW51YWxFbnF1ZXVlRm4uYWRkRnVuY3Rpb25Vcmwoe1xuICAgICAgYXV0aFR5cGU6IGxhbWJkYS5GdW5jdGlvblVybEF1dGhUeXBlLkFXU19JQU0sXG4gICAgICBjb3JzOiB7XG4gICAgICAgIGFsbG93ZWRPcmlnaW5zOiBbJyonXSxcbiAgICAgICAgYWxsb3dlZE1ldGhvZHM6IFtsYW1iZGEuSHR0cE1ldGhvZC5QT1NULCBsYW1iZGEuSHR0cE1ldGhvZC5PUFRJT05TXVxuICAgICAgfVxuICAgIH0pO1xuXG4gICAgLy8gU3BvdCBpbnRlcnJ1cHRpb24gZHJhaW4gTGFtYmRhXG4gICAgY29uc3QgZHJhaW5GbiA9IG5ldyBsYW1iZGEuRnVuY3Rpb24odGhpcywgJ0Vjc0RyYWluT25TcG90Rm4nLCB7XG4gICAgICBydW50aW1lOiBsYW1iZGEuUnVudGltZS5QWVRIT05fM18xMixcbiAgICAgIGhhbmRsZXI6ICdpbmRleC5oYW5kbGVyJyxcbiAgICAgIGNvZGU6IGxhbWJkYS5Db2RlLmZyb21Bc3NldCgnLi4vbGFtYmRhL2Vjc19kcmFpbl9vbl9zcG90JyksXG4gICAgICB0aW1lb3V0OiBjZGsuRHVyYXRpb24uc2Vjb25kcyg2MCksXG4gICAgICBlbnZpcm9ubWVudDogeyBDTFVTVEVSX0FSTjogY2x1c3Rlci5jbHVzdGVyLmNsdXN0ZXJBcm4gfVxuICAgIH0pO1xuXG4gICAgZHJhaW5Gbi5hZGRUb1JvbGVQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgYWN0aW9uczogWydlY3M6TGlzdENvbnRhaW5lckluc3RhbmNlcycsICdlY3M6RGVzY3JpYmVDb250YWluZXJJbnN0YW5jZXMnXSxcbiAgICAgIHJlc291cmNlczogW2NsdXN0ZXIuY2x1c3Rlci5jbHVzdGVyQXJuXVxuICAgIH0pKTtcblxuICAgIGRyYWluRm4uYWRkVG9Sb2xlUG9saWN5KG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgIGFjdGlvbnM6IFsnZWNzOlVwZGF0ZUNvbnRhaW5lckluc3RhbmNlc1N0YXRlJ10sXG4gICAgICByZXNvdXJjZXM6IFsnKiddLFxuICAgICAgY29uZGl0aW9uczogeyBTdHJpbmdFcXVhbHM6IHsgJ2VjczpjbHVzdGVyJzogY2x1c3Rlci5jbHVzdGVyLmNsdXN0ZXJBcm4gfSB9XG4gICAgfSkpO1xuXG4gICAgbmV3IGV2ZW50cy5SdWxlKHRoaXMsICdTcG90SW50ZXJydXB0aW9uRHJhaW5SdWxlJywge1xuICAgICAgZGVzY3JpcHRpb246ICdEcmFpbiBFQ1MgY29udGFpbmVyIGluc3RhbmNlcyBvbiBTcG90IGludGVycnVwdGlvbiB3YXJuaW5ncycsXG4gICAgICBldmVudFBhdHRlcm46IHtcbiAgICAgICAgc291cmNlOiBbJ2F3cy5lYzInXSxcbiAgICAgICAgZGV0YWlsVHlwZTogWydFQzIgU3BvdCBJbnN0YW5jZSBJbnRlcnJ1cHRpb24gV2FybmluZycsICdFQzIgSW5zdGFuY2UgUmViYWxhbmNlIFJlY29tbWVuZGF0aW9uJ11cbiAgICAgIH0sXG4gICAgICB0YXJnZXRzOiBbbmV3IHRhcmdldHMuTGFtYmRhRnVuY3Rpb24oZHJhaW5GbildXG4gICAgfSk7XG5cbiAgICAvLyBPYnNlcnZhYmlsaXR5IChhbGFybXMsIGRhc2hib2FyZHMpXG4gICAgY29uc3Qgb2JzZXJ2YWJpbGl0eSA9IG5ldyBPYnNlcnZhYmlsaXR5Q29uc3RydWN0KHRoaXMsICdPYnNlcnZhYmlsaXR5Jywge1xuICAgICAgYWxhcm1FbWFpbDogc3RhZ2VDb25maWcub2JzZXJ2YWJpbGl0eS5hbGFybUVtYWlsLFxuICAgICAgZGFzaGJvYXJkTmFtZTogYEFtaXJhR3B1VHJpdG9uLSR7c3RhZ2V9YCxcbiAgICAgIG1ldHJpY1NvdXJjZXM6IHtcbiAgICAgICAgbWFudWFsVHJpZ2dlckxhbWJkYTogbWFudWFsRW5xdWV1ZUZuLFxuICAgICAgICBlY3NTZXJ2aWNlOiB0cml0b25TZXJ2aWNlPy5zZXJ2aWNlLFxuICAgICAgICBlY3NDbHVzdGVyOiBjbHVzdGVyLmNsdXN0ZXIsXG4gICAgICAgIHF1ZXVlOiBxdWV1ZWluZy5xdWV1ZSxcbiAgICAgICAgZGxxOiBxdWV1ZWluZy5kbHEsXG4gICAgICAgIGFsYjogdHJpdG9uU2VydmljZT8uYWxiLFxuICAgICAgICB0YXJnZXRHcm91cDogdHJpdG9uU2VydmljZT8udGFyZ2V0R3JvdXAsXG4gICAgICAgIGFzZ3M6IGNsdXN0ZXIuYXNnc1xuICAgICAgfVxuICAgIH0pO1xuXG4gICAgLy8gQ3Jvc3Mtc3RhY2sgbGlua2luZyAoU1NNIHBhcmFtZXRlcnMpXG4gICAgbmV3IENyb3NzU3RhY2tMaW5raW5nQ29uc3RydWN0KHRoaXMsICdDcm9zc1N0YWNrTGlua2luZycsIHtcbiAgICAgIHZwYzogbmV0d29ya2luZy52cGMsXG4gICAgICBhbGJTZWN1cml0eUdyb3VwOiBuZXR3b3JraW5nLmFsYlNlY3VyaXR5R3JvdXAsXG4gICAgICB0cml0b25BbGI6IHRyaXRvblNlcnZpY2U/LmFsYlxuICAgIH0pO1xuXG4gICAgLy8gQXBwbHkgc2VjdXJpdHkgc3RhbmRhcmRzIGFuZCBjb21wbGlhbmNlIGFzcGVjdHNcbiAgICBjZGsuQXNwZWN0cy5vZih0aGlzKS5hZGQobmV3IFNlY3VyaXR5U3RhbmRhcmRzQXNwZWN0KCkpO1xuICAgIGNkay5Bc3BlY3RzLm9mKHRoaXMpLmFkZChuZXcgQ2RrTmFnSW50ZWdyYXRpb25Bc3BlY3QoKSk7XG5cbiAgICAvLyBPdXRwdXRzXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1RyaXRvblJlcG9zaXRvcnlVcmknLCB7XG4gICAgICB2YWx1ZTogcmVwb3NpdG9yaWVzLnRyaXRvbi5yZXBvc2l0b3J5VXJpLFxuICAgICAgZGVzY3JpcHRpb246ICdUcml0b24gRUNSIFJlcG9zaXRvcnkgVVJJJ1xuICAgIH0pO1xuXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ0dwdUNsdXN0ZXJOYW1lJywge1xuICAgICAgdmFsdWU6IGNsdXN0ZXIuY2x1c3Rlci5jbHVzdGVyTmFtZSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnR1BVIEVDUyBDbHVzdGVyIE5hbWUnXG4gICAgfSk7XG5cbiAgICBpZiAodHJpdG9uU2VydmljZSkge1xuICAgICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1RyaXRvbkNsdXN0ZXJVcmwnLCB7XG4gICAgICAgIHZhbHVlOiBgaHR0cHM6Ly8ke3RyaXRvblNlcnZpY2UuYWxiLmxvYWRCYWxhbmNlckRuc05hbWV9YCxcbiAgICAgICAgZGVzY3JpcHRpb246ICdIVFRQUyBVUkwgZm9yIFRyaXRvbiBHUFUgaW5mZXJlbmNlIGNsdXN0ZXInXG4gICAgICB9KTtcblxuICAgICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1RyaXRvblNlcnZpY2VOYW1lJywge1xuICAgICAgICB2YWx1ZTogdHJpdG9uU2VydmljZS5zZXJ2aWNlLnNlcnZpY2VOYW1lLFxuICAgICAgICBkZXNjcmlwdGlvbjogJ1RyaXRvbiBFQ1MgU2VydmljZSBOYW1lJ1xuICAgICAgfSk7XG4gICAgfVxuXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ0dwdURhc2hib2FyZFVybCcsIHtcbiAgICAgIHZhbHVlOiBgaHR0cHM6Ly8ke3RoaXMucmVnaW9ufS5jb25zb2xlLmF3cy5hbWF6b24uY29tL2Nsb3Vkd2F0Y2gvaG9tZT9yZWdpb249JHt0aGlzLnJlZ2lvbn0jZGFzaGJvYXJkczpuYW1lPSR7b2JzZXJ2YWJpbGl0eS5kYXNoYm9hcmQuZGFzaGJvYXJkTmFtZX1gLFxuICAgICAgZGVzY3JpcHRpb246ICdDbG91ZFdhdGNoIEdQVS9Ucml0b24gRGFzaGJvYXJkIFVSTCdcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdNYW51YWxFbnF1ZXVlVXJsJywge1xuICAgICAgdmFsdWU6IG1hbnVhbFVybC51cmwsXG4gICAgICBkZXNjcmlwdGlvbjogJ01hbnVhbCBlbnF1ZXVlIGZ1bmN0aW9uIFVSTCdcbiAgICB9KTtcbiAgfVxuXG4gIHByaXZhdGUgY3JlYXRlRWNyUmVwb3NpdG9yaWVzKCk6IHtcbiAgICBhcHA6IGVjci5SZXBvc2l0b3J5O1xuICAgIHRyaXRvbjogZWNyLlJlcG9zaXRvcnk7XG4gICAgY3dBZ2VudDogZWNyLlJlcG9zaXRvcnk7XG4gICAgZGNnbUV4cG9ydGVyOiBlY3IuUmVwb3NpdG9yeTtcbiAgfSB7XG4gICAgY29uc3QgYXBwID0gbmV3IGVjci5SZXBvc2l0b3J5KHRoaXMsICdBbWlyYUxldHRlclNjb3JpbmdSZXBvJywge1xuICAgICAgcmVwb3NpdG9yeU5hbWU6ICdhbWlyYS1sZXR0ZXItc2NvcmluZycsXG4gICAgICBpbWFnZVNjYW5PblB1c2g6IHRydWUsXG4gICAgICBpbWFnZVRhZ011dGFiaWxpdHk6IGVjci5UYWdNdXRhYmlsaXR5LklNTVVUQUJMRSxcbiAgICAgIGxpZmVjeWNsZVJ1bGVzOiBbe1xuICAgICAgICBtYXhJbWFnZUNvdW50OiAxMCxcbiAgICAgICAgZGVzY3JpcHRpb246ICdLZWVwIG9ubHkgMTAgbW9zdCByZWNlbnQgaW1hZ2VzJ1xuICAgICAgfV1cbiAgICB9KTtcblxuICAgIGNvbnN0IHRyaXRvbiA9IG5ldyBlY3IuUmVwb3NpdG9yeSh0aGlzLCAnVHJpdG9uU2VydmVyUmVwbycsIHtcbiAgICAgIHJlcG9zaXRvcnlOYW1lOiAndHJpdG9uLXNlcnZlcicsXG4gICAgICBpbWFnZVNjYW5PblB1c2g6IHRydWUsXG4gICAgICBpbWFnZVRhZ011dGFiaWxpdHk6IGVjci5UYWdNdXRhYmlsaXR5LklNTVVUQUJMRSxcbiAgICAgIGxpZmVjeWNsZVJ1bGVzOiBbe1xuICAgICAgICBtYXhJbWFnZUNvdW50OiA1LFxuICAgICAgICBkZXNjcmlwdGlvbjogJ0tlZXAgb25seSA1IG1vc3QgcmVjZW50IFRyaXRvbiBpbWFnZXMnXG4gICAgICB9XVxuICAgIH0pO1xuXG4gICAgY29uc3QgY3dBZ2VudCA9IG5ldyBlY3IuUmVwb3NpdG9yeSh0aGlzLCAnQ2xvdWRXYXRjaEFnZW50UmVwbycsIHtcbiAgICAgIHJlcG9zaXRvcnlOYW1lOiAnY2xvdWR3YXRjaC1hZ2VudCcsXG4gICAgICBpbWFnZVNjYW5PblB1c2g6IHRydWUsXG4gICAgICBpbWFnZVRhZ011dGFiaWxpdHk6IGVjci5UYWdNdXRhYmlsaXR5LklNTVVUQUJMRSxcbiAgICAgIGxpZmVjeWNsZVJ1bGVzOiBbe1xuICAgICAgICBtYXhJbWFnZUNvdW50OiA1LFxuICAgICAgICBkZXNjcmlwdGlvbjogJ0tlZXAgb25seSA1IG1vc3QgcmVjZW50IENsb3VkV2F0Y2ggQWdlbnQgaW1hZ2VzJ1xuICAgICAgfV1cbiAgICB9KTtcblxuICAgIGNvbnN0IGRjZ21FeHBvcnRlciA9IG5ldyBlY3IuUmVwb3NpdG9yeSh0aGlzLCAnRGNnbUV4cG9ydGVyUmVwbycsIHtcbiAgICAgIHJlcG9zaXRvcnlOYW1lOiAnZGNnbS1leHBvcnRlcicsXG4gICAgICBpbWFnZVNjYW5PblB1c2g6IHRydWUsXG4gICAgICBpbWFnZVRhZ011dGFiaWxpdHk6IGVjci5UYWdNdXRhYmlsaXR5LklNTVVUQUJMRSxcbiAgICAgIGxpZmVjeWNsZVJ1bGVzOiBbe1xuICAgICAgICBtYXhJbWFnZUNvdW50OiA1LFxuICAgICAgICBkZXNjcmlwdGlvbjogJ0tlZXAgb25seSA1IG1vc3QgcmVjZW50IERDR00gZXhwb3J0ZXIgaW1hZ2VzJ1xuICAgICAgfV1cbiAgICB9KTtcblxuICAgIHJldHVybiB7IGFwcCwgdHJpdG9uLCBjd0FnZW50LCBkY2dtRXhwb3J0ZXIgfTtcbiAgfVxufVxuIl19