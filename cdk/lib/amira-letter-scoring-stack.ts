import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as appscaling from 'aws-cdk-lib/aws-applicationautoscaling';
import * as cwactions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as cw_dash from 'aws-cdk-lib/aws-cloudwatch';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import { Construct } from 'constructs';
import cwAgentConfig = require('./cw-agent-config.json');

export class AmiraLetterScoringStack extends cdk.Stack {
  private createAsgAndCapacityProvider(scope: Construct, id: string, vpc: ec2.IVpc, instanceType: ec2.InstanceType, securityGroup: ec2.ISecurityGroup, role: iam.IRole): { asg: autoscaling.AutoScalingGroup; capacityProvider: ecs.AsgCapacityProvider } {
    const lt = new ec2.LaunchTemplate(scope, `${id}LaunchTemplate`, {
      instanceType,
      machineImage: ecs.EcsOptimizedImage.amazonLinux2(ecs.AmiHardwareType.GPU),
      userData: ec2.UserData.forLinux(),
      securityGroup,
      role,
      requireImdsv2: true,
      spotOptions: { requestType: ec2.SpotRequestType.ONE_TIME, interruptionBehavior: ec2.SpotInstanceInterruption.STOP }
    });

    const asg = new autoscaling.AutoScalingGroup(scope, `${id}Asg`, {
      vpc,
      launchTemplate: lt,
      minCapacity: 0,
      maxCapacity: 10,
      desiredCapacity: 0,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      capacityRebalance: true
    });

    const capacityProvider = new ecs.AsgCapacityProvider(scope, `${id}CapacityProvider`, {
      autoScalingGroup: asg,
      enableManagedScaling: true,
      enableManagedTerminationProtection: true,
      targetCapacityPercent: 100,
      machineImageType: ecs.MachineImageType.AMAZON_LINUX_2
    });
    return { asg, capacityProvider };
  }
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ECR Repositories for containers
    const repository = new ecr.Repository(this, 'AmiraLetterScoringRepo', {
      repositoryName: 'amira-letter-scoring',
      imageScanOnPush: true,
      lifecycleRules: [{
        maxImageCount: 10,
        description: 'Keep only 10 most recent images'
      }]
    });

    // ECR repositories for Triton GPU cluster (conditional)
    const tritonRepository = new ecr.Repository(this, 'TritonServerRepo', {
      repositoryName: 'triton-server',
      imageScanOnPush: true,
      lifecycleRules: [{ maxImageCount: 5 }]
    });
    const cwAgentRepository = new ecr.Repository(this, 'CloudWatchAgentRepo', {
      repositoryName: 'cloudwatch-agent',
      imageScanOnPush: true,
      lifecycleRules: [{ maxImageCount: 5 }]
    });
    const dcgmExporterRepository = new ecr.Repository(this, 'DcgmExporterRepo', {
      repositoryName: 'dcgm-exporter',
      imageScanOnPush: true,
      lifecycleRules: [{ maxImageCount: 5 }]
    });

    // Parameters for runtime configuration
    const appImageTagParam = new cdk.CfnParameter(this, 'AppImageTag', {
      type: 'String',
      default: 'v0.0.0',
      description: 'ECR image tag for application container'
    });
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
    const modelPathParam = new cdk.CfnParameter(this, 'ModelPath', {
      type: 'String',
      default: 'facebook/wav2vec2-base-960h',
      description: 'HF model path for Wav2Vec2'
    });
    const includeConfidenceParam = new cdk.CfnParameter(this, 'IncludeConfidence', {
      type: 'String',
      default: 'true',
      allowedValues: ['true', 'false'],
      description: 'Whether to compute confidence in worker'
    });
    const audioDirParam = new cdk.CfnParameter(this, 'AudioDir', {
      type: 'String',
      default: '/tmp/audio',
      description: 'Local audio working directory inside container'
    });
    const resultsPrefixParam = new cdk.CfnParameter(this, 'ResultsPrefix', {
      type: 'String',
      default: 'results/',
      description: 'S3 key prefix for results writes'
    });

