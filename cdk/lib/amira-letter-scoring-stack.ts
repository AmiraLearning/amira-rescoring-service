import * as cdk from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

// Import our new constructs
import { StorageConstruct } from './constructs/storage';
import { QueueingConstruct } from './constructs/queueing';
import { NetworkingConstruct } from './constructs/networking';
import { EcsClusterConstruct } from './constructs/ecs-cluster';
import { TritonServiceConstruct } from './constructs/triton-service';
import { ObservabilityConstruct } from './constructs/observability';
import { CrossStackLinkingConstruct } from './constructs/cross-stack-linking';
import { SecurityStandardsAspect } from './aspects/security-standards';
import { CdkNagIntegrationAspect } from './aspects/cdk-nag-integration';
import { StageConfigs, StageConfig } from './config/stages';

export interface AmiraLetterScoringStackProps extends cdk.StackProps {
  readonly stage?: string;
  readonly stageConfig?: StageConfig;
}

export class AmiraLetterScoringStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: AmiraLetterScoringStackProps = {}) {
    super(scope, id, props);

    // Get stage configuration
    const stage = props.stage || process.env.STAGE || 'dev';
    const stageConfig = props.stageConfig || StageConfigs.getConfig(stage);

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
    const storage = new StorageConstruct(this, 'Storage', {
      kmsKeyAlias: `alias/amira-letter-scoring-${stage}`,
      enableVersioning: stageConfig.security.enableVersioning
    });

    // Queueing (SQS with DLQ)
    const queueing = new QueueingConstruct(this, 'Queueing', {
      kmsKey: storage.kmsKey
    });

    // Networking (VPC, endpoints, security groups)
    const networking = new NetworkingConstruct(this, 'Networking', {
      natGatewayCount: stageConfig.networking.natGatewayCount,
      enableInterfaceEndpoints: stageConfig.features.enableInterfaceEndpoints,
      albClientSecurityGroupId: stageConfig.networking.albClientSecurityGroupId,
      stageName: stageConfig.stageName,
      encryptionKey: storage.kmsKey,
      useNatInstances: stageConfig.networking.useNatInstances
    });

    // ECS Cluster with GPU instances
    const cluster = new EcsClusterConstruct(this, 'EcsCluster', {
      vpc: networking.vpc,
      instanceTypes: stageConfig.compute.instanceTypes,
      securityGroup: networking.ecsSecurityGroup,
      clusterName: `amira-letter-scoring-${stage}`,
      spotMaxPrice: stageConfig.compute.spotMaxPrice
    });

    // Triton Service (if enabled and certificate ARN is configured)
    let tritonService: TritonServiceConstruct | undefined;
    if (stageConfig.features.enableTriton && stageConfig.security.tritonCertArn) {
      const logGroup = new logs.LogGroup(this, 'TritonLogGroup', {
        logGroupName: `/ecs/amira-letter-scoring-${stage}`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        encryptionKey: storage.kmsKey
      });

      tritonService = new TritonServiceConstruct(this, 'TritonService', {
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
    const observability = new ObservabilityConstruct(this, 'Observability', {
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
    new CrossStackLinkingConstruct(this, 'CrossStackLinking', {
      vpc: networking.vpc,
      albSecurityGroup: networking.albSecurityGroup,
      tritonAlb: tritonService?.alb
    });

    // Apply security standards and compliance aspects
    cdk.Aspects.of(this).add(new SecurityStandardsAspect());
    cdk.Aspects.of(this).add(new CdkNagIntegrationAspect());

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

  private createEcrRepositories(): {
    app: ecr.Repository;
    triton: ecr.Repository;
    cwAgent: ecr.Repository;
    dcgmExporter: ecr.Repository;
  } {
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