    const natGatewayCountParam = new cdk.CfnParameter(this, 'NatGatewayCount', {
      type: 'Number',
      default: 1,
      description: 'Number of NAT Gateways to create (set 0 to save cost with VPC endpoints)'
    });

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
    const athenaTableParam = new cdk.CfnParameter(this, 'AthenaTable', {
      type: 'String',
      default: '',
      description: 'Optional table name for dynamic query building'
    });
    const athenaWhereParam = new cdk.CfnParameter(this, 'AthenaWhere', {
      type: 'String',
      default: '',
      description: 'Optional WHERE clause (without WHERE keyword)'
    });
    const athenaLimitParam = new cdk.CfnParameter(this, 'AthenaLimit', {
      type: 'String',
      default: '',
      description: 'Optional LIMIT value for the query'
    });
    const athenaColumnsParam = new cdk.CfnParameter(this, 'AthenaColumns', {
      type: 'String',
      default: 'activity_id',
      description: 'Optional columns to select for dynamic query'
    });

    // Triton inference feature flag
    const useTritonParam = new cdk.CfnParameter(this, 'UseTriton', {
      type: 'String',
      default: 'false',
      allowedValues: ['true', 'false'],
      description: 'Whether to enable Triton inference server with GPU resources'
    });

    const tritonCertArnParam = new cdk.CfnParameter(this, 'TritonCertificateArn', {
      type: 'String',
      default: '',
      description: 'ACM certificate ARN for HTTPS on the Triton ALB (required for TLS)'
    });

    // Optional Audio bucket for read-only access
    const audioBucketNameParam = new cdk.CfnParameter(this, 'AudioBucketName', {
      type: 'String',
      default: '',
      description: 'Optional S3 bucket name for input audio (read-only). Leave blank to skip.'
    });

    // Triton URL parameter for remote inference (when UseTriton=false but want to call external Triton)
    const tritonServerUrlParam = new cdk.CfnParameter(this, 'TritonServerUrl', {
      type: 'String',
      default: '',
      description: 'Optional external Triton server URL for remote inference. Leave blank for local sidecar.'
    });
    const audioBucketPrefixParam = new cdk.CfnParameter(this, 'AudioPrefix', {
      type: 'String',
      default: '',
      description: 'Optional S3 key prefix within the audio bucket.'
    });

    // VPC for the ECS cluster
    const vpc = new ec2.Vpc(this, 'AmiraLetterScoringVpc', {
      maxAzs: 2,
      natGateways: natGatewayCountParam.valueAsNumber,
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          cidrMask: 24,
          name: 'Private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        }
      ]
    });

    // VPC Endpoints to reduce NAT egress
    vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
      subnets: [{ subnets: vpc.privateSubnets }]
    });
    vpc.addInterfaceEndpoint('EcrApiEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR,
      subnets: { subnets: vpc.privateSubnets }
    });
    vpc.addInterfaceEndpoint('EcrDockerEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
      subnets: { subnets: vpc.privateSubnets }
    });
    vpc.addInterfaceEndpoint('CloudWatchLogsEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
      subnets: { subnets: vpc.privateSubnets }
    });
    vpc.addInterfaceEndpoint('SqsEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.SQS,
      subnets: { subnets: vpc.privateSubnets }
    });
    vpc.addInterfaceEndpoint('SsmEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.SSM,
      subnets: { subnets: vpc.privateSubnets }
    });
    vpc.addInterfaceEndpoint('SsmMessagesEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES,
      subnets: { subnets: vpc.privateSubnets }
    });
    vpc.addInterfaceEndpoint('Ec2MessagesEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES,
      subnets: { subnets: vpc.privateSubnets }
    });
    vpc.addInterfaceEndpoint('StsEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.STS,
      subnets: { subnets: vpc.privateSubnets }
    });

    // Security group for ECS tasks
    const securityGroup = new ec2.SecurityGroup(this, 'AmiraLetterScoringSecurityGroup', {
      vpc,
      description: 'Security group for Amira Letter Scoring ECS tasks',
      allowAllOutbound: true
    });

    // ECS Cluster with GPU instances
    const cluster = new ecs.Cluster(this, 'AmiraLetterScoringCluster', {
      vpc,
      clusterName: 'amira-letter-scoring-cluster',
      containerInsights: true
    });

    // EC2 instance role for ECS cluster instances
    const instanceRole = new iam.Role(this, 'GpuInstanceRole', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com')
    });
    instanceRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonEC2ContainerServiceforEC2Role'));
    instanceRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'));

    // Launch template for GPU instances (A10G)
    const launchTemplate = new ec2.LaunchTemplate(this, 'GpuLaunchTemplate', {
      instanceType: ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE4), // g5.4xlarge has A10G GPU
      machineImage: ecs.EcsOptimizedImage.amazonLinux2(ecs.AmiHardwareType.GPU),
      userData: ec2.UserData.forLinux(),
      securityGroup,
      role: instanceRole,
      requireImdsv2: true,
      spotOptions: {
        requestType: ec2.SpotRequestType.ONE_TIME,
        interruptionBehavior: ec2.SpotInstanceInterruption.STOP
      }
    });

    // Auto Scaling Group for GPU instances
    const autoScalingGroup = new autoscaling.AutoScalingGroup(this, 'GpuAutoScalingGroup', {
      vpc,
      launchTemplate,
      minCapacity: 0,
      maxCapacity: 10,
      desiredCapacity: 0,
      vpcSubnets: {
        subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS
      },
      capacityRebalance: true
    });

    // Add capacity provider to cluster
    const capacityProvider = new ecs.AsgCapacityProvider(this, 'GpuCapacityProvider', {
      autoScalingGroup,
      enableManagedScaling: true,
      enableManagedTerminationProtection: true,
      targetCapacityPercent: 100,
      machineImageType: ecs.MachineImageType.AMAZON_LINUX_2
    });
    // Additional ASGs for diversified Spot capacity
    const { asg: asgG5xlarge, capacityProvider: cpG5xlarge } = this.createAsgAndCapacityProvider(this, 'GpuG5xlarge', vpc, ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE), securityGroup, instanceRole);
    cluster.addAsgCapacityProvider(cpG5xlarge);

    const { asg: asgG52xlarge, capacityProvider: cpG52xlarge } = this.createAsgAndCapacityProvider(this, 'GpuG52xlarge', vpc, ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE2), securityGroup, instanceRole);
    cluster.addAsgCapacityProvider(cpG52xlarge);

    cluster.addAsgCapacityProvider(capacityProvider);

    // S3 access logs bucket
    const accessLogsBucket = new s3.Bucket(this, 'AccessLogsBucket', {
      versioned: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN
    });

    // KMS key for results bucket
    const resultsBucketKey = new kms.Key(this, 'ResultsBucketKey', {
      enableKeyRotation: true,
      alias: 'alias/amira-letter-scoring-results'
    });

    // Results bucket (source of truth) with SSE-KMS, bucket key, access logs, and lifecycle
    const resultsBucket = new s3.Bucket(this, 'ResultsBucket', {
      versioned: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: resultsBucketKey,
      bucketKeyEnabled: true,
      serverAccessLogsBucket: accessLogsBucket,
      serverAccessLogsPrefix: 's3-access-logs/',
      lifecycleRules: [
        {
          id: 'IntelligentTieringNow',
          enabled: true,
          transitions: [{ storageClass: s3.StorageClass.INTELLIGENT_TIERING, transitionAfter: cdk.Duration.days(0) }]
        },
        {
          id: 'TransitionToIA',
          enabled: true,
          transitions: [{ storageClass: s3.StorageClass.INFREQUENT_ACCESS, transitionAfter: cdk.Duration.days(30) }]
        },
        {
          id: 'TransitionToGlacier',
          enabled: true,
          transitions: [{ storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL, transitionAfter: cdk.Duration.days(120) }]
        }
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      enforceSSL: true
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

    // Grant will be attached after taskRole is defined

    // SQS queue for jobs with DLQ
    const dlq = new sqs.Queue(this, 'JobsDLQ', {
      retentionPeriod: cdk.Duration.days(14),
      encryption: sqs.QueueEncryption.KMS_MANAGED,
      enforceSSL: true
    });
    const jobsQueue = new sqs.Queue(this, 'JobsQueue', {
      visibilityTimeout: cdk.Duration.minutes(15),
      deadLetterQueue: { queue: dlq, maxReceiveCount: 3 },
      encryption: sqs.QueueEncryption.KMS_MANAGED,
      enforceSSL: true
    });

    // Task execution role
    const taskExecutionRole = new iam.Role(this, 'TaskExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy')
      ]
    });

    // Task role with necessary permissions
    const taskRole = new iam.Role(this, 'TaskRole', {
      roleName: `amira-letter-scoring-task-${cdk.Stack.of(this).stackName}`,
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      inlinePolicies: {
        S3Access: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ['s3:ListBucket'],
              resources: [resultsBucket.bucketArn]
            }),
            new iam.PolicyStatement({
              actions: ['s3:PutObject'],
              resources: [`${resultsBucket.bucketArn}/*`]
            })
          ]
        }),
        SqsAccess: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ['sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:GetQueueAttributes'],
              resources: [jobsQueue.queueArn]
            })
          ]
        })
      }
    });

    // Conditions for conditional resources
    const audioProvided = new cdk.CfnCondition(this, 'AudioBucketProvided', {
      expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(audioBucketNameParam.valueAsString, ''))
    });
    const useTritonCondition = new cdk.CfnCondition(this, 'UseTritonCondition', {
      expression: cdk.Fn.conditionEquals(useTritonParam.valueAsString, 'true')
    });
    const audioPolicyDoc = new iam.PolicyDocument({
      statements: [
        new iam.PolicyStatement({
          actions: ['s3:ListBucket'],
          resources: [cdk.Arn.format({ service: 's3', resource: audioBucketNameParam.valueAsString }, this)],
          conditions: {
            StringLike: { 's3:prefix': [audioBucketPrefixParam.valueAsString] }
          }
        }),
        new iam.PolicyStatement({
          actions: ['s3:GetObject'],
          resources: [cdk.Arn.format({ service: 's3', resource: `${audioBucketNameParam.valueAsString}/${audioBucketPrefixParam.valueAsString}*` }, this)]
        })
      ]
    });
    const audioCfnPolicy = new iam.CfnPolicy(this, 'TaskRoleAudioReadPolicy', {
      policyDocument: audioPolicyDoc,
      roles: [taskRole.roleName!],
      policyName: `TaskRoleAudioReadPolicy-${cdk.Stack.of(this).stackName}`
    });
    audioCfnPolicy.cfnOptions.condition = audioProvided;

    // Allow task role to use the KMS key for SSE-KMS objects
    resultsBucketKey.grantEncryptDecrypt(taskRole);

    // CloudWatch Log Group
    const logGroup = new logs.LogGroup(this, 'AmiraLetterScoringLogGroup', {
      logGroupName: '/ecs/amira-letter-scoring',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryptionKey: resultsBucketKey
    });

    // ECS Task Definition
    const taskDefinition = new ecs.Ec2TaskDefinition(this, 'AmiraLetterScoringTaskDef', {
      family: `amira-letter-scoring-${cdk.Stack.of(this).stackName}`,
      executionRole: taskExecutionRole,
      taskRole,
      networkMode: ecs.NetworkMode.AWS_VPC
    });

    // Triton GPU inference server container
    const tritonContainer = taskDefinition.addContainer('TritonServerContainer', {
      image: ecs.ContainerImage.fromEcrRepository(tritonRepository, tritonImageTagParam.valueAsString),
      memoryReservationMiB: 4096,
      cpu: 1024,
      gpuCount: 1,
      logging: ecs.LogDriver.awsLogs({ logGroup, streamPrefix: 'triton-server' }),
      portMappings: [{ containerPort: 8000 }, { containerPort: 8001 }, { containerPort: 8002 }],
      healthCheck: {
        command: ['CMD-SHELL', 'curl -sf http://127.0.0.1:8000/v2/health/ready || exit 1'],
        interval: cdk.Duration.seconds(15),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(30)
      }
    });

    // DCGM exporter for GPU metrics
    const dcgmContainer = taskDefinition.addContainer('DcgmExporterContainer', {
      image: ecs.ContainerImage.fromEcrRepository(dcgmExporterRepository, dcgmImageTagParam.valueAsString),
      memoryReservationMiB: 256,
      cpu: 128,
      logging: ecs.LogDriver.awsLogs({ logGroup, streamPrefix: 'dcgm-exporter' }),
      portMappings: [{ containerPort: 9400 }],
    });

    // CloudWatch Agent to scrape DCGM metrics
    const cwAgentConfigString: string = JSON.stringify(cwAgentConfig);
    const cwAgentConfigParam = new ssm.StringParameter(this, 'SsmCwAgentConfig', {
      parameterName: '/amira/cwagent_config',
      stringValue: cwAgentConfigString
    });
    const cwAgentContainer = taskDefinition.addContainer('CloudWatchAgentContainer', {
      image: ecs.ContainerImage.fromEcrRepository(cwAgentRepository, cwAgentImageTagParam.valueAsString),
      memoryReservationMiB: 256,
      cpu: 128,
      logging: ecs.LogDriver.awsLogs({ logGroup, streamPrefix: 'cloudwatch-agent' }),
      command: ['/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent', '-a', 'fetch-config', '-m', 'ec2', '-c', `ssm:${cwAgentConfigParam.parameterName}`, '-s'],
      environment: { AWS_REGION: cdk.Stack.of(this).region }
    });

    // Grant CW Agent access to read its SSM config
    cwAgentConfigParam.grantRead(taskRole);

    // Increase nofile limits for containers
    tritonContainer.addUlimits({ name: ecs.UlimitName.NOFILE, softLimit: 65536, hardLimit: 65536 });

    // Application Load Balancer for Triton GPU cluster
    const tritonAlb = new elbv2.ApplicationLoadBalancer(this, 'TritonLoadBalancer', {
      vpc,
      internetFacing: false, // Internal ALB since Lambda will call it
      securityGroup
    });

    const tritonTargetGroup = new elbv2.ApplicationTargetGroup(this, 'TritonTargetGroup', {
      vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: '/v2/health/ready',
        protocol: elbv2.Protocol.HTTP,
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        timeout: cdk.Duration.seconds(10),
        interval: cdk.Duration.seconds(15)
      }
    });

    const tritonListener = tritonAlb.addListener('TritonListenerHttps', {
      port: 443,
      protocol: elbv2.ApplicationProtocol.HTTPS,
      certificates: [elbv2.ListenerCertificate.fromArn(tritonCertArnParam.valueAsString)],
      defaultTargetGroups: [tritonTargetGroup]
    });

    // ECS service for Triton GPU inference (scales based on demand)
    const service = new ecs.Ec2Service(this, 'TritonInferenceService', {
      cluster,
      taskDefinition,
      serviceName: 'triton-inference-service',
      desiredCount: 0, // Start with 0, scale up based on ALB requests
      securityGroups: [securityGroup],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      capacityProviderStrategies: [{
        capacityProvider: capacityProvider.capacityProviderName,
        weight: 1
      },{
        capacityProvider: cpG5xlarge.capacityProviderName,
        weight: 1
      },{
        capacityProvider: cpG52xlarge.capacityProviderName,
        weight: 1
      }],
      placementStrategies: [
        ecs.PlacementStrategy.spreadAcrossInstances()
      ],
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      enableExecuteCommand: true
    });

    // Attach ECS service to ALB target group
    service.attachToApplicationTargetGroup(tritonTargetGroup);

    // Autoscale Triton service based on ALB request metrics
    const scalableTarget = new appscaling.ScalableTarget(this, 'TritonScalableTarget', {
      serviceNamespace: appscaling.ServiceNamespace.ECS,
      maxCapacity: 10,
      minCapacity: 0,
      resourceId: `service/${cluster.clusterName}/${service.serviceName}`,
      scalableDimension: 'ecs:service:DesiredCount'
    });

    // Scale based on ALB request count per target
    const albRequestMetric = new cw.Metric({
      namespace: 'AWS/ApplicationELB',
      metricName: 'RequestCountPerTarget',
      dimensionsMap: {
        LoadBalancer: tritonAlb.loadBalancerFullName,
        TargetGroup: tritonTargetGroup.targetGroupFullName
      },
      statistic: 'Sum',
      period: cdk.Duration.minutes(1)
    });

    scalableTarget.scaleToTrackMetric('TritonScaling', {
      customMetric: albRequestMetric,
      targetValue: 50,
      scaleInCooldown: cdk.Duration.minutes(2),
      scaleOutCooldown: cdk.Duration.seconds(30)
    });

    // Add output for Triton cluster URL
    new cdk.CfnOutput(this, 'TritonClusterUrl', {
      value: `https://${tritonAlb.loadBalancerDnsName}`,
      description: 'HTTPS URL for Triton GPU inference cluster',
      condition: useTritonCondition
    });

    // GPU cluster monitoring and alarms

    // SNS notifications for alarms
    const alarmTopic = new sns.Topic(this, 'OpsAlarmTopic', { displayName: 'Triton GPU Cluster Alarms' });
    const alarmAction = new cwactions.SnsAction(alarmTopic);

    // Optional email subscription parameter
    const alarmEmailParam = new cdk.CfnParameter(this, 'AlarmEmail', {
      type: 'String',
      default: '',
      description: 'Optional email to subscribe to Ops alarms'
    });
    const emailProvided = new cdk.CfnCondition(this, 'AlarmEmailProvided', {
      expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(alarmEmailParam.valueAsString, ''))
    });
    const subscription = new sns.CfnSubscription(this, 'OpsAlarmEmailSubscription', {
      protocol: 'email',
      topicArn: alarmTopic.topicArn,
      endpoint: alarmEmailParam.valueAsString
    });
    subscription.cfnOptions.condition = emailProvided;

    // Manual enqueue Lambda for testing (accepts JSON {"activity_ids": ["...", ...]})
    const manualEnqueueFn = new lambda.Function(this, 'ManualEnqueueFunction', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset('../lambda/manual_enqueue'),
      timeout: cdk.Duration.minutes(1),
      tracing: lambda.Tracing.ACTIVE,
      environment: { JOBS_QUEUE_URL: jobsQueue.queueUrl }
    });
    jobsQueue.grantSendMessages(manualEnqueueFn);
    const manualUrl = manualEnqueueFn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.AWS_IAM,
      cors: { allowedOrigins: ['*'], allowedMethods: [lambda.HttpMethod.POST, lambda.HttpMethod.OPTIONS] }
    });

    // Alarms: GPU utilization low/high (from DCGM via CWAgent Prometheus)
    const gpuUtilMetric = new cw.Metric({
      namespace: 'CWAgent',
      metricName: 'DCGM_FI_DEV_GPU_UTIL',
      statistic: 'Average',
      period: cdk.Duration.minutes(1)
    });
    const gpuMemUsed = new cw.Metric({ namespace: 'CWAgent', metricName: 'DCGM_FI_DEV_FB_USED', statistic: 'Average', period: cdk.Duration.minutes(1) });
    const gpuMemTotal = new cw.Metric({ namespace: 'CWAgent', metricName: 'DCGM_FI_DEV_FB_TOTAL', statistic: 'Average', period: cdk.Duration.minutes(1) });
    const gpuUtilLow = new cw.Alarm(this, 'GpuUtilLow', {
      metric: gpuUtilMetric,
      threshold: 20,
      evaluationPeriods: 5,
      comparisonOperator: cw.ComparisonOperator.LESS_THAN_THRESHOLD
    });
    gpuUtilLow.addAlarmAction(alarmAction);
    const gpuUtilHigh = new cw.Alarm(this, 'GpuUtilHigh', {
      metric: gpuUtilMetric,
      threshold: 95,
      evaluationPeriods: 3,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD
    });
    gpuUtilHigh.addAlarmAction(alarmAction);

    // Triton request latency (95th percentile, microseconds)
    const tritonP95 = new cw.Metric({
      namespace: 'CWAgent',
      metricName: 'nv_inference_request_duration_us',
      statistic: 'p95',
      period: cdk.Duration.minutes(1)
    });
    const tritonQueueP95 = new cw.Metric({ namespace: 'CWAgent', metricName: 'nv_inference_queue_duration_us', statistic: 'p95', period: cdk.Duration.minutes(1) });
    const tritonThroughput = new cw.Metric({ namespace: 'CWAgent', metricName: 'nv_inference_count', statistic: 'Sum', period: cdk.Duration.minutes(1) });
    const tritonFailures = new cw.Metric({ namespace: 'CWAgent', metricName: 'nv_inference_fail', statistic: 'Sum', period: cdk.Duration.minutes(1) });
    const tritonLatencyHigh = new cw.Alarm(this, 'TritonLatencyHigh', {
      metric: tritonP95,
      threshold: 500000, // 500ms
      evaluationPeriods: 5,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD
    });
    tritonLatencyHigh.addAlarmAction(alarmAction);
    const tritonQueueHigh = new cw.Alarm(this, 'TritonQueueLatencyHigh', {
      metric: tritonQueueP95,
      threshold: 200000, // 200ms
      evaluationPeriods: 5,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD
    });
    tritonQueueHigh.addAlarmAction(alarmAction);
    const tritonFailuresHigh = new cw.Alarm(this, 'TritonFailuresHigh', {
      metric: tritonFailures,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
    });
    tritonFailuresHigh.addAlarmAction(alarmAction);

    // Dashboard for GPU and Triton
    const dashboard = new cw_dash.Dashboard(this, 'AmiraGpuTritonDashboard', { dashboardName: 'AmiraGpuTriton' });
    dashboard.addWidgets(
      new cw_dash.GraphWidget({
        title: 'GPU Utilization',
        left: [gpuUtilMetric],
        width: 12
      }),
      new cw_dash.GraphWidget({
        title: 'Triton p95 Latency (us)',
        left: [tritonP95],
        width: 12
      }),
      new cw_dash.GraphWidget({
        title: 'GPU Memory (bytes)',
        left: [gpuMemUsed, gpuMemTotal],
        width: 12
      }),
      new cw_dash.GraphWidget({
        title: 'Triton Queue p95 (us)',
        left: [tritonQueueP95],
        width: 12
      }),
      new cw_dash.GraphWidget({
        title: 'Triton Throughput (req/min) & Failures',
        left: [tritonThroughput],
        right: [tritonFailures],
        width: 24
      }),
      new cw_dash.GraphWidget({
        title: 'ECS Desired vs Running',
        left: [
          new cw.Metric({ namespace: 'ECS/ContainerInsights', metricName: 'ServiceDesiredCount', dimensionsMap: { ClusterName: cluster.clusterName, ServiceName: service.serviceName }, statistic: 'Average' }),
          new cw.Metric({ namespace: 'ECS/ContainerInsights', metricName: 'ServiceRunningCount', dimensionsMap: { ClusterName: cluster.clusterName, ServiceName: service.serviceName }, statistic: 'Average' })
        ],
        width: 24
      }),
      new cw_dash.GraphWidget({
        title: 'SQS Depth & Oldest Age',
        left: [
          new cw.Metric({ namespace: 'AWS/SQS', metricName: 'ApproximateNumberOfMessagesVisible', dimensionsMap: { QueueName: jobsQueue.queueName }, statistic: 'Average' })
        ],
        right: [
          new cw.Metric({ namespace: 'AWS/SQS', metricName: 'ApproximateAgeOfOldestMessage', dimensionsMap: { QueueName: jobsQueue.queueName }, statistic: 'Average' })
        ],
        width: 24
      }),
      new cw_dash.GraphWidget({
        title: 'ASG Desired vs InService',
        left: [
          new cw.Metric({ namespace: 'AWS/AutoScaling', metricName: 'GroupDesiredCapacity', dimensionsMap: { AutoScalingGroupName: autoScalingGroup.autoScalingGroupName }, statistic: 'Average' }),
          new cw.Metric({ namespace: 'AWS/AutoScaling', metricName: 'GroupInServiceInstances', dimensionsMap: { AutoScalingGroupName: autoScalingGroup.autoScalingGroupName }, statistic: 'Average' })
        ],
        right: [
          new cw.Metric({ namespace: 'AWS/AutoScaling', metricName: 'GroupDesiredCapacity', dimensionsMap: { AutoScalingGroupName: asgG5xlarge.autoScalingGroupName }, statistic: 'Average' }),
          new cw.Metric({ namespace: 'AWS/AutoScaling', metricName: 'GroupDesiredCapacity', dimensionsMap: { AutoScalingGroupName: asgG52xlarge.autoScalingGroupName }, statistic: 'Average' })
        ],
        width: 24
      }),
      new cw_dash.GraphWidget({
        title: 'Lambda Invocations/Errors',
        left: [
          manualEnqueueFn.metricInvocations(),
          manualEnqueueFn.metricErrors()
        ],
        right: [
          new cw.Metric({ namespace: 'AWS/Lambda', metricName: 'Invocations', dimensionsMap: { FunctionName: 'EcsDrainOnSpotFn' }, statistic: 'Sum' }),
          new cw.Metric({ namespace: 'AWS/Lambda', metricName: 'Errors', dimensionsMap: { FunctionName: 'EcsDrainOnSpotFn' }, statistic: 'Sum' })
        ],
        width: 24
      })
    );

    // Spot ITN/Rebalance drain Lambda
    const drainFn = new lambda.Function(this, 'EcsDrainOnSpotFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset('../lambda/ecs_drain_on_spot'),
      timeout: cdk.Duration.seconds(60),
      environment: { CLUSTER_ARN: cluster.clusterArn }
    });
    drainFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ecs:ListContainerInstances', 'ecs:DescribeContainerInstances'],
      resources: [cluster.clusterArn]
    }));
    drainFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ecs:UpdateContainerInstancesState'],
      resources: ['*'],
      conditions: { StringEquals: { 'ecs:cluster': cluster.clusterArn } }
    }));
    new events.Rule(this, 'SpotInterruptionDrainRule', {
      description: 'Drain ECS container instances on Spot interruption warnings',
      eventPattern: {
        source: ['aws.ec2'],
        detailType: ['EC2 Spot Instance Interruption Warning', 'EC2 Instance Rebalance Recommendation']
      },
      targets: [new targets.LambdaFunction(drainFn)]
    });

    // Outputs for GPU cluster and dashboard link
    new cdk.CfnOutput(this, 'TritonRepositoryUri', {
      value: tritonRepository.repositoryUri,
      description: 'Triton ECR Repository URI',
      condition: useTritonCondition
    });

    new cdk.CfnOutput(this, 'GpuClusterName', {
      value: cluster.clusterName,
      description: 'GPU ECS Cluster Name',
      condition: useTritonCondition
    });

    new cdk.CfnOutput(this, 'TritonServiceName', {
      value: service.serviceName,
      description: 'Triton ECS Service Name',
      condition: useTritonCondition
    });

    new cdk.CfnOutput(this, 'GpuDashboardUrl', {
      value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=${dashboard.dashboardName}`,
      description: 'CloudWatch GPU/Triton Dashboard URL',
      condition: useTritonCondition
    });
  }
}
