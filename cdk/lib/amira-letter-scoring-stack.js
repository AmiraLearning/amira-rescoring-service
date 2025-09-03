"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.AmiraLetterScoringStack = void 0;
const cdk = require("aws-cdk-lib");
const ec2 = require("aws-cdk-lib/aws-ec2");
const ecs = require("aws-cdk-lib/aws-ecs");
const ecr = require("aws-cdk-lib/aws-ecr");
const iam = require("aws-cdk-lib/aws-iam");
const events = require("aws-cdk-lib/aws-events");
const targets = require("aws-cdk-lib/aws-events-targets");
const autoscaling = require("aws-cdk-lib/aws-autoscaling");
const logs = require("aws-cdk-lib/aws-logs");
const s3 = require("aws-cdk-lib/aws-s3");
const sqs = require("aws-cdk-lib/aws-sqs");
const lambda = require("aws-cdk-lib/aws-lambda");
const cw = require("aws-cdk-lib/aws-cloudwatch");
const appscaling = require("aws-cdk-lib/aws-applicationautoscaling");
const cwactions = require("aws-cdk-lib/aws-cloudwatch-actions");
const kms = require("aws-cdk-lib/aws-kms");
const sns = require("aws-cdk-lib/aws-sns");
const ssm = require("aws-cdk-lib/aws-ssm");
const cw_dash = require("aws-cdk-lib/aws-cloudwatch");
const elbv2 = require("aws-cdk-lib/aws-elasticloadbalancingv2");
const cwAgentConfig = require("./cw-agent-config.json");
class AmiraLetterScoringStack extends cdk.Stack {
    createAsgAndCapacityProvider(scope, id, vpc, instanceType, securityGroup, role) {
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
    constructor(scope, id, props) {
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
            default: 2,
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
        // Optional TLS cert/key for target sidecar via Secrets Manager (expects JSON with fields: cert, key)
        const tritonTargetCertSecretArnParam = new cdk.CfnParameter(this, 'TritonTargetCertSecretArn', {
            type: 'String',
            default: '',
            description: 'Optional Secrets Manager ARN containing JSON with fields {"cert","key"} for sidecar TLS'
        });
        // TLS tuning for sidecar
        const enableTargetHttp2Param = new cdk.CfnParameter(this, 'EnableTargetHttp2', {
            type: 'String',
            default: 'true',
            allowedValues: ['true', 'false'],
            description: 'Enable HTTP/2 on TLS sidecar listener'
        });
        const targetSslCiphersParam = new cdk.CfnParameter(this, 'TargetSslCiphers', {
            type: 'String',
            default: '',
            description: 'Optional OpenSSL cipher suite string for TLS sidecar (empty for default)'
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
        // Toggle for Interface VPC Endpoints (can be disabled to reduce costs)
        const enableInterfaceEndpointsParam = new cdk.CfnParameter(this, 'EnableInterfaceEndpoints', {
            type: 'String',
            default: 'true',
            allowedValues: ['true', 'false'],
            description: 'Enable creation of Interface VPC Endpoints (ECR, CW Logs, SQS, SSM, STS, Secrets, KMS)'
        });
        const endpointsEnabled = new cdk.CfnCondition(this, 'InterfaceEndpointsEnabled', {
            expression: cdk.Fn.conditionEquals(enableInterfaceEndpointsParam.valueAsString, 'true')
        });
        // VPC Endpoints to reduce NAT egress
        vpc.addGatewayEndpoint('S3Endpoint', {
            service: ec2.GatewayVpcEndpointAwsService.S3,
            subnets: [{ subnets: vpc.privateSubnets }]
        });
        const ecrApiEp = vpc.addInterfaceEndpoint('EcrApiEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.ECR,
            subnets: { subnets: vpc.privateSubnets }
        });
        const ecrDockerEp = vpc.addInterfaceEndpoint('EcrDockerEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            subnets: { subnets: vpc.privateSubnets }
        });
        const cwLogsEp = vpc.addInterfaceEndpoint('CloudWatchLogsEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            subnets: { subnets: vpc.privateSubnets }
        });
        const sqsEp = vpc.addInterfaceEndpoint('SqsEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.SQS,
            subnets: { subnets: vpc.privateSubnets }
        });
        const ssmEp = vpc.addInterfaceEndpoint('SsmEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.SSM,
            subnets: { subnets: vpc.privateSubnets }
        });
        const ssmMsgsEp = vpc.addInterfaceEndpoint('SsmMessagesEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES,
            subnets: { subnets: vpc.privateSubnets }
        });
        const ec2MsgsEp = vpc.addInterfaceEndpoint('Ec2MessagesEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES,
            subnets: { subnets: vpc.privateSubnets }
        });
        const stsEp = vpc.addInterfaceEndpoint('StsEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.STS,
            subnets: { subnets: vpc.privateSubnets }
        });
        const secretsEp = vpc.addInterfaceEndpoint('SecretsManagerEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            subnets: { subnets: vpc.privateSubnets }
        });
        const kmsEp = vpc.addInterfaceEndpoint('KmsEndpoint', {
            service: ec2.InterfaceVpcEndpointAwsService.KMS,
            subnets: { subnets: vpc.privateSubnets }
        });
        ecrApiEp.node.defaultChild.cfnOptions.condition = endpointsEnabled;
        ecrDockerEp.node.defaultChild.cfnOptions.condition = endpointsEnabled;
        cwLogsEp.node.defaultChild.cfnOptions.condition = endpointsEnabled;
        sqsEp.node.defaultChild.cfnOptions.condition = endpointsEnabled;
        ssmEp.node.defaultChild.cfnOptions.condition = endpointsEnabled;
        ssmMsgsEp.node.defaultChild.cfnOptions.condition = endpointsEnabled;
        ec2MsgsEp.node.defaultChild.cfnOptions.condition = endpointsEnabled;
        stsEp.node.defaultChild.cfnOptions.condition = endpointsEnabled;
        secretsEp.node.defaultChild.cfnOptions.condition = endpointsEnabled;
        kmsEp.node.defaultChild.cfnOptions.condition = endpointsEnabled;
        // Security groups split: ALB and ECS tasks
        const albSecurityGroup = new ec2.SecurityGroup(this, 'AmiraAlbSecurityGroup', {
            vpc,
            description: 'Security group for internal ALB fronting Triton TLS proxy',
            allowAllOutbound: true
        });
        // Restrict ALB ingress: allow either from a specific client SG, or fallback to VPC CIDR
        const albClientSgParam = new cdk.CfnParameter(this, 'AlbClientSecurityGroupId', {
            type: 'String',
            default: '',
            description: 'Optional Security Group ID allowed to access ALB:443. Leave blank to default to VPC CIDR.'
        });
        const clientSgProvided = new cdk.CfnCondition(this, 'AlbClientSgProvided', {
            expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(albClientSgParam.valueAsString, ''))
        });
        const ingressFromClientSg = new ec2.CfnSecurityGroupIngress(this, 'AlbIngressFromClientSg', {
            groupId: albSecurityGroup.securityGroupId,
            ipProtocol: 'tcp',
            fromPort: 443,
            toPort: 443,
            sourceSecurityGroupId: albClientSgParam.valueAsString
        });
        ingressFromClientSg.cfnOptions.condition = clientSgProvided;
        const ingressFromVpcCidr = new ec2.CfnSecurityGroupIngress(this, 'AlbIngressFromVpcCidr', {
            groupId: albSecurityGroup.securityGroupId,
            ipProtocol: 'tcp',
            fromPort: 443,
            toPort: 443,
            cidrIp: vpc.vpcCidrBlock
        });
        ingressFromVpcCidr.cfnOptions.condition = new cdk.CfnCondition(this, 'AlbClientSgNotProvided', {
            expression: cdk.Fn.conditionEquals(albClientSgParam.valueAsString, '')
        });
        const ecsSecurityGroup = new ec2.SecurityGroup(this, 'AmiraEcsSecurityGroup', {
            vpc,
            description: 'Security group for Amira Letter Scoring ECS tasks',
            allowAllOutbound: true
        });
        ecsSecurityGroup.addIngressRule(albSecurityGroup, ec2.Port.tcp(8443), 'Allow ALB to reach TLS proxy over 8443');
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
            instanceType: ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE4),
            machineImage: ecs.EcsOptimizedImage.amazonLinux2(ecs.AmiHardwareType.GPU),
            userData: ec2.UserData.forLinux(),
            securityGroup: ecsSecurityGroup,
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
        const { asg: asgG5xlarge, capacityProvider: cpG5xlarge } = this.createAsgAndCapacityProvider(this, 'GpuG5xlarge', vpc, ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE), ecsSecurityGroup, instanceRole);
        cluster.addAsgCapacityProvider(cpG5xlarge);
        const { asg: asgG52xlarge, capacityProvider: cpG52xlarge } = this.createAsgAndCapacityProvider(this, 'GpuG52xlarge', vpc, ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE2), ecsSecurityGroup, instanceRole);
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
            versioned: true,
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
                }
            ],
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            enforceSSL: true
        });
        resultsBucket.addToResourcePolicy(new iam.PolicyStatement({
            sid: 'DenyInsecureTransport',
            effect: iam.Effect.DENY,
            principals: [new iam.AnyPrincipal()],
            actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket', 's3:DeleteObject', 's3:DeleteObjectVersion'],
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
            roles: [taskRole.roleName],
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
        // VPC Flow Logs
        const vpcFlowLogGroup = new logs.LogGroup(this, 'VpcFlowLogs', {
            retention: logs.RetentionDays.ONE_MONTH,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
            encryptionKey: resultsBucketKey
        });
        vpc.addFlowLog('FlowLogsAll', {
            destination: ec2.FlowLogDestination.toCloudWatchLogs(vpcFlowLogGroup),
            trafficType: ec2.FlowLogTrafficType.ALL
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
        // TLS proxy sidecar to terminate TLS inside the task and proxy to Triton over localhost:8000
        const requireTargetTlsSecretParam = new cdk.CfnParameter(this, 'RequireTargetTlsSecret', {
            type: 'String',
            default: 'false',
            allowedValues: ['true', 'false'],
            description: 'Require TLS cert/key secret for sidecar (disallow self-signed)'
        });
        const tlsProxyContainer = taskDefinition.addContainer('TlsProxyContainer', {
            image: ecs.ContainerImage.fromRegistry('nginx:1.25-alpine'),
            memoryReservationMiB: 128,
            cpu: 128,
            logging: ecs.LogDriver.awsLogs({ logGroup, streamPrefix: 'tls-proxy' }),
            portMappings: [{ containerPort: 8443 }],
            environment: {
                ENABLE_HTTP2: enableTargetHttp2Param.valueAsString,
                SSL_CIPHERS: targetSslCiphersParam.valueAsString,
                REQUIRE_TLS_SECRET: requireTargetTlsSecretParam.valueAsString,
            },
            command: [
                'sh',
                '-c',
                [
                    'set -e',
                    'apk add --no-cache openssl',
                    'mkdir -p /etc/nginx/certs /etc/nginx/conf.d',
                    'if [ "$REQUIRE_TLS_SECRET" = "true" ] && { [ -z "${TLS_CERT:-}" ] || [ -z "${TLS_KEY:-}" ]; }; then echo "TLS cert/key secret required" >&2; exit 1; fi',
                    'if [ -n "${TLS_CERT:-}" ] && [ -n "${TLS_KEY:-}" ]; then echo "$TLS_CERT" > /etc/nginx/certs/tls.crt && echo "$TLS_KEY" > /etc/nginx/certs/tls.key; else openssl req -x509 -nodes -days 3650 -newkey rsa:2048 -subj "/CN=localhost" -keyout /etc/nginx/certs/tls.key -out /etc/nginx/certs/tls.crt; fi',
                    "HTTP2_DIRECTIVE='' && [ \"$ENABLE_HTTP2\" = \"true\" ] && HTTP2_DIRECTIVE=' http2' || true",
                    "CIPHERS_DIRECTIVE='' && [ -n \"$SSL_CIPHERS\" ] && CIPHERS_DIRECTIVE=\\\"  ssl_ciphers $SSL_CIPHERS;\\\" || true",
                    "printf 'server {\\n  listen 8443 ssl%s;\\n  ssl_certificate /etc/nginx/certs/tls.crt;\\n  ssl_certificate_key /etc/nginx/certs/tls.key;\\n  ssl_protocols TLSv1.2 TLSv1.3;\\n%s\\n  location / {\\n    proxy_pass http://127.0.0.1:8000;\\n    proxy_set_header Host $host;\\n    proxy_set_header X-Forwarded-Proto $scheme;\\n    proxy_set_header X-Forwarded-For $remote_addr;\\n  }\\n}\\n' \"$HTTP2_DIRECTIVE\" \"$CIPHERS_DIRECTIVE\" > /etc/nginx/conf.d/default.conf",
                    "nginx -g 'daemon off;'",
                ].join(' && '),
            ],
        });
        // Conditionally wire Secrets Manager secret with {cert,key} to TLS_CERT/TLS_KEY env for sidecar
        const targetCertProvided = new cdk.CfnCondition(this, 'TritonTargetCertProvided', {
            expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(tritonTargetCertSecretArnParam.valueAsString, ''))
        });
        // Use property override to conditionally add secrets to the TLS proxy container
        const cfnTaskDef = taskDefinition.node.defaultChild;
        const certValueFrom = cdk.Fn.join('', [tritonTargetCertSecretArnParam.valueAsString, ':cert::']);
        const keyValueFrom = cdk.Fn.join('', [tritonTargetCertSecretArnParam.valueAsString, ':key::']);
        // Find the TlsProxyContainer index and conditionally set secrets
        cfnTaskDef.addPropertyOverride('ContainerDefinitions.1.Secrets', cdk.Fn.conditionIf(targetCertProvided.logicalId, [
            { name: 'TLS_CERT', valueFrom: certValueFrom },
            { name: 'TLS_KEY', valueFrom: keyValueFrom },
        ], cdk.Aws.NO_VALUE));
        // DCGM exporter for GPU metrics
        const dcgmContainer = taskDefinition.addContainer('DcgmExporterContainer', {
            image: ecs.ContainerImage.fromEcrRepository(dcgmExporterRepository, dcgmImageTagParam.valueAsString),
            memoryReservationMiB: 256,
            cpu: 128,
            logging: ecs.LogDriver.awsLogs({ logGroup, streamPrefix: 'dcgm-exporter' }),
            portMappings: [{ containerPort: 9400 }],
        });
        // CloudWatch Agent to scrape DCGM metrics
        const cwAgentConfigString = JSON.stringify(cwAgentConfig);
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
            internetFacing: false,
            securityGroup: albSecurityGroup,
            deletionProtection: true
        });
        tritonAlb.logAccessLogs(accessLogsBucket, 'alb-access-logs/');
        const tritonTargetGroup = new elbv2.ApplicationTargetGroup(this, 'TritonTargetGroup', {
            vpc,
            port: 8443,
            protocol: elbv2.ApplicationProtocol.HTTPS,
            targetType: elbv2.TargetType.IP,
            healthCheck: {
                path: '/v2/health/ready',
                protocol: elbv2.Protocol.HTTPS,
                healthyThresholdCount: 3,
                unhealthyThresholdCount: 5,
                timeout: cdk.Duration.seconds(10),
                interval: cdk.Duration.seconds(30)
            }
        });
        const tritonListener = tritonAlb.addListener('TritonListenerHttps', {
            port: 443,
            protocol: elbv2.ApplicationProtocol.HTTPS,
            sslPolicy: elbv2.SslPolicy.TLS12_EXT,
            certificates: [elbv2.ListenerCertificate.fromArn(tritonCertArnParam.valueAsString)],
            defaultTargetGroups: [tritonTargetGroup]
        });
        // ECS service for Triton GPU inference (scales based on demand)
        const service = new ecs.Ec2Service(this, 'TritonInferenceService', {
            cluster,
            taskDefinition,
            serviceName: 'triton-inference-service',
            desiredCount: 0,
            securityGroups: [ecsSecurityGroup],
            vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
            capacityProviderStrategies: [{
                    capacityProvider: capacityProvider.capacityProviderName,
                    weight: 1
                }, {
                    capacityProvider: cpG5xlarge.capacityProviderName,
                    weight: 1
                }, {
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
        // Additional scaling policy based on Triton p95 latency
        const tritonLatencyP95Metric = new cw.Metric({
            namespace: 'CWAgent',
            metricName: 'nv_inference_request_duration_us',
            statistic: 'p95',
            period: cdk.Duration.minutes(1)
        });
        const latencyHigh = new cw.Alarm(this, 'TritonLatencyHighForScaling', {
            metric: tritonLatencyP95Metric,
            threshold: 500000,
            evaluationPeriods: 3,
            comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD
        });
        const latencyLow = new cw.Alarm(this, 'TritonLatencyLowForScaling', {
            metric: tritonLatencyP95Metric,
            threshold: 200000,
            evaluationPeriods: 5,
            comparisonOperator: cw.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD
        });
        scalableTarget.scaleOnMetric('LatencyScaleOut', {
            metric: tritonLatencyP95Metric,
            scalingSteps: [
                { lower: 500000, change: +1 },
                { lower: 800000, change: +2 }
            ],
            adjustmentType: appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            cooldown: cdk.Duration.minutes(2),
            minAdjustmentMagnitude: 1
        });
        // Optional scaling based on GPU utilization (DCGM)
        const gpuUtilForScaling = new cw.Metric({
            namespace: 'CWAgent',
            metricName: 'DCGM_FI_DEV_GPU_UTIL',
            statistic: 'Average',
            period: cdk.Duration.minutes(1)
        });
        scalableTarget.scaleOnMetric('GpuUtilScaling', {
            metric: gpuUtilForScaling,
            scalingSteps: [
                { lower: 70, change: +1 },
                { lower: 90, change: +2 },
                { upper: 30, change: -1 }
            ],
            adjustmentType: appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            cooldown: cdk.Duration.minutes(2),
            minAdjustmentMagnitude: 1
        });
        // Add output for Triton cluster URL
        new cdk.CfnOutput(this, 'TritonClusterUrl', {
            value: `https://${tritonAlb.loadBalancerDnsName}`,
            description: 'HTTPS URL for Triton GPU inference cluster',
            condition: useTritonCondition
        });
        // Publish Triton URL to SSM for cross-stack linking
        new ssm.StringParameter(this, 'TritonAlbUrlParam', {
            parameterName: '/amira/triton_alb_url',
            stringValue: `https://${tritonAlb.loadBalancerDnsName}`
        });
        // Publish VPC and subnet attributes for cross-stack Lambda attachment
        new ssm.StringParameter(this, 'VpcIdParam', {
            parameterName: '/amira/vpc_id',
            stringValue: vpc.vpcId
        });
        new ssm.StringParameter(this, 'VpcPrivateSubnetIdsParam', {
            parameterName: '/amira/vpc_private_subnet_ids',
            stringValue: vpc.privateSubnets.map(s => s.subnetId).join(',')
        });
        new ssm.StringParameter(this, 'AlbSecurityGroupIdParam', {
            parameterName: '/amira/alb_sg_id',
            stringValue: albSecurityGroup.securityGroupId
        });
        // GPU cluster monitoring and alarms
        // SNS notifications for alarms
        const alarmTopic = new sns.Topic(this, 'OpsAlarmTopic', { displayName: 'Triton GPU Cluster Alarms' });
        const alarmAction = new cwactions.SnsAction(alarmTopic);
        // Optional Secrets Manager rotation alarm (if secret ARN provided)
        const rotationAlarmEmailParam = new cdk.CfnParameter(this, 'RotationAlarmEmail', {
            type: 'String',
            default: '',
            description: 'Optional email for Secrets Manager rotation failure alarms'
        });
        const rotationAlarmEmailProvided = new cdk.CfnCondition(this, 'RotationAlarmEmailProvided', {
            expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(rotationAlarmEmailParam.valueAsString, ''))
        });
        const rotationFailuresMetric = new cw.Metric({
            namespace: 'AWS/SecretsManager',
            metricName: 'RotationEnabled',
            statistic: 'Minimum',
            period: cdk.Duration.minutes(5)
        });
        const rotationAlarm = new cw.Alarm(this, 'SecretsRotationDisabled', {
            metric: rotationFailuresMetric,
            threshold: 1,
            evaluationPeriods: 1,
            comparisonOperator: cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            alarmDescription: 'Secrets Manager rotation disabled or failing'
        });
        rotationAlarm.addAlarmAction(alarmAction);
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
            threshold: 500000,
            evaluationPeriods: 5,
            comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD
        });
        tritonLatencyHigh.addAlarmAction(alarmAction);
        const tritonQueueHigh = new cw.Alarm(this, 'TritonQueueLatencyHigh', {
            metric: tritonQueueP95,
            threshold: 200000,
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
        // Alarm for DLQ depth in GPU stack
        const jobsDlqDepthAlarm = new cw.Alarm(this, 'JobsDlqDepthAlarm', {
            metric: dlq.metricApproximateNumberOfMessagesVisible(),
            threshold: 1,
            evaluationPeriods: 1,
            comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
        });
        jobsDlqDepthAlarm.addAlarmAction(alarmAction);
        // Dashboard for GPU and Triton
        const dashboard = new cw_dash.Dashboard(this, 'AmiraGpuTritonDashboard', { dashboardName: 'AmiraGpuTriton' });
        dashboard.addWidgets(new cw_dash.GraphWidget({
            title: 'GPU Utilization',
            left: [gpuUtilMetric],
            width: 12
        }), new cw_dash.GraphWidget({
            title: 'Triton p95 Latency (us)',
            left: [tritonP95],
            width: 12
        }), new cw_dash.GraphWidget({
            title: 'GPU Memory (bytes)',
            left: [gpuMemUsed, gpuMemTotal],
            width: 12
        }), new cw_dash.GraphWidget({
            title: 'Triton Queue p95 (us)',
            left: [tritonQueueP95],
            width: 12
        }), new cw_dash.GraphWidget({
            title: 'Triton Throughput (req/min) & Failures',
            left: [tritonThroughput],
            right: [tritonFailures],
            width: 24
        }), new cw_dash.GraphWidget({
            title: 'Inference SLOs (p95 ms)',
            left: [
                new cw.Metric({ namespace: 'Amira/Inference', metricName: 'InferenceTotalMs', statistic: 'p95', period: cdk.Duration.minutes(1) }),
            ],
            width: 12
        }), new cw_dash.GraphWidget({
            title: 'Activity SLOs (p95 ms)',
            left: [
                new cw.Metric({ namespace: 'Amira/Activity', metricName: 'ActivityTotalMs', statistic: 'p95', period: cdk.Duration.minutes(1) }),
            ],
            width: 12
        }), new cw_dash.GraphWidget({
            title: 'ECS Desired vs Running',
            left: [
                new cw.Metric({ namespace: 'ECS/ContainerInsights', metricName: 'ServiceDesiredCount', dimensionsMap: { ClusterName: cluster.clusterName, ServiceName: service.serviceName }, statistic: 'Average' }),
                new cw.Metric({ namespace: 'ECS/ContainerInsights', metricName: 'ServiceRunningCount', dimensionsMap: { ClusterName: cluster.clusterName, ServiceName: service.serviceName }, statistic: 'Average' })
            ],
            width: 24
        }), new cw_dash.GraphWidget({
            title: 'SQS Depth & Oldest Age',
            left: [
                new cw.Metric({ namespace: 'AWS/SQS', metricName: 'ApproximateNumberOfMessagesVisible', dimensionsMap: { QueueName: jobsQueue.queueName }, statistic: 'Average' })
            ],
            right: [
                new cw.Metric({ namespace: 'AWS/SQS', metricName: 'ApproximateAgeOfOldestMessage', dimensionsMap: { QueueName: jobsQueue.queueName }, statistic: 'Average' })
            ],
            width: 24
        }), new cw_dash.GraphWidget({
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
        }), new cw_dash.GraphWidget({
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
        }));
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
exports.AmiraLetterScoringStack = AmiraLetterScoringStack;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiYW1pcmEtbGV0dGVyLXNjb3Jpbmctc3RhY2suanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyJhbWlyYS1sZXR0ZXItc2NvcmluZy1zdGFjay50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOzs7QUFBQSxtQ0FBbUM7QUFDbkMsMkNBQTJDO0FBQzNDLDJDQUEyQztBQUMzQywyQ0FBMkM7QUFDM0MsMkNBQTJDO0FBQzNDLGlEQUFpRDtBQUNqRCwwREFBMEQ7QUFDMUQsMkRBQTJEO0FBQzNELDZDQUE2QztBQUM3Qyx5Q0FBeUM7QUFDekMsMkNBQTJDO0FBQzNDLGlEQUFpRDtBQUNqRCxpREFBaUQ7QUFDakQscUVBQXFFO0FBQ3JFLGdFQUFnRTtBQUNoRSwyQ0FBMkM7QUFDM0MsMkNBQTJDO0FBQzNDLDJDQUEyQztBQUMzQyxzREFBc0Q7QUFDdEQsZ0VBQWdFO0FBR2hFLHdEQUF5RDtBQUV6RCxNQUFhLHVCQUF3QixTQUFRLEdBQUcsQ0FBQyxLQUFLO0lBQzVDLDRCQUE0QixDQUFDLEtBQWdCLEVBQUUsRUFBVSxFQUFFLEdBQWEsRUFBRSxZQUE4QixFQUFFLGFBQWlDLEVBQUUsSUFBZTtRQUNsSyxNQUFNLEVBQUUsR0FBRyxJQUFJLEdBQUcsQ0FBQyxjQUFjLENBQUMsS0FBSyxFQUFFLEdBQUcsRUFBRSxnQkFBZ0IsRUFBRTtZQUM5RCxZQUFZO1lBQ1osWUFBWSxFQUFFLEdBQUcsQ0FBQyxpQkFBaUIsQ0FBQyxZQUFZLENBQUMsR0FBRyxDQUFDLGVBQWUsQ0FBQyxHQUFHLENBQUM7WUFDekUsUUFBUSxFQUFFLEdBQUcsQ0FBQyxRQUFRLENBQUMsUUFBUSxFQUFFO1lBQ2pDLGFBQWE7WUFDYixJQUFJO1lBQ0osYUFBYSxFQUFFLElBQUk7WUFDbkIsV0FBVyxFQUFFLEVBQUUsV0FBVyxFQUFFLEdBQUcsQ0FBQyxlQUFlLENBQUMsUUFBUSxFQUFFLG9CQUFvQixFQUFFLEdBQUcsQ0FBQyx3QkFBd0IsQ0FBQyxJQUFJLEVBQUU7U0FDcEgsQ0FBQyxDQUFDO1FB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WUFDdEMsU0FBUyxFQUFFLGtCQUFrQjtTQUM5QixDQUFDLENBQUM7UUFFSCxJQUFJLEdBQUcsQ0FBQyxTQUFTLENBQUMsSUFBSSxFQUFFLGlCQUFpQixFQUFFO1lBQ3pDLEtBQUssRUFBRSxXQUFXLElBQUksQ0FBQyxNQUFNLGtEQUFrRCxJQUFJLENBQUMsTUFBTSxvQkFBb0IsU0FBUyxDQUFDLGFBQWEsRUFBRTtZQUN2SSxXQUFXLEVBQUUscUNBQXFDO1lBQ2xELFNBQVMsRUFBRSxrQkFBa0I7U0FDOUIsQ0FBQyxDQUFDO0lBQ0wsQ0FBQztDQUNGO0FBcGpDRCwwREFvakNDIiwic291cmNlc0NvbnRlbnQiOlsiaW1wb3J0ICogYXMgY2RrIGZyb20gJ2F3cy1jZGstbGliJztcbmltcG9ydCAqIGFzIGVjMiBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZWMyJztcbmltcG9ydCAqIGFzIGVjcyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZWNzJztcbmltcG9ydCAqIGFzIGVjciBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZWNyJztcbmltcG9ydCAqIGFzIGlhbSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtaWFtJztcbmltcG9ydCAqIGFzIGV2ZW50cyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZXZlbnRzJztcbmltcG9ydCAqIGFzIHRhcmdldHMgZnJvbSAnYXdzLWNkay1saWIvYXdzLWV2ZW50cy10YXJnZXRzJztcbmltcG9ydCAqIGFzIGF1dG9zY2FsaW5nIGZyb20gJ2F3cy1jZGstbGliL2F3cy1hdXRvc2NhbGluZyc7XG5pbXBvcnQgKiBhcyBsb2dzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1sb2dzJztcbmltcG9ydCAqIGFzIHMzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zMyc7XG5pbXBvcnQgKiBhcyBzcXMgZnJvbSAnYXdzLWNkay1saWIvYXdzLXNxcyc7XG5pbXBvcnQgKiBhcyBsYW1iZGEgZnJvbSAnYXdzLWNkay1saWIvYXdzLWxhbWJkYSc7XG5pbXBvcnQgKiBhcyBjdyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtY2xvdWR3YXRjaCc7XG5pbXBvcnQgKiBhcyBhcHBzY2FsaW5nIGZyb20gJ2F3cy1jZGstbGliL2F3cy1hcHBsaWNhdGlvbmF1dG9zY2FsaW5nJztcbmltcG9ydCAqIGFzIGN3YWN0aW9ucyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtY2xvdWR3YXRjaC1hY3Rpb25zJztcbmltcG9ydCAqIGFzIGttcyBmcm9tICdhd3MtY2RrLWxpYi9hd3Mta21zJztcbmltcG9ydCAqIGFzIHNucyBmcm9tICdhd3MtY2RrLWxpYi9hd3Mtc25zJztcbmltcG9ydCAqIGFzIHNzbSBmcm9tICdhd3MtY2RrLWxpYi9hd3Mtc3NtJztcbmltcG9ydCAqIGFzIGN3X2Rhc2ggZnJvbSAnYXdzLWNkay1saWIvYXdzLWNsb3Vkd2F0Y2gnO1xuaW1wb3J0ICogYXMgZWxidjIgZnJvbSAnYXdzLWNkay1saWIvYXdzLWVsYXN0aWNsb2FkYmFsYW5jaW5ndjInO1xuaW1wb3J0ICogYXMgYWNtIGZyb20gJ2F3cy1jZGstbGliL2F3cy1jZXJ0aWZpY2F0ZW1hbmFnZXInO1xuaW1wb3J0IHsgQ29uc3RydWN0IH0gZnJvbSAnY29uc3RydWN0cyc7XG5pbXBvcnQgY3dBZ2VudENvbmZpZyA9IHJlcXVpcmUoJy4vY3ctYWdlbnQtY29uZmlnLmpzb24nKTtcblxuZXhwb3J0IGNsYXNzIEFtaXJhTGV0dGVyU2NvcmluZ1N0YWNrIGV4dGVuZHMgY2RrLlN0YWNrIHtcbiAgcHJpdmF0ZSBjcmVhdGVBc2dBbmRDYXBhY2l0eVByb3ZpZGVyKHNjb3BlOiBDb25zdHJ1Y3QsIGlkOiBzdHJpbmcsIHZwYzogZWMyLklWcGMsIGluc3RhbmNlVHlwZTogZWMyLkluc3RhbmNlVHlwZSwgc2VjdXJpdHlHcm91cDogZWMyLklTZWN1cml0eUdyb3VwLCByb2xlOiBpYW0uSVJvbGUpOiB7IGFzZzogYXV0b3NjYWxpbmcuQXV0b1NjYWxpbmdHcm91cDsgY2FwYWNpdHlQcm92aWRlcjogZWNzLkFzZ0NhcGFjaXR5UHJvdmlkZXIgfSB7XG4gICAgY29uc3QgbHQgPSBuZXcgZWMyLkxhdW5jaFRlbXBsYXRlKHNjb3BlLCBgJHtpZH1MYXVuY2hUZW1wbGF0ZWAsIHtcbiAgICAgIGluc3RhbmNlVHlwZSxcbiAgICAgIG1hY2hpbmVJbWFnZTogZWNzLkVjc09wdGltaXplZEltYWdlLmFtYXpvbkxpbnV4MihlY3MuQW1pSGFyZHdhcmVUeXBlLkdQVSksXG4gICAgICB1c2VyRGF0YTogZWMyLlVzZXJEYXRhLmZvckxpbnV4KCksXG4gICAgICBzZWN1cml0eUdyb3VwLFxuICAgICAgcm9sZSxcbiAgICAgIHJlcXVpcmVJbWRzdjI6IHRydWUsXG4gICAgICBzcG90T3B0aW9uczogeyByZXF1ZXN0VHlwZTogZWMyLlNwb3RSZXF1ZXN0VHlwZS5PTkVfVElNRSwgaW50ZXJydXB0aW9uQmVoYXZpb3I6IGVjMi5TcG90SW5zdGFuY2VJbnRlcnJ1cHRpb24uU1RPUCB9XG4gICAgfSk7XG5cbiAgICBjb25zdCBhc2cgPSBuZXcgYXV0b3NjYWxpbmcuQXV0b1NjYWxpbmdHcm91cChzY29wZSwgYCR7aWR9QXNnYCwge1xuICAgICAgdnBjLFxuICAgICAgbGF1bmNoVGVtcGxhdGU6IGx0LFxuICAgICAgbWluQ2FwYWNpdHk6IDAsXG4gICAgICBtYXhDYXBhY2l0eTogMTAsXG4gICAgICBkZXNpcmVkQ2FwYWNpdHk6IDAsXG4gICAgICB2cGNTdWJuZXRzOiB7IHN1Ym5ldFR5cGU6IGVjMi5TdWJuZXRUeXBlLlBSSVZBVEVfV0lUSF9FR1JFU1MgfSxcbiAgICAgIGNhcGFjaXR5UmViYWxhbmNlOiB0cnVlXG4gICAgfSk7XG5cbiAgICBjb25zdCBjYXBhY2l0eVByb3ZpZGVyID0gbmV3IGVjcy5Bc2dDYXBhY2l0eVByb3ZpZGVyKHNjb3BlLCBgJHtpZH1DYXBhY2l0eVByb3ZpZGVyYCwge1xuICAgICAgYXV0b1NjYWxpbmdHcm91cDogYXNnLFxuICAgICAgZW5hYmxlTWFuYWdlZFNjYWxpbmc6IHRydWUsXG4gICAgICBlbmFibGVNYW5hZ2VkVGVybWluYXRpb25Qcm90ZWN0aW9uOiB0cnVlLFxuICAgICAgdGFyZ2V0Q2FwYWNpdHlQZXJjZW50OiAxMDAsXG4gICAgICBtYWNoaW5lSW1hZ2VUeXBlOiBlY3MuTWFjaGluZUltYWdlVHlwZS5BTUFaT05fTElOVVhfMlxuICAgIH0pO1xuICAgIHJldHVybiB7IGFzZywgY2FwYWNpdHlQcm92aWRlciB9O1xuICB9XG4gIGNvbnN0cnVjdG9yKHNjb3BlOiBDb25zdHJ1Y3QsIGlkOiBzdHJpbmcsIHByb3BzPzogY2RrLlN0YWNrUHJvcHMpIHtcbiAgICBzdXBlcihzY29wZSwgaWQsIHByb3BzKTtcblxuICAgIC8vIEVDUiBSZXBvc2l0b3JpZXMgZm9yIGNvbnRhaW5lcnNcbiAgICBjb25zdCByZXBvc2l0b3J5ID0gbmV3IGVjci5SZXBvc2l0b3J5KHRoaXMsICdBbWlyYUxldHRlclNjb3JpbmdSZXBvJywge1xuICAgICAgcmVwb3NpdG9yeU5hbWU6ICdhbWlyYS1sZXR0ZXItc2NvcmluZycsXG4gICAgICBpbWFnZVNjYW5PblB1c2g6IHRydWUsXG4gICAgICBsaWZlY3ljbGVSdWxlczogW3tcbiAgICAgICAgbWF4SW1hZ2VDb3VudDogMTAsXG4gICAgICAgIGRlc2NyaXB0aW9uOiAnS2VlcCBvbmx5IDEwIG1vc3QgcmVjZW50IGltYWdlcydcbiAgICAgIH1dXG4gICAgfSk7XG5cbiAgICAvLyBFQ1IgcmVwb3NpdG9yaWVzIGZvciBUcml0b24gR1BVIGNsdXN0ZXIgKGNvbmRpdGlvbmFsKVxuICAgIGNvbnN0IHRyaXRvblJlcG9zaXRvcnkgPSBuZXcgZWNyLlJlcG9zaXRvcnkodGhpcywgJ1RyaXRvblNlcnZlclJlcG8nLCB7XG4gICAgICByZXBvc2l0b3J5TmFtZTogJ3RyaXRvbi1zZXJ2ZXInLFxuICAgICAgaW1hZ2VTY2FuT25QdXNoOiB0cnVlLFxuICAgICAgbGlmZWN5Y2xlUnVsZXM6IFt7IG1heEltYWdlQ291bnQ6IDUgfV1cbiAgICB9KTtcbiAgICBjb25zdCBjd0FnZW50UmVwb3NpdG9yeSA9IG5ldyBlY3IuUmVwb3NpdG9yeSh0aGlzLCAnQ2xvdWRXYXRjaEFnZW50UmVwbycsIHtcbiAgICAgIHJlcG9zaXRvcnlOYW1lOiAnY2xvdWR3YXRjaC1hZ2VudCcsXG4gICAgICBpbWFnZVNjYW5PblB1c2g6IHRydWUsXG4gICAgICBsaWZlY3ljbGVSdWxlczogW3sgbWF4SW1hZ2VDb3VudDogNSB9XVxuICAgIH0pO1xuICAgIGNvbnN0IGRjZ21FeHBvcnRlclJlcG9zaXRvcnkgPSBuZXcgZWNyLlJlcG9zaXRvcnkodGhpcywgJ0RjZ21FeHBvcnRlclJlcG8nLCB7XG4gICAgICByZXBvc2l0b3J5TmFtZTogJ2RjZ20tZXhwb3J0ZXInLFxuICAgICAgaW1hZ2VTY2FuT25QdXNoOiB0cnVlLFxuICAgICAgbGlmZWN5Y2xlUnVsZXM6IFt7IG1heEltYWdlQ291bnQ6IDUgfV1cbiAgICB9KTtcblxuICAgIC8vIFBhcmFtZXRlcnMgZm9yIHJ1bnRpbWUgY29uZmlndXJhdGlvblxuICAgIGNvbnN0IGFwcEltYWdlVGFnUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXBwSW1hZ2VUYWcnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICd2MC4wLjAnLFxuICAgICAgZGVzY3JpcHRpb246ICdFQ1IgaW1hZ2UgdGFnIGZvciBhcHBsaWNhdGlvbiBjb250YWluZXInXG4gICAgfSk7XG4gICAgY29uc3QgdHJpdG9uSW1hZ2VUYWdQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdUcml0b25JbWFnZVRhZycsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3YwLjAuMCcsXG4gICAgICBkZXNjcmlwdGlvbjogJ0VDUiBpbWFnZSB0YWcgZm9yIFRyaXRvbiBjb250YWluZXInXG4gICAgfSk7XG4gICAgY29uc3QgY3dBZ2VudEltYWdlVGFnUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQ3dBZ2VudEltYWdlVGFnJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnbGF0ZXN0JyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRUNSIGltYWdlIHRhZyBmb3IgQ2xvdWRXYXRjaCBBZ2VudCBjb250YWluZXInXG4gICAgfSk7XG4gICAgY29uc3QgZGNnbUltYWdlVGFnUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnRGNnbUltYWdlVGFnJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnbGF0ZXN0JyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRUNSIGltYWdlIHRhZyBmb3IgRENHTSBleHBvcnRlciBjb250YWluZXInXG4gICAgfSk7XG4gICAgY29uc3QgbW9kZWxQYXRoUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnTW9kZWxQYXRoJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnZmFjZWJvb2svd2F2MnZlYzItYmFzZS05NjBoJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnSEYgbW9kZWwgcGF0aCBmb3IgV2F2MlZlYzInXG4gICAgfSk7XG4gICAgY29uc3QgaW5jbHVkZUNvbmZpZGVuY2VQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdJbmNsdWRlQ29uZmlkZW5jZScsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3RydWUnLFxuICAgICAgYWxsb3dlZFZhbHVlczogWyd0cnVlJywgJ2ZhbHNlJ10sXG4gICAgICBkZXNjcmlwdGlvbjogJ1doZXRoZXIgdG8gY29tcHV0ZSBjb25maWRlbmNlIGluIHdvcmtlcidcbiAgICB9KTtcbiAgICBjb25zdCBhdWRpb0RpclBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F1ZGlvRGlyJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnL3RtcC9hdWRpbycsXG4gICAgICBkZXNjcmlwdGlvbjogJ0xvY2FsIGF1ZGlvIHdvcmtpbmcgZGlyZWN0b3J5IGluc2lkZSBjb250YWluZXInXG4gICAgfSk7XG4gICAgY29uc3QgcmVzdWx0c1ByZWZpeFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1Jlc3VsdHNQcmVmaXgnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdyZXN1bHRzLycsXG4gICAgICBkZXNjcmlwdGlvbjogJ1MzIGtleSBwcmVmaXggZm9yIHJlc3VsdHMgd3JpdGVzJ1xuICAgIH0pO1xuXG4gICAgY29uc3QgbmF0R2F0ZXdheUNvdW50UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnTmF0R2F0ZXdheUNvdW50Jywge1xuICAgICAgdHlwZTogJ051bWJlcicsXG4gICAgICBkZWZhdWx0OiAyLFxuICAgICAgZGVzY3JpcHRpb246ICdOdW1iZXIgb2YgTkFUIEdhdGV3YXlzIHRvIGNyZWF0ZSAoc2V0IDAgdG8gc2F2ZSBjb3N0IHdpdGggVlBDIGVuZHBvaW50cyknXG4gICAgfSk7XG5cbiAgICBjb25zdCBhdGhlbmFEYlBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYURhdGFiYXNlJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnZGVmYXVsdCcsXG4gICAgICBkZXNjcmlwdGlvbjogJ0F0aGVuYSBkYXRhYmFzZSBuYW1lJ1xuICAgIH0pO1xuICAgIGNvbnN0IGF0aGVuYU91dHB1dFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYU91dHB1dCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3MzOi8vYXRoZW5hLXF1ZXJ5LXJlc3VsdHMvJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnQXRoZW5hIHF1ZXJ5IG91dHB1dCBTMyBwYXRoJ1xuICAgIH0pO1xuICAgIGNvbnN0IGF0aGVuYVF1ZXJ5UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXRoZW5hUXVlcnknLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdTRUxFQ1QgYWN0aXZpdHlfaWQgRlJPTSBhY3Rpdml0aWVzIFdIRVJFIHByb2Nlc3NfZmxhZyA9IDEnLFxuICAgICAgZGVzY3JpcHRpb246ICdBdGhlbmEgU1FMIHRvIHByb2R1Y2UgYWN0aXZpdHkgSURzJ1xuICAgIH0pO1xuICAgIGNvbnN0IGF0aGVuYVRhYmxlUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXRoZW5hVGFibGUnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCB0YWJsZSBuYW1lIGZvciBkeW5hbWljIHF1ZXJ5IGJ1aWxkaW5nJ1xuICAgIH0pO1xuICAgIGNvbnN0IGF0aGVuYVdoZXJlUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXRoZW5hV2hlcmUnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBXSEVSRSBjbGF1c2UgKHdpdGhvdXQgV0hFUkUga2V5d29yZCknXG4gICAgfSk7XG4gICAgY29uc3QgYXRoZW5hTGltaXRQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFMaW1pdCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIExJTUlUIHZhbHVlIGZvciB0aGUgcXVlcnknXG4gICAgfSk7XG4gICAgY29uc3QgYXRoZW5hQ29sdW1uc1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYUNvbHVtbnMnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdhY3Rpdml0eV9pZCcsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIGNvbHVtbnMgdG8gc2VsZWN0IGZvciBkeW5hbWljIHF1ZXJ5J1xuICAgIH0pO1xuXG4gICAgLy8gVHJpdG9uIGluZmVyZW5jZSBmZWF0dXJlIGZsYWdcbiAgICBjb25zdCB1c2VUcml0b25QYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdVc2VUcml0b24nLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdmYWxzZScsXG4gICAgICBhbGxvd2VkVmFsdWVzOiBbJ3RydWUnLCAnZmFsc2UnXSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnV2hldGhlciB0byBlbmFibGUgVHJpdG9uIGluZmVyZW5jZSBzZXJ2ZXIgd2l0aCBHUFUgcmVzb3VyY2VzJ1xuICAgIH0pO1xuXG4gICAgY29uc3QgdHJpdG9uQ2VydEFyblBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1RyaXRvbkNlcnRpZmljYXRlQXJuJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnQUNNIGNlcnRpZmljYXRlIEFSTiBmb3IgSFRUUFMgb24gdGhlIFRyaXRvbiBBTEIgKHJlcXVpcmVkIGZvciBUTFMpJ1xuICAgIH0pO1xuXG4gICAgLy8gT3B0aW9uYWwgVExTIGNlcnQva2V5IGZvciB0YXJnZXQgc2lkZWNhciB2aWEgU2VjcmV0cyBNYW5hZ2VyIChleHBlY3RzIEpTT04gd2l0aCBmaWVsZHM6IGNlcnQsIGtleSlcbiAgICBjb25zdCB0cml0b25UYXJnZXRDZXJ0U2VjcmV0QXJuUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnVHJpdG9uVGFyZ2V0Q2VydFNlY3JldEFybicsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIFNlY3JldHMgTWFuYWdlciBBUk4gY29udGFpbmluZyBKU09OIHdpdGggZmllbGRzIHtcImNlcnRcIixcImtleVwifSBmb3Igc2lkZWNhciBUTFMnXG4gICAgfSk7XG5cbiAgICAvLyBUTFMgdHVuaW5nIGZvciBzaWRlY2FyXG4gICAgY29uc3QgZW5hYmxlVGFyZ2V0SHR0cDJQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdFbmFibGVUYXJnZXRIdHRwMicsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3RydWUnLFxuICAgICAgYWxsb3dlZFZhbHVlczogWyd0cnVlJywgJ2ZhbHNlJ10sXG4gICAgICBkZXNjcmlwdGlvbjogJ0VuYWJsZSBIVFRQLzIgb24gVExTIHNpZGVjYXIgbGlzdGVuZXInXG4gICAgfSk7XG4gICAgY29uc3QgdGFyZ2V0U3NsQ2lwaGVyc1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1RhcmdldFNzbENpcGhlcnMnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBPcGVuU1NMIGNpcGhlciBzdWl0ZSBzdHJpbmcgZm9yIFRMUyBzaWRlY2FyIChlbXB0eSBmb3IgZGVmYXVsdCknXG4gICAgfSk7XG5cbiAgICAvLyBPcHRpb25hbCBBdWRpbyBidWNrZXQgZm9yIHJlYWQtb25seSBhY2Nlc3NcbiAgICBjb25zdCBhdWRpb0J1Y2tldE5hbWVQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdWRpb0J1Y2tldE5hbWUnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBTMyBidWNrZXQgbmFtZSBmb3IgaW5wdXQgYXVkaW8gKHJlYWQtb25seSkuIExlYXZlIGJsYW5rIHRvIHNraXAuJ1xuICAgIH0pO1xuXG4gICAgLy8gVHJpdG9uIFVSTCBwYXJhbWV0ZXIgZm9yIHJlbW90ZSBpbmZlcmVuY2UgKHdoZW4gVXNlVHJpdG9uPWZhbHNlIGJ1dCB3YW50IHRvIGNhbGwgZXh0ZXJuYWwgVHJpdG9uKVxuICAgIGNvbnN0IHRyaXRvblNlcnZlclVybFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1RyaXRvblNlcnZlclVybCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIGV4dGVybmFsIFRyaXRvbiBzZXJ2ZXIgVVJMIGZvciByZW1vdGUgaW5mZXJlbmNlLiBMZWF2ZSBibGFuayBmb3IgbG9jYWwgc2lkZWNhci4nXG4gICAgfSk7XG4gICAgY29uc3QgYXVkaW9CdWNrZXRQcmVmaXhQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdWRpb1ByZWZpeCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIFMzIGtleSBwcmVmaXggd2l0aGluIHRoZSBhdWRpbyBidWNrZXQuJ1xuICAgIH0pO1xuXG4gICAgLy8gVlBDIGZvciB0aGUgRUNTIGNsdXN0ZXJcbiAgICBjb25zdCB2cGMgPSBuZXcgZWMyLlZwYyh0aGlzLCAnQW1pcmFMZXR0ZXJTY29yaW5nVnBjJywge1xuICAgICAgbWF4QXpzOiAyLFxuICAgICAgbmF0R2F0ZXdheXM6IG5hdEdhdGV3YXlDb3VudFBhcmFtLnZhbHVlQXNOdW1iZXIsXG4gICAgICBzdWJuZXRDb25maWd1cmF0aW9uOiBbXG4gICAgICAgIHtcbiAgICAgICAgICBjaWRyTWFzazogMjQsXG4gICAgICAgICAgbmFtZTogJ1B1YmxpYycsXG4gICAgICAgICAgc3VibmV0VHlwZTogZWMyLlN1Ym5ldFR5cGUuUFVCTElDLFxuICAgICAgICB9LFxuICAgICAgICB7XG4gICAgICAgICAgY2lkck1hc2s6IDI0LFxuICAgICAgICAgIG5hbWU6ICdQcml2YXRlJyxcbiAgICAgICAgICBzdWJuZXRUeXBlOiBlYzIuU3VibmV0VHlwZS5QUklWQVRFX1dJVEhfRUdSRVNTLFxuICAgICAgICB9XG4gICAgICBdXG4gICAgfSk7XG5cbiAgICAvLyBUb2dnbGUgZm9yIEludGVyZmFjZSBWUEMgRW5kcG9pbnRzIChjYW4gYmUgZGlzYWJsZWQgdG8gcmVkdWNlIGNvc3RzKVxuICAgIGNvbnN0IGVuYWJsZUludGVyZmFjZUVuZHBvaW50c1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0VuYWJsZUludGVyZmFjZUVuZHBvaW50cycsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3RydWUnLFxuICAgICAgYWxsb3dlZFZhbHVlczogWyd0cnVlJywgJ2ZhbHNlJ10sXG4gICAgICBkZXNjcmlwdGlvbjogJ0VuYWJsZSBjcmVhdGlvbiBvZiBJbnRlcmZhY2UgVlBDIEVuZHBvaW50cyAoRUNSLCBDVyBMb2dzLCBTUVMsIFNTTSwgU1RTLCBTZWNyZXRzLCBLTVMpJ1xuICAgIH0pO1xuICAgIGNvbnN0IGVuZHBvaW50c0VuYWJsZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnSW50ZXJmYWNlRW5kcG9pbnRzRW5hYmxlZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25FcXVhbHMoZW5hYmxlSW50ZXJmYWNlRW5kcG9pbnRzUGFyYW0udmFsdWVBc1N0cmluZywgJ3RydWUnKVxuICAgIH0pO1xuXG4gICAgLy8gVlBDIEVuZHBvaW50cyB0byByZWR1Y2UgTkFUIGVncmVzc1xuICAgIHZwYy5hZGRHYXRld2F5RW5kcG9pbnQoJ1MzRW5kcG9pbnQnLCB7XG4gICAgICBzZXJ2aWNlOiBlYzIuR2F0ZXdheVZwY0VuZHBvaW50QXdzU2VydmljZS5TMyxcbiAgICAgIHN1Ym5ldHM6IFt7IHN1Ym5ldHM6IHZwYy5wcml2YXRlU3VibmV0cyB9XVxuICAgIH0pO1xuICAgIGNvbnN0IGVjckFwaUVwID0gdnBjLmFkZEludGVyZmFjZUVuZHBvaW50KCdFY3JBcGlFbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5JbnRlcmZhY2VWcGNFbmRwb2ludEF3c1NlcnZpY2UuRUNSLFxuICAgICAgc3VibmV0czogeyBzdWJuZXRzOiB2cGMucHJpdmF0ZVN1Ym5ldHMgfVxuICAgIH0pO1xuICAgIGNvbnN0IGVjckRvY2tlckVwID0gdnBjLmFkZEludGVyZmFjZUVuZHBvaW50KCdFY3JEb2NrZXJFbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5JbnRlcmZhY2VWcGNFbmRwb2ludEF3c1NlcnZpY2UuRUNSX0RPQ0tFUixcbiAgICAgIHN1Ym5ldHM6IHsgc3VibmV0czogdnBjLnByaXZhdGVTdWJuZXRzIH1cbiAgICB9KTtcbiAgICBjb25zdCBjd0xvZ3NFcCA9IHZwYy5hZGRJbnRlcmZhY2VFbmRwb2ludCgnQ2xvdWRXYXRjaExvZ3NFbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5JbnRlcmZhY2VWcGNFbmRwb2ludEF3c1NlcnZpY2UuQ0xPVURXQVRDSF9MT0dTLFxuICAgICAgc3VibmV0czogeyBzdWJuZXRzOiB2cGMucHJpdmF0ZVN1Ym5ldHMgfVxuICAgIH0pO1xuICAgIGNvbnN0IHNxc0VwID0gdnBjLmFkZEludGVyZmFjZUVuZHBvaW50KCdTcXNFbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5JbnRlcmZhY2VWcGNFbmRwb2ludEF3c1NlcnZpY2UuU1FTLFxuICAgICAgc3VibmV0czogeyBzdWJuZXRzOiB2cGMucHJpdmF0ZVN1Ym5ldHMgfVxuICAgIH0pO1xuICAgIGNvbnN0IHNzbUVwID0gdnBjLmFkZEludGVyZmFjZUVuZHBvaW50KCdTc21FbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5JbnRlcmZhY2VWcGNFbmRwb2ludEF3c1NlcnZpY2UuU1NNLFxuICAgICAgc3VibmV0czogeyBzdWJuZXRzOiB2cGMucHJpdmF0ZVN1Ym5ldHMgfVxuICAgIH0pO1xuICAgIGNvbnN0IHNzbU1zZ3NFcCA9IHZwYy5hZGRJbnRlcmZhY2VFbmRwb2ludCgnU3NtTWVzc2FnZXNFbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5JbnRlcmZhY2VWcGNFbmRwb2ludEF3c1NlcnZpY2UuU1NNX01FU1NBR0VTLFxuICAgICAgc3VibmV0czogeyBzdWJuZXRzOiB2cGMucHJpdmF0ZVN1Ym5ldHMgfVxuICAgIH0pO1xuICAgIGNvbnN0IGVjMk1zZ3NFcCA9IHZwYy5hZGRJbnRlcmZhY2VFbmRwb2ludCgnRWMyTWVzc2FnZXNFbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5JbnRlcmZhY2VWcGNFbmRwb2ludEF3c1NlcnZpY2UuRUMyX01FU1NBR0VTLFxuICAgICAgc3VibmV0czogeyBzdWJuZXRzOiB2cGMucHJpdmF0ZVN1Ym5ldHMgfVxuICAgIH0pO1xuICAgIGNvbnN0IHN0c0VwID0gdnBjLmFkZEludGVyZmFjZUVuZHBvaW50KCdTdHNFbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5JbnRlcmZhY2VWcGNFbmRwb2ludEF3c1NlcnZpY2UuU1RTLFxuICAgICAgc3VibmV0czogeyBzdWJuZXRzOiB2cGMucHJpdmF0ZVN1Ym5ldHMgfVxuICAgIH0pO1xuICAgIGNvbnN0IHNlY3JldHNFcCA9IHZwYy5hZGRJbnRlcmZhY2VFbmRwb2ludCgnU2VjcmV0c01hbmFnZXJFbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5JbnRlcmZhY2VWcGNFbmRwb2ludEF3c1NlcnZpY2UuU0VDUkVUU19NQU5BR0VSLFxuICAgICAgc3VibmV0czogeyBzdWJuZXRzOiB2cGMucHJpdmF0ZVN1Ym5ldHMgfVxuICAgIH0pO1xuICAgIGNvbnN0IGttc0VwID0gdnBjLmFkZEludGVyZmFjZUVuZHBvaW50KCdLbXNFbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5JbnRlcmZhY2VWcGNFbmRwb2ludEF3c1NlcnZpY2UuS01TLFxuICAgICAgc3VibmV0czogeyBzdWJuZXRzOiB2cGMucHJpdmF0ZVN1Ym5ldHMgfVxuICAgIH0pO1xuXG4gICAgKGVjckFwaUVwLm5vZGUuZGVmYXVsdENoaWxkIGFzIGVjMi5DZm5WUENFbmRwb2ludCkuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBlbmRwb2ludHNFbmFibGVkO1xuICAgIChlY3JEb2NrZXJFcC5ub2RlLmRlZmF1bHRDaGlsZCBhcyBlYzIuQ2ZuVlBDRW5kcG9pbnQpLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gZW5kcG9pbnRzRW5hYmxlZDtcbiAgICAoY3dMb2dzRXAubm9kZS5kZWZhdWx0Q2hpbGQgYXMgZWMyLkNmblZQQ0VuZHBvaW50KS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGVuZHBvaW50c0VuYWJsZWQ7XG4gICAgKHNxc0VwLm5vZGUuZGVmYXVsdENoaWxkIGFzIGVjMi5DZm5WUENFbmRwb2ludCkuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBlbmRwb2ludHNFbmFibGVkO1xuICAgIChzc21FcC5ub2RlLmRlZmF1bHRDaGlsZCBhcyBlYzIuQ2ZuVlBDRW5kcG9pbnQpLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gZW5kcG9pbnRzRW5hYmxlZDtcbiAgICAoc3NtTXNnc0VwLm5vZGUuZGVmYXVsdENoaWxkIGFzIGVjMi5DZm5WUENFbmRwb2ludCkuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBlbmRwb2ludHNFbmFibGVkO1xuICAgIChlYzJNc2dzRXAubm9kZS5kZWZhdWx0Q2hpbGQgYXMgZWMyLkNmblZQQ0VuZHBvaW50KS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGVuZHBvaW50c0VuYWJsZWQ7XG4gICAgKHN0c0VwLm5vZGUuZGVmYXVsdENoaWxkIGFzIGVjMi5DZm5WUENFbmRwb2ludCkuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBlbmRwb2ludHNFbmFibGVkO1xuICAgIChzZWNyZXRzRXAubm9kZS5kZWZhdWx0Q2hpbGQgYXMgZWMyLkNmblZQQ0VuZHBvaW50KS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGVuZHBvaW50c0VuYWJsZWQ7XG4gICAgKGttc0VwLm5vZGUuZGVmYXVsdENoaWxkIGFzIGVjMi5DZm5WUENFbmRwb2ludCkuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBlbmRwb2ludHNFbmFibGVkO1xuXG4gICAgLy8gU2VjdXJpdHkgZ3JvdXBzIHNwbGl0OiBBTEIgYW5kIEVDUyB0YXNrc1xuICAgIGNvbnN0IGFsYlNlY3VyaXR5R3JvdXAgPSBuZXcgZWMyLlNlY3VyaXR5R3JvdXAodGhpcywgJ0FtaXJhQWxiU2VjdXJpdHlHcm91cCcsIHtcbiAgICAgIHZwYyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnU2VjdXJpdHkgZ3JvdXAgZm9yIGludGVybmFsIEFMQiBmcm9udGluZyBUcml0b24gVExTIHByb3h5JyxcbiAgICAgIGFsbG93QWxsT3V0Ym91bmQ6IHRydWVcbiAgICB9KTtcbiAgICAvLyBSZXN0cmljdCBBTEIgaW5ncmVzczogYWxsb3cgZWl0aGVyIGZyb20gYSBzcGVjaWZpYyBjbGllbnQgU0csIG9yIGZhbGxiYWNrIHRvIFZQQyBDSURSXG4gICAgY29uc3QgYWxiQ2xpZW50U2dQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBbGJDbGllbnRTZWN1cml0eUdyb3VwSWQnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBTZWN1cml0eSBHcm91cCBJRCBhbGxvd2VkIHRvIGFjY2VzcyBBTEI6NDQzLiBMZWF2ZSBibGFuayB0byBkZWZhdWx0IHRvIFZQQyBDSURSLidcbiAgICB9KTtcbiAgICBjb25zdCBjbGllbnRTZ1Byb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ0FsYkNsaWVudFNnUHJvdmlkZWQnLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uTm90KGNkay5Gbi5jb25kaXRpb25FcXVhbHMoYWxiQ2xpZW50U2dQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpXG4gICAgfSk7XG4gICAgY29uc3QgaW5ncmVzc0Zyb21DbGllbnRTZyA9IG5ldyBlYzIuQ2ZuU2VjdXJpdHlHcm91cEluZ3Jlc3ModGhpcywgJ0FsYkluZ3Jlc3NGcm9tQ2xpZW50U2cnLCB7XG4gICAgICBncm91cElkOiBhbGJTZWN1cml0eUdyb3VwLnNlY3VyaXR5R3JvdXBJZCxcbiAgICAgIGlwUHJvdG9jb2w6ICd0Y3AnLFxuICAgICAgZnJvbVBvcnQ6IDQ0MyxcbiAgICAgIHRvUG9ydDogNDQzLFxuICAgICAgc291cmNlU2VjdXJpdHlHcm91cElkOiBhbGJDbGllbnRTZ1BhcmFtLnZhbHVlQXNTdHJpbmdcbiAgICB9KTtcbiAgICBpbmdyZXNzRnJvbUNsaWVudFNnLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gY2xpZW50U2dQcm92aWRlZDtcbiAgICBjb25zdCBpbmdyZXNzRnJvbVZwY0NpZHIgPSBuZXcgZWMyLkNmblNlY3VyaXR5R3JvdXBJbmdyZXNzKHRoaXMsICdBbGJJbmdyZXNzRnJvbVZwY0NpZHInLCB7XG4gICAgICBncm91cElkOiBhbGJTZWN1cml0eUdyb3VwLnNlY3VyaXR5R3JvdXBJZCxcbiAgICAgIGlwUHJvdG9jb2w6ICd0Y3AnLFxuICAgICAgZnJvbVBvcnQ6IDQ0MyxcbiAgICAgIHRvUG9ydDogNDQzLFxuICAgICAgY2lkcklwOiB2cGMudnBjQ2lkckJsb2NrXG4gICAgfSk7XG4gICAgaW5ncmVzc0Zyb21WcGNDaWRyLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ0FsYkNsaWVudFNnTm90UHJvdmlkZWQnLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGFsYkNsaWVudFNnUGFyYW0udmFsdWVBc1N0cmluZywgJycpXG4gICAgfSk7XG5cbiAgICBjb25zdCBlY3NTZWN1cml0eUdyb3VwID0gbmV3IGVjMi5TZWN1cml0eUdyb3VwKHRoaXMsICdBbWlyYUVjc1NlY3VyaXR5R3JvdXAnLCB7XG4gICAgICB2cGMsXG4gICAgICBkZXNjcmlwdGlvbjogJ1NlY3VyaXR5IGdyb3VwIGZvciBBbWlyYSBMZXR0ZXIgU2NvcmluZyBFQ1MgdGFza3MnLFxuICAgICAgYWxsb3dBbGxPdXRib3VuZDogdHJ1ZVxuICAgIH0pO1xuICAgIGVjc1NlY3VyaXR5R3JvdXAuYWRkSW5ncmVzc1J1bGUoYWxiU2VjdXJpdHlHcm91cCwgZWMyLlBvcnQudGNwKDg0NDMpLCAnQWxsb3cgQUxCIHRvIHJlYWNoIFRMUyBwcm94eSBvdmVyIDg0NDMnKTtcblxuICAgIC8vIEVDUyBDbHVzdGVyIHdpdGggR1BVIGluc3RhbmNlc1xuICAgIGNvbnN0IGNsdXN0ZXIgPSBuZXcgZWNzLkNsdXN0ZXIodGhpcywgJ0FtaXJhTGV0dGVyU2NvcmluZ0NsdXN0ZXInLCB7XG4gICAgICB2cGMsXG4gICAgICBjbHVzdGVyTmFtZTogJ2FtaXJhLWxldHRlci1zY29yaW5nLWNsdXN0ZXInLFxuICAgICAgY29udGFpbmVySW5zaWdodHM6IHRydWVcbiAgICB9KTtcblxuICAgIC8vIEVDMiBpbnN0YW5jZSByb2xlIGZvciBFQ1MgY2x1c3RlciBpbnN0YW5jZXNcbiAgICBjb25zdCBpbnN0YW5jZVJvbGUgPSBuZXcgaWFtLlJvbGUodGhpcywgJ0dwdUluc3RhbmNlUm9sZScsIHtcbiAgICAgIGFzc3VtZWRCeTogbmV3IGlhbS5TZXJ2aWNlUHJpbmNpcGFsKCdlYzIuYW1hem9uYXdzLmNvbScpXG4gICAgfSk7XG4gICAgaW5zdGFuY2VSb2xlLmFkZE1hbmFnZWRQb2xpY3koaWFtLk1hbmFnZWRQb2xpY3kuZnJvbUF3c01hbmFnZWRQb2xpY3lOYW1lKCdzZXJ2aWNlLXJvbGUvQW1hem9uRUMyQ29udGFpbmVyU2VydmljZWZvckVDMlJvbGUnKSk7XG4gICAgaW5zdGFuY2VSb2xlLmFkZE1hbmFnZWRQb2xpY3koaWFtLk1hbmFnZWRQb2xpY3kuZnJvbUF3c01hbmFnZWRQb2xpY3lOYW1lKCdBbWF6b25TU01NYW5hZ2VkSW5zdGFuY2VDb3JlJykpO1xuXG4gICAgLy8gTGF1bmNoIHRlbXBsYXRlIGZvciBHUFUgaW5zdGFuY2VzIChBMTBHKVxuICAgIGNvbnN0IGxhdW5jaFRlbXBsYXRlID0gbmV3IGVjMi5MYXVuY2hUZW1wbGF0ZSh0aGlzLCAnR3B1TGF1bmNoVGVtcGxhdGUnLCB7XG4gICAgICBpbnN0YW5jZVR5cGU6IGVjMi5JbnN0YW5jZVR5cGUub2YoZWMyLkluc3RhbmNlQ2xhc3MuRzUsIGVjMi5JbnN0YW5jZVNpemUuWExBUkdFNCksIC8vIGc1LjR4bGFyZ2UgaGFzIEExMEcgR1BVXG4gICAgICBtYWNoaW5lSW1hZ2U6IGVjcy5FY3NPcHRpbWl6ZWRJbWFnZS5hbWF6b25MaW51eDIoZWNzLkFtaUhhcmR3YXJlVHlwZS5HUFUpLFxuICAgICAgdXNlckRhdGE6IGVjMi5Vc2VyRGF0YS5mb3JMaW51eCgpLFxuICAgICAgc2VjdXJpdHlHcm91cDogZWNzU2VjdXJpdHlHcm91cCxcbiAgICAgIHJvbGU6IGluc3RhbmNlUm9sZSxcbiAgICAgIHJlcXVpcmVJbWRzdjI6IHRydWUsXG4gICAgICBzcG90T3B0aW9uczoge1xuICAgICAgICByZXF1ZXN0VHlwZTogZWMyLlNwb3RSZXF1ZXN0VHlwZS5PTkVfVElNRSxcbiAgICAgICAgaW50ZXJydXB0aW9uQmVoYXZpb3I6IGVjMi5TcG90SW5zdGFuY2VJbnRlcnJ1cHRpb24uU1RPUFxuICAgICAgfVxuICAgIH0pO1xuXG4gICAgLy8gQXV0byBTY2FsaW5nIEdyb3VwIGZvciBHUFUgaW5zdGFuY2VzXG4gICAgY29uc3QgYXV0b1NjYWxpbmdHcm91cCA9IG5ldyBhdXRvc2NhbGluZy5BdXRvU2NhbGluZ0dyb3VwKHRoaXMsICdHcHVBdXRvU2NhbGluZ0dyb3VwJywge1xuICAgICAgdnBjLFxuICAgICAgbGF1bmNoVGVtcGxhdGUsXG4gICAgICBtaW5DYXBhY2l0eTogMCxcbiAgICAgIG1heENhcGFjaXR5OiAxMCxcbiAgICAgIGRlc2lyZWRDYXBhY2l0eTogMCxcbiAgICAgIHZwY1N1Ym5ldHM6IHtcbiAgICAgICAgc3VibmV0VHlwZTogZWMyLlN1Ym5ldFR5cGUuUFJJVkFURV9XSVRIX0VHUkVTU1xuICAgICAgfSxcbiAgICAgIGNhcGFjaXR5UmViYWxhbmNlOiB0cnVlXG4gICAgfSk7XG5cbiAgICAvLyBBZGQgY2FwYWNpdHkgcHJvdmlkZXIgdG8gY2x1c3RlclxuICAgIGNvbnN0IGNhcGFjaXR5UHJvdmlkZXIgPSBuZXcgZWNzLkFzZ0NhcGFjaXR5UHJvdmlkZXIodGhpcywgJ0dwdUNhcGFjaXR5UHJvdmlkZXInLCB7XG4gICAgICBhdXRvU2NhbGluZ0dyb3VwLFxuICAgICAgZW5hYmxlTWFuYWdlZFNjYWxpbmc6IHRydWUsXG4gICAgICBlbmFibGVNYW5hZ2VkVGVybWluYXRpb25Qcm90ZWN0aW9uOiB0cnVlLFxuICAgICAgdGFyZ2V0Q2FwYWNpdHlQZXJjZW50OiAxMDAsXG4gICAgICBtYWNoaW5lSW1hZ2VUeXBlOiBlY3MuTWFjaGluZUltYWdlVHlwZS5BTUFaT05fTElOVVhfMlxuICAgIH0pO1xuICAgIC8vIEFkZGl0aW9uYWwgQVNHcyBmb3IgZGl2ZXJzaWZpZWQgU3BvdCBjYXBhY2l0eVxuICAgIGNvbnN0IHsgYXNnOiBhc2dHNXhsYXJnZSwgY2FwYWNpdHlQcm92aWRlcjogY3BHNXhsYXJnZSB9ID0gdGhpcy5jcmVhdGVBc2dBbmRDYXBhY2l0eVByb3ZpZGVyKHRoaXMsICdHcHVHNXhsYXJnZScsIHZwYywgZWMyLkluc3RhbmNlVHlwZS5vZihlYzIuSW5zdGFuY2VDbGFzcy5HNSwgZWMyLkluc3RhbmNlU2l6ZS5YTEFSR0UpLCBlY3NTZWN1cml0eUdyb3VwLCBpbnN0YW5jZVJvbGUpO1xuICAgIGNsdXN0ZXIuYWRkQXNnQ2FwYWNpdHlQcm92aWRlcihjcEc1eGxhcmdlKTtcblxuICAgIGNvbnN0IHsgYXNnOiBhc2dHNTJ4bGFyZ2UsIGNhcGFjaXR5UHJvdmlkZXI6IGNwRzUyeGxhcmdlIH0gPSB0aGlzLmNyZWF0ZUFzZ0FuZENhcGFjaXR5UHJvdmlkZXIodGhpcywgJ0dwdUc1MnhsYXJnZScsIHZwYywgZWMyLkluc3RhbmNlVHlwZS5vZihlYzIuSW5zdGFuY2VDbGFzcy5HNSwgZWMyLkluc3RhbmNlU2l6ZS5YTEFSR0UyKSwgZWNzU2VjdXJpdHlHcm91cCwgaW5zdGFuY2VSb2xlKTtcbiAgICBjbHVzdGVyLmFkZEFzZ0NhcGFjaXR5UHJvdmlkZXIoY3BHNTJ4bGFyZ2UpO1xuXG4gICAgY2x1c3Rlci5hZGRBc2dDYXBhY2l0eVByb3ZpZGVyKGNhcGFjaXR5UHJvdmlkZXIpO1xuXG4gICAgLy8gUzMgYWNjZXNzIGxvZ3MgYnVja2V0XG4gICAgY29uc3QgYWNjZXNzTG9nc0J1Y2tldCA9IG5ldyBzMy5CdWNrZXQodGhpcywgJ0FjY2Vzc0xvZ3NCdWNrZXQnLCB7XG4gICAgICB2ZXJzaW9uZWQ6IGZhbHNlLFxuICAgICAgYmxvY2tQdWJsaWNBY2Nlc3M6IHMzLkJsb2NrUHVibGljQWNjZXNzLkJMT0NLX0FMTCxcbiAgICAgIGVuY3J5cHRpb246IHMzLkJ1Y2tldEVuY3J5cHRpb24uUzNfTUFOQUdFRCxcbiAgICAgIGVuZm9yY2VTU0w6IHRydWUsXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5SRVRBSU5cbiAgICB9KTtcblxuICAgIC8vIEtNUyBrZXkgZm9yIHJlc3VsdHMgYnVja2V0XG4gICAgY29uc3QgcmVzdWx0c0J1Y2tldEtleSA9IG5ldyBrbXMuS2V5KHRoaXMsICdSZXN1bHRzQnVja2V0S2V5Jywge1xuICAgICAgZW5hYmxlS2V5Um90YXRpb246IHRydWUsXG4gICAgICBhbGlhczogJ2FsaWFzL2FtaXJhLWxldHRlci1zY29yaW5nLXJlc3VsdHMnXG4gICAgfSk7XG5cbiAgICAvLyBSZXN1bHRzIGJ1Y2tldCAoc291cmNlIG9mIHRydXRoKSB3aXRoIFNTRS1LTVMsIGJ1Y2tldCBrZXksIGFjY2VzcyBsb2dzLCBhbmQgbGlmZWN5Y2xlXG4gICAgY29uc3QgcmVzdWx0c0J1Y2tldCA9IG5ldyBzMy5CdWNrZXQodGhpcywgJ1Jlc3VsdHNCdWNrZXQnLCB7XG4gICAgICB2ZXJzaW9uZWQ6IHRydWUsXG4gICAgICBibG9ja1B1YmxpY0FjY2VzczogczMuQmxvY2tQdWJsaWNBY2Nlc3MuQkxPQ0tfQUxMLFxuICAgICAgZW5jcnlwdGlvbjogczMuQnVja2V0RW5jcnlwdGlvbi5LTVMsXG4gICAgICBlbmNyeXB0aW9uS2V5OiByZXN1bHRzQnVja2V0S2V5LFxuICAgICAgYnVja2V0S2V5RW5hYmxlZDogdHJ1ZSxcbiAgICAgIHNlcnZlckFjY2Vzc0xvZ3NCdWNrZXQ6IGFjY2Vzc0xvZ3NCdWNrZXQsXG4gICAgICBzZXJ2ZXJBY2Nlc3NMb2dzUHJlZml4OiAnczMtYWNjZXNzLWxvZ3MvJyxcbiAgICAgIGxpZmVjeWNsZVJ1bGVzOiBbXG4gICAgICAgIHtcbiAgICAgICAgICBpZDogJ0ludGVsbGlnZW50VGllcmluZ05vdycsXG4gICAgICAgICAgZW5hYmxlZDogdHJ1ZSxcbiAgICAgICAgICB0cmFuc2l0aW9uczogW3sgc3RvcmFnZUNsYXNzOiBzMy5TdG9yYWdlQ2xhc3MuSU5URUxMSUdFTlRfVElFUklORywgdHJhbnNpdGlvbkFmdGVyOiBjZGsuRHVyYXRpb24uZGF5cygwKSB9XVxuICAgICAgICB9XG4gICAgICBdLFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuUkVUQUlOLFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZVxuICAgIH0pO1xuICAgIHJlc3VsdHNCdWNrZXQuYWRkVG9SZXNvdXJjZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBzaWQ6ICdEZW55SW5zZWN1cmVUcmFuc3BvcnQnLFxuICAgICAgZWZmZWN0OiBpYW0uRWZmZWN0LkRFTlksXG4gICAgICBwcmluY2lwYWxzOiBbbmV3IGlhbS5BbnlQcmluY2lwYWwoKV0sXG4gICAgICBhY3Rpb25zOiBbJ3MzOkdldE9iamVjdCcsICdzMzpQdXRPYmplY3QnLCAnczM6TGlzdEJ1Y2tldCcsICdzMzpEZWxldGVPYmplY3QnLCAnczM6RGVsZXRlT2JqZWN0VmVyc2lvbiddLFxuICAgICAgcmVzb3VyY2VzOiBbcmVzdWx0c0J1Y2tldC5idWNrZXRBcm4sIGAke3Jlc3VsdHNCdWNrZXQuYnVja2V0QXJufS8qYF0sXG4gICAgICBjb25kaXRpb25zOiB7IEJvb2w6IHsgJ2F3czpTZWN1cmVUcmFuc3BvcnQnOiAnZmFsc2UnIH0gfVxuICAgIH0pKTtcbiAgICByZXN1bHRzQnVja2V0LmFkZFRvUmVzb3VyY2VQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgc2lkOiAnRGVueVVuRW5jcnlwdGVkT2JqZWN0VXBsb2FkcycsXG4gICAgICBlZmZlY3Q6IGlhbS5FZmZlY3QuREVOWSxcbiAgICAgIHByaW5jaXBhbHM6IFtuZXcgaWFtLkFueVByaW5jaXBhbCgpXSxcbiAgICAgIGFjdGlvbnM6IFsnczM6UHV0T2JqZWN0J10sXG4gICAgICByZXNvdXJjZXM6IFtgJHtyZXN1bHRzQnVja2V0LmJ1Y2tldEFybn0vKmBdLFxuICAgICAgY29uZGl0aW9uczogeyBTdHJpbmdOb3RFcXVhbHM6IHsgJ3MzOngtYW16LXNlcnZlci1zaWRlLWVuY3J5cHRpb24nOiAnYXdzOmttcycgfSB9XG4gICAgfSkpO1xuXG4gICAgLy8gR3JhbnQgd2lsbCBiZSBhdHRhY2hlZCBhZnRlciB0YXNrUm9sZSBpcyBkZWZpbmVkXG5cbiAgICAvLyBTUVMgcXVldWUgZm9yIGpvYnMgd2l0aCBETFFcbiAgICBjb25zdCBkbHEgPSBuZXcgc3FzLlF1ZXVlKHRoaXMsICdKb2JzRExRJywge1xuICAgICAgcmV0ZW50aW9uUGVyaW9kOiBjZGsuRHVyYXRpb24uZGF5cygxNCksXG4gICAgICBlbmNyeXB0aW9uOiBzcXMuUXVldWVFbmNyeXB0aW9uLktNU19NQU5BR0VELFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZVxuICAgIH0pO1xuICAgIGNvbnN0IGpvYnNRdWV1ZSA9IG5ldyBzcXMuUXVldWUodGhpcywgJ0pvYnNRdWV1ZScsIHtcbiAgICAgIHZpc2liaWxpdHlUaW1lb3V0OiBjZGsuRHVyYXRpb24ubWludXRlcygxNSksXG4gICAgICBkZWFkTGV0dGVyUXVldWU6IHsgcXVldWU6IGRscSwgbWF4UmVjZWl2ZUNvdW50OiAzIH0sXG4gICAgICBlbmNyeXB0aW9uOiBzcXMuUXVldWVFbmNyeXB0aW9uLktNU19NQU5BR0VELFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZVxuICAgIH0pO1xuXG4gICAgLy8gVGFzayBleGVjdXRpb24gcm9sZVxuICAgIGNvbnN0IHRhc2tFeGVjdXRpb25Sb2xlID0gbmV3IGlhbS5Sb2xlKHRoaXMsICdUYXNrRXhlY3V0aW9uUm9sZScsIHtcbiAgICAgIGFzc3VtZWRCeTogbmV3IGlhbS5TZXJ2aWNlUHJpbmNpcGFsKCdlY3MtdGFza3MuYW1hem9uYXdzLmNvbScpLFxuICAgICAgbWFuYWdlZFBvbGljaWVzOiBbXG4gICAgICAgIGlhbS5NYW5hZ2VkUG9saWN5LmZyb21Bd3NNYW5hZ2VkUG9saWN5TmFtZSgnc2VydmljZS1yb2xlL0FtYXpvbkVDU1Rhc2tFeGVjdXRpb25Sb2xlUG9saWN5JylcbiAgICAgIF1cbiAgICB9KTtcblxuICAgIC8vIFRhc2sgcm9sZSB3aXRoIG5lY2Vzc2FyeSBwZXJtaXNzaW9uc1xuICAgIGNvbnN0IHRhc2tSb2xlID0gbmV3IGlhbS5Sb2xlKHRoaXMsICdUYXNrUm9sZScsIHtcbiAgICAgIHJvbGVOYW1lOiBgYW1pcmEtbGV0dGVyLXNjb3JpbmctdGFzay0ke2Nkay5TdGFjay5vZih0aGlzKS5zdGFja05hbWV9YCxcbiAgICAgIGFzc3VtZWRCeTogbmV3IGlhbS5TZXJ2aWNlUHJpbmNpcGFsKCdlY3MtdGFza3MuYW1hem9uYXdzLmNvbScpLFxuICAgICAgaW5saW5lUG9saWNpZXM6IHtcbiAgICAgICAgUzNBY2Nlc3M6IG5ldyBpYW0uUG9saWN5RG9jdW1lbnQoe1xuICAgICAgICAgIHN0YXRlbWVudHM6IFtcbiAgICAgICAgICAgIG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgICAgICAgICAgYWN0aW9uczogWydzMzpMaXN0QnVja2V0J10sXG4gICAgICAgICAgICAgIHJlc291cmNlczogW3Jlc3VsdHNCdWNrZXQuYnVja2V0QXJuXVxuICAgICAgICAgICAgfSksXG4gICAgICAgICAgICBuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICAgICAgICAgIGFjdGlvbnM6IFsnczM6UHV0T2JqZWN0J10sXG4gICAgICAgICAgICAgIHJlc291cmNlczogW2Ake3Jlc3VsdHNCdWNrZXQuYnVja2V0QXJufS8qYF1cbiAgICAgICAgICAgIH0pXG4gICAgICAgICAgXVxuICAgICAgICB9KSxcbiAgICAgICAgU3FzQWNjZXNzOiBuZXcgaWFtLlBvbGljeURvY3VtZW50KHtcbiAgICAgICAgICBzdGF0ZW1lbnRzOiBbXG4gICAgICAgICAgICBuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICAgICAgICAgIGFjdGlvbnM6IFsnc3FzOlJlY2VpdmVNZXNzYWdlJywgJ3NxczpEZWxldGVNZXNzYWdlJywgJ3NxczpHZXRRdWV1ZUF0dHJpYnV0ZXMnXSxcbiAgICAgICAgICAgICAgcmVzb3VyY2VzOiBbam9ic1F1ZXVlLnF1ZXVlQXJuXVxuICAgICAgICAgICAgfSlcbiAgICAgICAgICBdXG4gICAgICAgIH0pXG4gICAgICB9XG4gICAgfSk7XG5cbiAgICAvLyBDb25kaXRpb25zIGZvciBjb25kaXRpb25hbCByZXNvdXJjZXNcbiAgICBjb25zdCBhdWRpb1Byb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ0F1ZGlvQnVja2V0UHJvdmlkZWQnLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uTm90KGNkay5Gbi5jb25kaXRpb25FcXVhbHMoYXVkaW9CdWNrZXROYW1lUGFyYW0udmFsdWVBc1N0cmluZywgJycpKVxuICAgIH0pO1xuICAgIGNvbnN0IHVzZVRyaXRvbkNvbmRpdGlvbiA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdVc2VUcml0b25Db25kaXRpb24nLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHVzZVRyaXRvblBhcmFtLnZhbHVlQXNTdHJpbmcsICd0cnVlJylcbiAgICB9KTtcbiAgICBjb25zdCBhdWRpb1BvbGljeURvYyA9IG5ldyBpYW0uUG9saWN5RG9jdW1lbnQoe1xuICAgICAgc3RhdGVtZW50czogW1xuICAgICAgICBuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICAgICAgYWN0aW9uczogWydzMzpMaXN0QnVja2V0J10sXG4gICAgICAgICAgcmVzb3VyY2VzOiBbY2RrLkFybi5mb3JtYXQoeyBzZXJ2aWNlOiAnczMnLCByZXNvdXJjZTogYXVkaW9CdWNrZXROYW1lUGFyYW0udmFsdWVBc1N0cmluZyB9LCB0aGlzKV0sXG4gICAgICAgICAgY29uZGl0aW9uczoge1xuICAgICAgICAgICAgU3RyaW5nTGlrZTogeyAnczM6cHJlZml4JzogW2F1ZGlvQnVja2V0UHJlZml4UGFyYW0udmFsdWVBc1N0cmluZ10gfVxuICAgICAgICAgIH1cbiAgICAgICAgfSksXG4gICAgICAgIG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgICAgICBhY3Rpb25zOiBbJ3MzOkdldE9iamVjdCddLFxuICAgICAgICAgIHJlc291cmNlczogW2Nkay5Bcm4uZm9ybWF0KHsgc2VydmljZTogJ3MzJywgcmVzb3VyY2U6IGAke2F1ZGlvQnVja2V0TmFtZVBhcmFtLnZhbHVlQXNTdHJpbmd9LyR7YXVkaW9CdWNrZXRQcmVmaXhQYXJhbS52YWx1ZUFzU3RyaW5nfSpgIH0sIHRoaXMpXVxuICAgICAgICB9KVxuICAgICAgXVxuICAgIH0pO1xuICAgIGNvbnN0IGF1ZGlvQ2ZuUG9saWN5ID0gbmV3IGlhbS5DZm5Qb2xpY3kodGhpcywgJ1Rhc2tSb2xlQXVkaW9SZWFkUG9saWN5Jywge1xuICAgICAgcG9saWN5RG9jdW1lbnQ6IGF1ZGlvUG9saWN5RG9jLFxuICAgICAgcm9sZXM6IFt0YXNrUm9sZS5yb2xlTmFtZSFdLFxuICAgICAgcG9saWN5TmFtZTogYFRhc2tSb2xlQXVkaW9SZWFkUG9saWN5LSR7Y2RrLlN0YWNrLm9mKHRoaXMpLnN0YWNrTmFtZX1gXG4gICAgfSk7XG4gICAgYXVkaW9DZm5Qb2xpY3kuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBhdWRpb1Byb3ZpZGVkO1xuXG4gICAgLy8gQWxsb3cgdGFzayByb2xlIHRvIHVzZSB0aGUgS01TIGtleSBmb3IgU1NFLUtNUyBvYmplY3RzXG4gICAgcmVzdWx0c0J1Y2tldEtleS5ncmFudEVuY3J5cHREZWNyeXB0KHRhc2tSb2xlKTtcblxuICAgIC8vIENsb3VkV2F0Y2ggTG9nIEdyb3VwXG4gICAgY29uc3QgbG9nR3JvdXAgPSBuZXcgbG9ncy5Mb2dHcm91cCh0aGlzLCAnQW1pcmFMZXR0ZXJTY29yaW5nTG9nR3JvdXAnLCB7XG4gICAgICBsb2dHcm91cE5hbWU6ICcvZWNzL2FtaXJhLWxldHRlci1zY29yaW5nJyxcbiAgICAgIHJldGVudGlvbjogbG9ncy5SZXRlbnRpb25EYXlzLk9ORV9NT05USCxcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNkay5SZW1vdmFsUG9saWN5LkRFU1RST1ksXG4gICAgICBlbmNyeXB0aW9uS2V5OiByZXN1bHRzQnVja2V0S2V5XG4gICAgfSk7XG5cbiAgICAvLyBWUEMgRmxvdyBMb2dzXG4gICAgY29uc3QgdnBjRmxvd0xvZ0dyb3VwID0gbmV3IGxvZ3MuTG9nR3JvdXAodGhpcywgJ1ZwY0Zsb3dMb2dzJywge1xuICAgICAgcmV0ZW50aW9uOiBsb2dzLlJldGVudGlvbkRheXMuT05FX01PTlRILFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuREVTVFJPWSxcbiAgICAgIGVuY3J5cHRpb25LZXk6IHJlc3VsdHNCdWNrZXRLZXlcbiAgICB9KTtcbiAgICB2cGMuYWRkRmxvd0xvZygnRmxvd0xvZ3NBbGwnLCB7XG4gICAgICBkZXN0aW5hdGlvbjogZWMyLkZsb3dMb2dEZXN0aW5hdGlvbi50b0Nsb3VkV2F0Y2hMb2dzKHZwY0Zsb3dMb2dHcm91cCksXG4gICAgICB0cmFmZmljVHlwZTogZWMyLkZsb3dMb2dUcmFmZmljVHlwZS5BTExcbiAgICB9KTtcblxuICAgIC8vIEVDUyBUYXNrIERlZmluaXRpb25cbiAgICBjb25zdCB0YXNrRGVmaW5pdGlvbiA9IG5ldyBlY3MuRWMyVGFza0RlZmluaXRpb24odGhpcywgJ0FtaXJhTGV0dGVyU2NvcmluZ1Rhc2tEZWYnLCB7XG4gICAgICBmYW1pbHk6IGBhbWlyYS1sZXR0ZXItc2NvcmluZy0ke2Nkay5TdGFjay5vZih0aGlzKS5zdGFja05hbWV9YCxcbiAgICAgIGV4ZWN1dGlvblJvbGU6IHRhc2tFeGVjdXRpb25Sb2xlLFxuICAgICAgdGFza1JvbGUsXG4gICAgICBuZXR3b3JrTW9kZTogZWNzLk5ldHdvcmtNb2RlLkFXU19WUENcbiAgICB9KTtcblxuICAgIC8vIFRyaXRvbiBHUFUgaW5mZXJlbmNlIHNlcnZlciBjb250YWluZXJcbiAgICBjb25zdCB0cml0b25Db250YWluZXIgPSB0YXNrRGVmaW5pdGlvbi5hZGRDb250YWluZXIoJ1RyaXRvblNlcnZlckNvbnRhaW5lcicsIHtcbiAgICAgIGltYWdlOiBlY3MuQ29udGFpbmVySW1hZ2UuZnJvbUVjclJlcG9zaXRvcnkodHJpdG9uUmVwb3NpdG9yeSwgdHJpdG9uSW1hZ2VUYWdQYXJhbS52YWx1ZUFzU3RyaW5nKSxcbiAgICAgIG1lbW9yeVJlc2VydmF0aW9uTWlCOiA0MDk2LFxuICAgICAgY3B1OiAxMDI0LFxuICAgICAgZ3B1Q291bnQ6IDEsXG4gICAgICBsb2dnaW5nOiBlY3MuTG9nRHJpdmVyLmF3c0xvZ3MoeyBsb2dHcm91cCwgc3RyZWFtUHJlZml4OiAndHJpdG9uLXNlcnZlcicgfSksXG4gICAgICBwb3J0TWFwcGluZ3M6IFt7IGNvbnRhaW5lclBvcnQ6IDgwMDAgfSwgeyBjb250YWluZXJQb3J0OiA4MDAxIH0sIHsgY29udGFpbmVyUG9ydDogODAwMiB9XSxcbiAgICAgIGhlYWx0aENoZWNrOiB7XG4gICAgICAgIGNvbW1hbmQ6IFsnQ01ELVNIRUxMJywgJ2N1cmwgLXNmIGh0dHA6Ly8xMjcuMC4wLjE6ODAwMC92Mi9oZWFsdGgvcmVhZHkgfHwgZXhpdCAxJ10sXG4gICAgICAgIGludGVydmFsOiBjZGsuRHVyYXRpb24uc2Vjb25kcygxNSksXG4gICAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDUpLFxuICAgICAgICByZXRyaWVzOiAzLFxuICAgICAgICBzdGFydFBlcmlvZDogY2RrLkR1cmF0aW9uLnNlY29uZHMoMzApXG4gICAgICB9XG4gICAgfSk7XG5cbiAgICAvLyBUTFMgcHJveHkgc2lkZWNhciB0byB0ZXJtaW5hdGUgVExTIGluc2lkZSB0aGUgdGFzayBhbmQgcHJveHkgdG8gVHJpdG9uIG92ZXIgbG9jYWxob3N0OjgwMDBcbiAgICBjb25zdCByZXF1aXJlVGFyZ2V0VGxzU2VjcmV0UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnUmVxdWlyZVRhcmdldFRsc1NlY3JldCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2ZhbHNlJyxcbiAgICAgIGFsbG93ZWRWYWx1ZXM6IFsndHJ1ZScsICdmYWxzZSddLFxuICAgICAgZGVzY3JpcHRpb246ICdSZXF1aXJlIFRMUyBjZXJ0L2tleSBzZWNyZXQgZm9yIHNpZGVjYXIgKGRpc2FsbG93IHNlbGYtc2lnbmVkKSdcbiAgICB9KTtcblxuICAgIGNvbnN0IHRsc1Byb3h5Q29udGFpbmVyID0gdGFza0RlZmluaXRpb24uYWRkQ29udGFpbmVyKCdUbHNQcm94eUNvbnRhaW5lcicsIHtcbiAgICAgIGltYWdlOiBlY3MuQ29udGFpbmVySW1hZ2UuZnJvbVJlZ2lzdHJ5KCduZ2lueDoxLjI1LWFscGluZScpLFxuICAgICAgbWVtb3J5UmVzZXJ2YXRpb25NaUI6IDEyOCxcbiAgICAgIGNwdTogMTI4LFxuICAgICAgbG9nZ2luZzogZWNzLkxvZ0RyaXZlci5hd3NMb2dzKHsgbG9nR3JvdXAsIHN0cmVhbVByZWZpeDogJ3Rscy1wcm94eScgfSksXG4gICAgICBwb3J0TWFwcGluZ3M6IFt7IGNvbnRhaW5lclBvcnQ6IDg0NDMgfV0sXG4gICAgICBlbnZpcm9ubWVudDoge1xuICAgICAgICBFTkFCTEVfSFRUUDI6IGVuYWJsZVRhcmdldEh0dHAyUGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgU1NMX0NJUEhFUlM6IHRhcmdldFNzbENpcGhlcnNQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBSRVFVSVJFX1RMU19TRUNSRVQ6IHJlcXVpcmVUYXJnZXRUbHNTZWNyZXRQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgfSxcbiAgICAgIGNvbW1hbmQ6IFtcbiAgICAgICAgJ3NoJyxcbiAgICAgICAgJy1jJyxcbiAgICAgICAgW1xuICAgICAgICAgICdzZXQgLWUnLFxuICAgICAgICAgICdhcGsgYWRkIC0tbm8tY2FjaGUgb3BlbnNzbCcsXG4gICAgICAgICAgJ21rZGlyIC1wIC9ldGMvbmdpbngvY2VydHMgL2V0Yy9uZ2lueC9jb25mLmQnLFxuICAgICAgICAgICdpZiBbIFwiJFJFUVVJUkVfVExTX1NFQ1JFVFwiID0gXCJ0cnVlXCIgXSAmJiB7IFsgLXogXCIke1RMU19DRVJUOi19XCIgXSB8fCBbIC16IFwiJHtUTFNfS0VZOi19XCIgXTsgfTsgdGhlbiBlY2hvIFwiVExTIGNlcnQva2V5IHNlY3JldCByZXF1aXJlZFwiID4mMjsgZXhpdCAxOyBmaScsXG4gICAgICAgICAgJ2lmIFsgLW4gXCIke1RMU19DRVJUOi19XCIgXSAmJiBbIC1uIFwiJHtUTFNfS0VZOi19XCIgXTsgdGhlbiBlY2hvIFwiJFRMU19DRVJUXCIgPiAvZXRjL25naW54L2NlcnRzL3Rscy5jcnQgJiYgZWNobyBcIiRUTFNfS0VZXCIgPiAvZXRjL25naW54L2NlcnRzL3Rscy5rZXk7IGVsc2Ugb3BlbnNzbCByZXEgLXg1MDkgLW5vZGVzIC1kYXlzIDM2NTAgLW5ld2tleSByc2E6MjA0OCAtc3ViaiBcIi9DTj1sb2NhbGhvc3RcIiAta2V5b3V0IC9ldGMvbmdpbngvY2VydHMvdGxzLmtleSAtb3V0IC9ldGMvbmdpbngvY2VydHMvdGxzLmNydDsgZmknLFxuICAgICAgICAgIFwiSFRUUDJfRElSRUNUSVZFPScnICYmIFsgXFxcIiRFTkFCTEVfSFRUUDJcXFwiID0gXFxcInRydWVcXFwiIF0gJiYgSFRUUDJfRElSRUNUSVZFPScgaHR0cDInIHx8IHRydWVcIixcbiAgICAgICAgICBcIkNJUEhFUlNfRElSRUNUSVZFPScnICYmIFsgLW4gXFxcIiRTU0xfQ0lQSEVSU1xcXCIgXSAmJiBDSVBIRVJTX0RJUkVDVElWRT1cXFxcXFxcIiAgc3NsX2NpcGhlcnMgJFNTTF9DSVBIRVJTO1xcXFxcXFwiIHx8IHRydWVcIixcbiAgICAgICAgICBcInByaW50ZiAnc2VydmVyIHtcXFxcbiAgbGlzdGVuIDg0NDMgc3NsJXM7XFxcXG4gIHNzbF9jZXJ0aWZpY2F0ZSAvZXRjL25naW54L2NlcnRzL3Rscy5jcnQ7XFxcXG4gIHNzbF9jZXJ0aWZpY2F0ZV9rZXkgL2V0Yy9uZ2lueC9jZXJ0cy90bHMua2V5O1xcXFxuICBzc2xfcHJvdG9jb2xzIFRMU3YxLjIgVExTdjEuMztcXFxcbiVzXFxcXG4gIGxvY2F0aW9uIC8ge1xcXFxuICAgIHByb3h5X3Bhc3MgaHR0cDovLzEyNy4wLjAuMTo4MDAwO1xcXFxuICAgIHByb3h5X3NldF9oZWFkZXIgSG9zdCAkaG9zdDtcXFxcbiAgICBwcm94eV9zZXRfaGVhZGVyIFgtRm9yd2FyZGVkLVByb3RvICRzY2hlbWU7XFxcXG4gICAgcHJveHlfc2V0X2hlYWRlciBYLUZvcndhcmRlZC1Gb3IgJHJlbW90ZV9hZGRyO1xcXFxuICB9XFxcXG59XFxcXG4nIFxcXCIkSFRUUDJfRElSRUNUSVZFXFxcIiBcXFwiJENJUEhFUlNfRElSRUNUSVZFXFxcIiA+IC9ldGMvbmdpbngvY29uZi5kL2RlZmF1bHQuY29uZlwiLFxuICAgICAgICAgIFwibmdpbnggLWcgJ2RhZW1vbiBvZmY7J1wiLFxuICAgICAgICBdLmpvaW4oJyAmJiAnKSxcbiAgICAgIF0sXG4gICAgfSk7XG5cbiAgICAvLyBDb25kaXRpb25hbGx5IHdpcmUgU2VjcmV0cyBNYW5hZ2VyIHNlY3JldCB3aXRoIHtjZXJ0LGtleX0gdG8gVExTX0NFUlQvVExTX0tFWSBlbnYgZm9yIHNpZGVjYXJcbiAgICBjb25zdCB0YXJnZXRDZXJ0UHJvdmlkZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnVHJpdG9uVGFyZ2V0Q2VydFByb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHRyaXRvblRhcmdldENlcnRTZWNyZXRBcm5QYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpXG4gICAgfSk7XG5cbiAgICAvLyBVc2UgcHJvcGVydHkgb3ZlcnJpZGUgdG8gY29uZGl0aW9uYWxseSBhZGQgc2VjcmV0cyB0byB0aGUgVExTIHByb3h5IGNvbnRhaW5lclxuICAgIGNvbnN0IGNmblRhc2tEZWYgPSB0YXNrRGVmaW5pdGlvbi5ub2RlLmRlZmF1bHRDaGlsZCBhcyBlY3MuQ2ZuVGFza0RlZmluaXRpb247XG4gICAgY29uc3QgY2VydFZhbHVlRnJvbSA9IGNkay5Gbi5qb2luKCcnLCBbdHJpdG9uVGFyZ2V0Q2VydFNlY3JldEFyblBhcmFtLnZhbHVlQXNTdHJpbmcsICc6Y2VydDo6J10pO1xuICAgIGNvbnN0IGtleVZhbHVlRnJvbSA9IGNkay5Gbi5qb2luKCcnLCBbdHJpdG9uVGFyZ2V0Q2VydFNlY3JldEFyblBhcmFtLnZhbHVlQXNTdHJpbmcsICc6a2V5OjonXSk7XG5cbiAgICAvLyBGaW5kIHRoZSBUbHNQcm94eUNvbnRhaW5lciBpbmRleCBhbmQgY29uZGl0aW9uYWxseSBzZXQgc2VjcmV0c1xuICAgIGNmblRhc2tEZWYuYWRkUHJvcGVydHlPdmVycmlkZSgnQ29udGFpbmVyRGVmaW5pdGlvbnMuMS5TZWNyZXRzJywgY2RrLkZuLmNvbmRpdGlvbklmKFxuICAgICAgdGFyZ2V0Q2VydFByb3ZpZGVkLmxvZ2ljYWxJZCxcbiAgICAgIFtcbiAgICAgICAgeyBuYW1lOiAnVExTX0NFUlQnLCB2YWx1ZUZyb206IGNlcnRWYWx1ZUZyb20gfSxcbiAgICAgICAgeyBuYW1lOiAnVExTX0tFWScsIHZhbHVlRnJvbToga2V5VmFsdWVGcm9tIH0sXG4gICAgICBdLFxuICAgICAgY2RrLkF3cy5OT19WQUxVRVxuICAgICkpO1xuXG4gICAgLy8gRENHTSBleHBvcnRlciBmb3IgR1BVIG1ldHJpY3NcbiAgICBjb25zdCBkY2dtQ29udGFpbmVyID0gdGFza0RlZmluaXRpb24uYWRkQ29udGFpbmVyKCdEY2dtRXhwb3J0ZXJDb250YWluZXInLCB7XG4gICAgICBpbWFnZTogZWNzLkNvbnRhaW5lckltYWdlLmZyb21FY3JSZXBvc2l0b3J5KGRjZ21FeHBvcnRlclJlcG9zaXRvcnksIGRjZ21JbWFnZVRhZ1BhcmFtLnZhbHVlQXNTdHJpbmcpLFxuICAgICAgbWVtb3J5UmVzZXJ2YXRpb25NaUI6IDI1NixcbiAgICAgIGNwdTogMTI4LFxuICAgICAgbG9nZ2luZzogZWNzLkxvZ0RyaXZlci5hd3NMb2dzKHsgbG9nR3JvdXAsIHN0cmVhbVByZWZpeDogJ2RjZ20tZXhwb3J0ZXInIH0pLFxuICAgICAgcG9ydE1hcHBpbmdzOiBbeyBjb250YWluZXJQb3J0OiA5NDAwIH1dLFxuICAgIH0pO1xuXG4gICAgLy8gQ2xvdWRXYXRjaCBBZ2VudCB0byBzY3JhcGUgRENHTSBtZXRyaWNzXG4gICAgY29uc3QgY3dBZ2VudENvbmZpZ1N0cmluZzogc3RyaW5nID0gSlNPTi5zdHJpbmdpZnkoY3dBZ2VudENvbmZpZyk7XG4gICAgY29uc3QgY3dBZ2VudENvbmZpZ1BhcmFtID0gbmV3IHNzbS5TdHJpbmdQYXJhbWV0ZXIodGhpcywgJ1NzbUN3QWdlbnRDb25maWcnLCB7XG4gICAgICBwYXJhbWV0ZXJOYW1lOiAnL2FtaXJhL2N3YWdlbnRfY29uZmlnJyxcbiAgICAgIHN0cmluZ1ZhbHVlOiBjd0FnZW50Q29uZmlnU3RyaW5nXG4gICAgfSk7XG4gICAgY29uc3QgY3dBZ2VudENvbnRhaW5lciA9IHRhc2tEZWZpbml0aW9uLmFkZENvbnRhaW5lcignQ2xvdWRXYXRjaEFnZW50Q29udGFpbmVyJywge1xuICAgICAgaW1hZ2U6IGVjcy5Db250YWluZXJJbWFnZS5mcm9tRWNyUmVwb3NpdG9yeShjd0FnZW50UmVwb3NpdG9yeSwgY3dBZ2VudEltYWdlVGFnUGFyYW0udmFsdWVBc1N0cmluZyksXG4gICAgICBtZW1vcnlSZXNlcnZhdGlvbk1pQjogMjU2LFxuICAgICAgY3B1OiAxMjgsXG4gICAgICBsb2dnaW5nOiBlY3MuTG9nRHJpdmVyLmF3c0xvZ3MoeyBsb2dHcm91cCwgc3RyZWFtUHJlZml4OiAnY2xvdWR3YXRjaC1hZ2VudCcgfSksXG4gICAgICBjb21tYW5kOiBbJy9vcHQvYXdzL2FtYXpvbi1jbG91ZHdhdGNoLWFnZW50L2Jpbi9hbWF6b24tY2xvdWR3YXRjaC1hZ2VudCcsICctYScsICdmZXRjaC1jb25maWcnLCAnLW0nLCAnZWMyJywgJy1jJywgYHNzbToke2N3QWdlbnRDb25maWdQYXJhbS5wYXJhbWV0ZXJOYW1lfWAsICctcyddLFxuICAgICAgZW52aXJvbm1lbnQ6IHsgQVdTX1JFR0lPTjogY2RrLlN0YWNrLm9mKHRoaXMpLnJlZ2lvbiB9XG4gICAgfSk7XG5cbiAgICAvLyBHcmFudCBDVyBBZ2VudCBhY2Nlc3MgdG8gcmVhZCBpdHMgU1NNIGNvbmZpZ1xuICAgIGN3QWdlbnRDb25maWdQYXJhbS5ncmFudFJlYWQodGFza1JvbGUpO1xuXG4gICAgLy8gSW5jcmVhc2Ugbm9maWxlIGxpbWl0cyBmb3IgY29udGFpbmVyc1xuICAgIHRyaXRvbkNvbnRhaW5lci5hZGRVbGltaXRzKHsgbmFtZTogZWNzLlVsaW1pdE5hbWUuTk9GSUxFLCBzb2Z0TGltaXQ6IDY1NTM2LCBoYXJkTGltaXQ6IDY1NTM2IH0pO1xuXG4gICAgLy8gQXBwbGljYXRpb24gTG9hZCBCYWxhbmNlciBmb3IgVHJpdG9uIEdQVSBjbHVzdGVyXG4gICAgY29uc3QgdHJpdG9uQWxiID0gbmV3IGVsYnYyLkFwcGxpY2F0aW9uTG9hZEJhbGFuY2VyKHRoaXMsICdUcml0b25Mb2FkQmFsYW5jZXInLCB7XG4gICAgICB2cGMsXG4gICAgICBpbnRlcm5ldEZhY2luZzogZmFsc2UsIC8vIEludGVybmFsIEFMQiBzaW5jZSBMYW1iZGEgd2lsbCBjYWxsIGl0XG4gICAgICBzZWN1cml0eUdyb3VwOiBhbGJTZWN1cml0eUdyb3VwLFxuICAgICAgZGVsZXRpb25Qcm90ZWN0aW9uOiB0cnVlXG4gICAgfSk7XG4gICAgdHJpdG9uQWxiLmxvZ0FjY2Vzc0xvZ3MoYWNjZXNzTG9nc0J1Y2tldCwgJ2FsYi1hY2Nlc3MtbG9ncy8nKTtcblxuICAgIGNvbnN0IHRyaXRvblRhcmdldEdyb3VwID0gbmV3IGVsYnYyLkFwcGxpY2F0aW9uVGFyZ2V0R3JvdXAodGhpcywgJ1RyaXRvblRhcmdldEdyb3VwJywge1xuICAgICAgdnBjLFxuICAgICAgcG9ydDogODQ0MyxcbiAgICAgIHByb3RvY29sOiBlbGJ2Mi5BcHBsaWNhdGlvblByb3RvY29sLkhUVFBTLFxuICAgICAgdGFyZ2V0VHlwZTogZWxidjIuVGFyZ2V0VHlwZS5JUCxcbiAgICAgIGhlYWx0aENoZWNrOiB7XG4gICAgICAgIHBhdGg6ICcvdjIvaGVhbHRoL3JlYWR5JyxcbiAgICAgICAgcHJvdG9jb2w6IGVsYnYyLlByb3RvY29sLkhUVFBTLFxuICAgICAgICBoZWFsdGh5VGhyZXNob2xkQ291bnQ6IDMsXG4gICAgICAgIHVuaGVhbHRoeVRocmVzaG9sZENvdW50OiA1LFxuICAgICAgICB0aW1lb3V0OiBjZGsuRHVyYXRpb24uc2Vjb25kcygxMCksXG4gICAgICAgIGludGVydmFsOiBjZGsuRHVyYXRpb24uc2Vjb25kcygzMClcbiAgICAgIH1cbiAgICB9KTtcblxuICAgIGNvbnN0IHRyaXRvbkxpc3RlbmVyID0gdHJpdG9uQWxiLmFkZExpc3RlbmVyKCdUcml0b25MaXN0ZW5lckh0dHBzJywge1xuICAgICAgcG9ydDogNDQzLFxuICAgICAgcHJvdG9jb2w6IGVsYnYyLkFwcGxpY2F0aW9uUHJvdG9jb2wuSFRUUFMsXG4gICAgICBzc2xQb2xpY3k6IGVsYnYyLlNzbFBvbGljeS5UTFMxMl9FWFQsXG4gICAgICBjZXJ0aWZpY2F0ZXM6IFtlbGJ2Mi5MaXN0ZW5lckNlcnRpZmljYXRlLmZyb21Bcm4odHJpdG9uQ2VydEFyblBhcmFtLnZhbHVlQXNTdHJpbmcpXSxcbiAgICAgIGRlZmF1bHRUYXJnZXRHcm91cHM6IFt0cml0b25UYXJnZXRHcm91cF1cbiAgICB9KTtcblxuICAgIC8vIEVDUyBzZXJ2aWNlIGZvciBUcml0b24gR1BVIGluZmVyZW5jZSAoc2NhbGVzIGJhc2VkIG9uIGRlbWFuZClcbiAgICBjb25zdCBzZXJ2aWNlID0gbmV3IGVjcy5FYzJTZXJ2aWNlKHRoaXMsICdUcml0b25JbmZlcmVuY2VTZXJ2aWNlJywge1xuICAgICAgY2x1c3RlcixcbiAgICAgIHRhc2tEZWZpbml0aW9uLFxuICAgICAgc2VydmljZU5hbWU6ICd0cml0b24taW5mZXJlbmNlLXNlcnZpY2UnLFxuICAgICAgZGVzaXJlZENvdW50OiAwLCAvLyBTdGFydCB3aXRoIDAsIHNjYWxlIHVwIGJhc2VkIG9uIEFMQiByZXF1ZXN0c1xuICAgICAgc2VjdXJpdHlHcm91cHM6IFtlY3NTZWN1cml0eUdyb3VwXSxcbiAgICAgIHZwY1N1Ym5ldHM6IHsgc3VibmV0VHlwZTogZWMyLlN1Ym5ldFR5cGUuUFJJVkFURV9XSVRIX0VHUkVTUyB9LFxuICAgICAgY2FwYWNpdHlQcm92aWRlclN0cmF0ZWdpZXM6IFt7XG4gICAgICAgIGNhcGFjaXR5UHJvdmlkZXI6IGNhcGFjaXR5UHJvdmlkZXIuY2FwYWNpdHlQcm92aWRlck5hbWUsXG4gICAgICAgIHdlaWdodDogMVxuICAgICAgfSx7XG4gICAgICAgIGNhcGFjaXR5UHJvdmlkZXI6IGNwRzV4bGFyZ2UuY2FwYWNpdHlQcm92aWRlck5hbWUsXG4gICAgICAgIHdlaWdodDogMVxuICAgICAgfSx7XG4gICAgICAgIGNhcGFjaXR5UHJvdmlkZXI6IGNwRzUyeGxhcmdlLmNhcGFjaXR5UHJvdmlkZXJOYW1lLFxuICAgICAgICB3ZWlnaHQ6IDFcbiAgICAgIH1dLFxuICAgICAgcGxhY2VtZW50U3RyYXRlZ2llczogW1xuICAgICAgICBlY3MuUGxhY2VtZW50U3RyYXRlZ3kuc3ByZWFkQWNyb3NzSW5zdGFuY2VzKClcbiAgICAgIF0sXG4gICAgICBtaW5IZWFsdGh5UGVyY2VudDogMTAwLFxuICAgICAgbWF4SGVhbHRoeVBlcmNlbnQ6IDIwMCxcbiAgICAgIGVuYWJsZUV4ZWN1dGVDb21tYW5kOiB0cnVlXG4gICAgfSk7XG5cbiAgICAvLyBBdHRhY2ggRUNTIHNlcnZpY2UgdG8gQUxCIHRhcmdldCBncm91cFxuICAgIHNlcnZpY2UuYXR0YWNoVG9BcHBsaWNhdGlvblRhcmdldEdyb3VwKHRyaXRvblRhcmdldEdyb3VwKTtcblxuICAgIC8vIEF1dG9zY2FsZSBUcml0b24gc2VydmljZSBiYXNlZCBvbiBBTEIgcmVxdWVzdCBtZXRyaWNzXG4gICAgY29uc3Qgc2NhbGFibGVUYXJnZXQgPSBuZXcgYXBwc2NhbGluZy5TY2FsYWJsZVRhcmdldCh0aGlzLCAnVHJpdG9uU2NhbGFibGVUYXJnZXQnLCB7XG4gICAgICBzZXJ2aWNlTmFtZXNwYWNlOiBhcHBzY2FsaW5nLlNlcnZpY2VOYW1lc3BhY2UuRUNTLFxuICAgICAgbWF4Q2FwYWNpdHk6IDEwLFxuICAgICAgbWluQ2FwYWNpdHk6IDAsXG4gICAgICByZXNvdXJjZUlkOiBgc2VydmljZS8ke2NsdXN0ZXIuY2x1c3Rlck5hbWV9LyR7c2VydmljZS5zZXJ2aWNlTmFtZX1gLFxuICAgICAgc2NhbGFibGVEaW1lbnNpb246ICdlY3M6c2VydmljZTpEZXNpcmVkQ291bnQnXG4gICAgfSk7XG5cbiAgICAvLyBTY2FsZSBiYXNlZCBvbiBBTEIgcmVxdWVzdCBjb3VudCBwZXIgdGFyZ2V0XG4gICAgY29uc3QgYWxiUmVxdWVzdE1ldHJpYyA9IG5ldyBjdy5NZXRyaWMoe1xuICAgICAgbmFtZXNwYWNlOiAnQVdTL0FwcGxpY2F0aW9uRUxCJyxcbiAgICAgIG1ldHJpY05hbWU6ICdSZXF1ZXN0Q291bnRQZXJUYXJnZXQnLFxuICAgICAgZGltZW5zaW9uc01hcDoge1xuICAgICAgICBMb2FkQmFsYW5jZXI6IHRyaXRvbkFsYi5sb2FkQmFsYW5jZXJGdWxsTmFtZSxcbiAgICAgICAgVGFyZ2V0R3JvdXA6IHRyaXRvblRhcmdldEdyb3VwLnRhcmdldEdyb3VwRnVsbE5hbWVcbiAgICAgIH0sXG4gICAgICBzdGF0aXN0aWM6ICdTdW0nLFxuICAgICAgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKVxuICAgIH0pO1xuXG4gICAgc2NhbGFibGVUYXJnZXQuc2NhbGVUb1RyYWNrTWV0cmljKCdUcml0b25TY2FsaW5nJywge1xuICAgICAgY3VzdG9tTWV0cmljOiBhbGJSZXF1ZXN0TWV0cmljLFxuICAgICAgdGFyZ2V0VmFsdWU6IDUwLFxuICAgICAgc2NhbGVJbkNvb2xkb3duOiBjZGsuRHVyYXRpb24ubWludXRlcygyKSxcbiAgICAgIHNjYWxlT3V0Q29vbGRvd246IGNkay5EdXJhdGlvbi5zZWNvbmRzKDMwKVxuICAgIH0pO1xuXG4gICAgLy8gQWRkaXRpb25hbCBzY2FsaW5nIHBvbGljeSBiYXNlZCBvbiBUcml0b24gcDk1IGxhdGVuY3lcbiAgICBjb25zdCB0cml0b25MYXRlbmN5UDk1TWV0cmljID0gbmV3IGN3Lk1ldHJpYyh7XG4gICAgICBuYW1lc3BhY2U6ICdDV0FnZW50JyxcbiAgICAgIG1ldHJpY05hbWU6ICdudl9pbmZlcmVuY2VfcmVxdWVzdF9kdXJhdGlvbl91cycsXG4gICAgICBzdGF0aXN0aWM6ICdwOTUnLFxuICAgICAgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKVxuICAgIH0pO1xuICAgIGNvbnN0IGxhdGVuY3lIaWdoID0gbmV3IGN3LkFsYXJtKHRoaXMsICdUcml0b25MYXRlbmN5SGlnaEZvclNjYWxpbmcnLCB7XG4gICAgICBtZXRyaWM6IHRyaXRvbkxhdGVuY3lQOTVNZXRyaWMsXG4gICAgICB0aHJlc2hvbGQ6IDUwMDAwMCwgLy8gNTAwbXNcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAzLFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuR1JFQVRFUl9USEFOX1RIUkVTSE9MRFxuICAgIH0pO1xuICAgIGNvbnN0IGxhdGVuY3lMb3cgPSBuZXcgY3cuQWxhcm0odGhpcywgJ1RyaXRvbkxhdGVuY3lMb3dGb3JTY2FsaW5nJywge1xuICAgICAgbWV0cmljOiB0cml0b25MYXRlbmN5UDk1TWV0cmljLFxuICAgICAgdGhyZXNob2xkOiAyMDAwMDAsIC8vIDIwMG1zXG4gICAgICBldmFsdWF0aW9uUGVyaW9kczogNSxcbiAgICAgIGNvbXBhcmlzb25PcGVyYXRvcjogY3cuQ29tcGFyaXNvbk9wZXJhdG9yLkxFU1NfVEhBTl9PUl9FUVVBTF9UT19USFJFU0hPTERcbiAgICB9KTtcbiAgICBzY2FsYWJsZVRhcmdldC5zY2FsZU9uTWV0cmljKCdMYXRlbmN5U2NhbGVPdXQnLCB7XG4gICAgICBtZXRyaWM6IHRyaXRvbkxhdGVuY3lQOTVNZXRyaWMsXG4gICAgICBzY2FsaW5nU3RlcHM6IFtcbiAgICAgICAgeyBsb3dlcjogNTAwMDAwLCBjaGFuZ2U6ICsxIH0sXG4gICAgICAgIHsgbG93ZXI6IDgwMDAwMCwgY2hhbmdlOiArMiB9XG4gICAgICBdLFxuICAgICAgYWRqdXN0bWVudFR5cGU6IGFwcHNjYWxpbmcuQWRqdXN0bWVudFR5cGUuQ0hBTkdFX0lOX0NBUEFDSVRZLFxuICAgICAgY29vbGRvd246IGNkay5EdXJhdGlvbi5taW51dGVzKDIpLFxuICAgICAgbWluQWRqdXN0bWVudE1hZ25pdHVkZTogMVxuICAgIH0pO1xuXG4gICAgLy8gT3B0aW9uYWwgc2NhbGluZyBiYXNlZCBvbiBHUFUgdXRpbGl6YXRpb24gKERDR00pXG4gICAgY29uc3QgZ3B1VXRpbEZvclNjYWxpbmcgPSBuZXcgY3cuTWV0cmljKHtcbiAgICAgIG5hbWVzcGFjZTogJ0NXQWdlbnQnLFxuICAgICAgbWV0cmljTmFtZTogJ0RDR01fRklfREVWX0dQVV9VVElMJyxcbiAgICAgIHN0YXRpc3RpYzogJ0F2ZXJhZ2UnLFxuICAgICAgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKVxuICAgIH0pO1xuICAgIHNjYWxhYmxlVGFyZ2V0LnNjYWxlT25NZXRyaWMoJ0dwdVV0aWxTY2FsaW5nJywge1xuICAgICAgbWV0cmljOiBncHVVdGlsRm9yU2NhbGluZyxcbiAgICAgIHNjYWxpbmdTdGVwczogW1xuICAgICAgICB7IGxvd2VyOiA3MCwgY2hhbmdlOiArMSB9LFxuICAgICAgICB7IGxvd2VyOiA5MCwgY2hhbmdlOiArMiB9LFxuICAgICAgICB7IHVwcGVyOiAzMCwgY2hhbmdlOiAtMSB9XG4gICAgICBdLFxuICAgICAgYWRqdXN0bWVudFR5cGU6IGFwcHNjYWxpbmcuQWRqdXN0bWVudFR5cGUuQ0hBTkdFX0lOX0NBUEFDSVRZLFxuICAgICAgY29vbGRvd246IGNkay5EdXJhdGlvbi5taW51dGVzKDIpLFxuICAgICAgbWluQWRqdXN0bWVudE1hZ25pdHVkZTogMVxuICAgIH0pO1xuXG4gICAgLy8gQWRkIG91dHB1dCBmb3IgVHJpdG9uIGNsdXN0ZXIgVVJMXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1RyaXRvbkNsdXN0ZXJVcmwnLCB7XG4gICAgICB2YWx1ZTogYGh0dHBzOi8vJHt0cml0b25BbGIubG9hZEJhbGFuY2VyRG5zTmFtZX1gLFxuICAgICAgZGVzY3JpcHRpb246ICdIVFRQUyBVUkwgZm9yIFRyaXRvbiBHUFUgaW5mZXJlbmNlIGNsdXN0ZXInLFxuICAgICAgY29uZGl0aW9uOiB1c2VUcml0b25Db25kaXRpb25cbiAgICB9KTtcblxuICAgIC8vIFB1Ymxpc2ggVHJpdG9uIFVSTCB0byBTU00gZm9yIGNyb3NzLXN0YWNrIGxpbmtpbmdcbiAgICBuZXcgc3NtLlN0cmluZ1BhcmFtZXRlcih0aGlzLCAnVHJpdG9uQWxiVXJsUGFyYW0nLCB7XG4gICAgICBwYXJhbWV0ZXJOYW1lOiAnL2FtaXJhL3RyaXRvbl9hbGJfdXJsJyxcbiAgICAgIHN0cmluZ1ZhbHVlOiBgaHR0cHM6Ly8ke3RyaXRvbkFsYi5sb2FkQmFsYW5jZXJEbnNOYW1lfWBcbiAgICB9KTtcblxuICAgIC8vIFB1Ymxpc2ggVlBDIGFuZCBzdWJuZXQgYXR0cmlidXRlcyBmb3IgY3Jvc3Mtc3RhY2sgTGFtYmRhIGF0dGFjaG1lbnRcbiAgICBuZXcgc3NtLlN0cmluZ1BhcmFtZXRlcih0aGlzLCAnVnBjSWRQYXJhbScsIHtcbiAgICAgIHBhcmFtZXRlck5hbWU6ICcvYW1pcmEvdnBjX2lkJyxcbiAgICAgIHN0cmluZ1ZhbHVlOiB2cGMudnBjSWRcbiAgICB9KTtcbiAgICBuZXcgc3NtLlN0cmluZ1BhcmFtZXRlcih0aGlzLCAnVnBjUHJpdmF0ZVN1Ym5ldElkc1BhcmFtJywge1xuICAgICAgcGFyYW1ldGVyTmFtZTogJy9hbWlyYS92cGNfcHJpdmF0ZV9zdWJuZXRfaWRzJyxcbiAgICAgIHN0cmluZ1ZhbHVlOiB2cGMucHJpdmF0ZVN1Ym5ldHMubWFwKHMgPT4gcy5zdWJuZXRJZCkuam9pbignLCcpXG4gICAgfSk7XG4gICAgbmV3IHNzbS5TdHJpbmdQYXJhbWV0ZXIodGhpcywgJ0FsYlNlY3VyaXR5R3JvdXBJZFBhcmFtJywge1xuICAgICAgcGFyYW1ldGVyTmFtZTogJy9hbWlyYS9hbGJfc2dfaWQnLFxuICAgICAgc3RyaW5nVmFsdWU6IGFsYlNlY3VyaXR5R3JvdXAuc2VjdXJpdHlHcm91cElkXG4gICAgfSk7XG5cbiAgICAvLyBHUFUgY2x1c3RlciBtb25pdG9yaW5nIGFuZCBhbGFybXNcblxuICAgIC8vIFNOUyBub3RpZmljYXRpb25zIGZvciBhbGFybXNcbiAgICBjb25zdCBhbGFybVRvcGljID0gbmV3IHNucy5Ub3BpYyh0aGlzLCAnT3BzQWxhcm1Ub3BpYycsIHsgZGlzcGxheU5hbWU6ICdUcml0b24gR1BVIENsdXN0ZXIgQWxhcm1zJyB9KTtcbiAgICBjb25zdCBhbGFybUFjdGlvbiA9IG5ldyBjd2FjdGlvbnMuU25zQWN0aW9uKGFsYXJtVG9waWMpO1xuICAgIC8vIE9wdGlvbmFsIFNlY3JldHMgTWFuYWdlciByb3RhdGlvbiBhbGFybSAoaWYgc2VjcmV0IEFSTiBwcm92aWRlZClcbiAgICBjb25zdCByb3RhdGlvbkFsYXJtRW1haWxQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdSb3RhdGlvbkFsYXJtRW1haWwnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBlbWFpbCBmb3IgU2VjcmV0cyBNYW5hZ2VyIHJvdGF0aW9uIGZhaWx1cmUgYWxhcm1zJ1xuICAgIH0pO1xuICAgIGNvbnN0IHJvdGF0aW9uQWxhcm1FbWFpbFByb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ1JvdGF0aW9uQWxhcm1FbWFpbFByb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHJvdGF0aW9uQWxhcm1FbWFpbFBhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSlcbiAgICB9KTtcbiAgICBjb25zdCByb3RhdGlvbkZhaWx1cmVzTWV0cmljID0gbmV3IGN3Lk1ldHJpYyh7XG4gICAgICBuYW1lc3BhY2U6ICdBV1MvU2VjcmV0c01hbmFnZXInLFxuICAgICAgbWV0cmljTmFtZTogJ1JvdGF0aW9uRW5hYmxlZCcsXG4gICAgICBzdGF0aXN0aWM6ICdNaW5pbXVtJyxcbiAgICAgIHBlcmlvZDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoNSlcbiAgICB9KTtcbiAgICBjb25zdCByb3RhdGlvbkFsYXJtID0gbmV3IGN3LkFsYXJtKHRoaXMsICdTZWNyZXRzUm90YXRpb25EaXNhYmxlZCcsIHtcbiAgICAgIG1ldHJpYzogcm90YXRpb25GYWlsdXJlc01ldHJpYyxcbiAgICAgIHRocmVzaG9sZDogMSxcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAxLFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuTEVTU19USEFOX1RIUkVTSE9MRCxcbiAgICAgIGFsYXJtRGVzY3JpcHRpb246ICdTZWNyZXRzIE1hbmFnZXIgcm90YXRpb24gZGlzYWJsZWQgb3IgZmFpbGluZydcbiAgICB9KTtcbiAgICByb3RhdGlvbkFsYXJtLmFkZEFsYXJtQWN0aW9uKGFsYXJtQWN0aW9uKTtcblxuICAgIC8vIE9wdGlvbmFsIGVtYWlsIHN1YnNjcmlwdGlvbiBwYXJhbWV0ZXJcbiAgICBjb25zdCBhbGFybUVtYWlsUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQWxhcm1FbWFpbCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIGVtYWlsIHRvIHN1YnNjcmliZSB0byBPcHMgYWxhcm1zJ1xuICAgIH0pO1xuICAgIGNvbnN0IGVtYWlsUHJvdmlkZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnQWxhcm1FbWFpbFByb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGFsYXJtRW1haWxQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpXG4gICAgfSk7XG4gICAgY29uc3Qgc3Vic2NyaXB0aW9uID0gbmV3IHNucy5DZm5TdWJzY3JpcHRpb24odGhpcywgJ09wc0FsYXJtRW1haWxTdWJzY3JpcHRpb24nLCB7XG4gICAgICBwcm90b2NvbDogJ2VtYWlsJyxcbiAgICAgIHRvcGljQXJuOiBhbGFybVRvcGljLnRvcGljQXJuLFxuICAgICAgZW5kcG9pbnQ6IGFsYXJtRW1haWxQYXJhbS52YWx1ZUFzU3RyaW5nXG4gICAgfSk7XG4gICAgc3Vic2NyaXB0aW9uLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gZW1haWxQcm92aWRlZDtcblxuICAgIC8vIE1hbnVhbCBlbnF1ZXVlIExhbWJkYSBmb3IgdGVzdGluZyAoYWNjZXB0cyBKU09OIHtcImFjdGl2aXR5X2lkc1wiOiBbXCIuLi5cIiwgLi4uXX0pXG4gICAgY29uc3QgbWFudWFsRW5xdWV1ZUZuID0gbmV3IGxhbWJkYS5GdW5jdGlvbih0aGlzLCAnTWFudWFsRW5xdWV1ZUZ1bmN0aW9uJywge1xuICAgICAgcnVudGltZTogbGFtYmRhLlJ1bnRpbWUuUFlUSE9OXzNfMTIsXG4gICAgICBoYW5kbGVyOiAnaW5kZXguaGFuZGxlcicsXG4gICAgICBjb2RlOiBsYW1iZGEuQ29kZS5mcm9tQXNzZXQoJy4uL2xhbWJkYS9tYW51YWxfZW5xdWV1ZScpLFxuICAgICAgdGltZW91dDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMSksXG4gICAgICB0cmFjaW5nOiBsYW1iZGEuVHJhY2luZy5BQ1RJVkUsXG4gICAgICBlbnZpcm9ubWVudDogeyBKT0JTX1FVRVVFX1VSTDogam9ic1F1ZXVlLnF1ZXVlVXJsIH1cbiAgICB9KTtcbiAgICBqb2JzUXVldWUuZ3JhbnRTZW5kTWVzc2FnZXMobWFudWFsRW5xdWV1ZUZuKTtcbiAgICBjb25zdCBtYW51YWxVcmwgPSBtYW51YWxFbnF1ZXVlRm4uYWRkRnVuY3Rpb25Vcmwoe1xuICAgICAgYXV0aFR5cGU6IGxhbWJkYS5GdW5jdGlvblVybEF1dGhUeXBlLkFXU19JQU0sXG4gICAgICBjb3JzOiB7IGFsbG93ZWRPcmlnaW5zOiBbJyonXSwgYWxsb3dlZE1ldGhvZHM6IFtsYW1iZGEuSHR0cE1ldGhvZC5QT1NULCBsYW1iZGEuSHR0cE1ldGhvZC5PUFRJT05TXSB9XG4gICAgfSk7XG5cbiAgICAvLyBBbGFybXM6IEdQVSB1dGlsaXphdGlvbiBsb3cvaGlnaCAoZnJvbSBEQ0dNIHZpYSBDV0FnZW50IFByb21ldGhldXMpXG4gICAgY29uc3QgZ3B1VXRpbE1ldHJpYyA9IG5ldyBjdy5NZXRyaWMoe1xuICAgICAgbmFtZXNwYWNlOiAnQ1dBZ2VudCcsXG4gICAgICBtZXRyaWNOYW1lOiAnRENHTV9GSV9ERVZfR1BVX1VUSUwnLFxuICAgICAgc3RhdGlzdGljOiAnQXZlcmFnZScsXG4gICAgICBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDEpXG4gICAgfSk7XG4gICAgY29uc3QgZ3B1TWVtVXNlZCA9IG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdDV0FnZW50JywgbWV0cmljTmFtZTogJ0RDR01fRklfREVWX0ZCX1VTRUQnLCBzdGF0aXN0aWM6ICdBdmVyYWdlJywgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKSB9KTtcbiAgICBjb25zdCBncHVNZW1Ub3RhbCA9IG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdDV0FnZW50JywgbWV0cmljTmFtZTogJ0RDR01fRklfREVWX0ZCX1RPVEFMJywgc3RhdGlzdGljOiAnQXZlcmFnZScsIHBlcmlvZDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMSkgfSk7XG4gICAgY29uc3QgZ3B1VXRpbExvdyA9IG5ldyBjdy5BbGFybSh0aGlzLCAnR3B1VXRpbExvdycsIHtcbiAgICAgIG1ldHJpYzogZ3B1VXRpbE1ldHJpYyxcbiAgICAgIHRocmVzaG9sZDogMjAsXG4gICAgICBldmFsdWF0aW9uUGVyaW9kczogNSxcbiAgICAgIGNvbXBhcmlzb25PcGVyYXRvcjogY3cuQ29tcGFyaXNvbk9wZXJhdG9yLkxFU1NfVEhBTl9USFJFU0hPTERcbiAgICB9KTtcbiAgICBncHVVdGlsTG93LmFkZEFsYXJtQWN0aW9uKGFsYXJtQWN0aW9uKTtcbiAgICBjb25zdCBncHVVdGlsSGlnaCA9IG5ldyBjdy5BbGFybSh0aGlzLCAnR3B1VXRpbEhpZ2gnLCB7XG4gICAgICBtZXRyaWM6IGdwdVV0aWxNZXRyaWMsXG4gICAgICB0aHJlc2hvbGQ6IDk1LFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDMsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fVEhSRVNIT0xEXG4gICAgfSk7XG4gICAgZ3B1VXRpbEhpZ2guYWRkQWxhcm1BY3Rpb24oYWxhcm1BY3Rpb24pO1xuXG4gICAgLy8gVHJpdG9uIHJlcXVlc3QgbGF0ZW5jeSAoOTV0aCBwZXJjZW50aWxlLCBtaWNyb3NlY29uZHMpXG4gICAgY29uc3QgdHJpdG9uUDk1ID0gbmV3IGN3Lk1ldHJpYyh7XG4gICAgICBuYW1lc3BhY2U6ICdDV0FnZW50JyxcbiAgICAgIG1ldHJpY05hbWU6ICdudl9pbmZlcmVuY2VfcmVxdWVzdF9kdXJhdGlvbl91cycsXG4gICAgICBzdGF0aXN0aWM6ICdwOTUnLFxuICAgICAgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKVxuICAgIH0pO1xuICAgIGNvbnN0IHRyaXRvblF1ZXVlUDk1ID0gbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0NXQWdlbnQnLCBtZXRyaWNOYW1lOiAnbnZfaW5mZXJlbmNlX3F1ZXVlX2R1cmF0aW9uX3VzJywgc3RhdGlzdGljOiAncDk1JywgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKSB9KTtcbiAgICBjb25zdCB0cml0b25UaHJvdWdocHV0ID0gbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0NXQWdlbnQnLCBtZXRyaWNOYW1lOiAnbnZfaW5mZXJlbmNlX2NvdW50Jywgc3RhdGlzdGljOiAnU3VtJywgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKSB9KTtcbiAgICBjb25zdCB0cml0b25GYWlsdXJlcyA9IG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdDV0FnZW50JywgbWV0cmljTmFtZTogJ252X2luZmVyZW5jZV9mYWlsJywgc3RhdGlzdGljOiAnU3VtJywgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKSB9KTtcbiAgICBjb25zdCB0cml0b25MYXRlbmN5SGlnaCA9IG5ldyBjdy5BbGFybSh0aGlzLCAnVHJpdG9uTGF0ZW5jeUhpZ2gnLCB7XG4gICAgICBtZXRyaWM6IHRyaXRvblA5NSxcbiAgICAgIHRocmVzaG9sZDogNTAwMDAwLCAvLyA1MDBtc1xuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDUsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fVEhSRVNIT0xEXG4gICAgfSk7XG4gICAgdHJpdG9uTGF0ZW5jeUhpZ2guYWRkQWxhcm1BY3Rpb24oYWxhcm1BY3Rpb24pO1xuICAgIGNvbnN0IHRyaXRvblF1ZXVlSGlnaCA9IG5ldyBjdy5BbGFybSh0aGlzLCAnVHJpdG9uUXVldWVMYXRlbmN5SGlnaCcsIHtcbiAgICAgIG1ldHJpYzogdHJpdG9uUXVldWVQOTUsXG4gICAgICB0aHJlc2hvbGQ6IDIwMDAwMCwgLy8gMjAwbXNcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiA1LFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuR1JFQVRFUl9USEFOX1RIUkVTSE9MRFxuICAgIH0pO1xuICAgIHRyaXRvblF1ZXVlSGlnaC5hZGRBbGFybUFjdGlvbihhbGFybUFjdGlvbik7XG4gICAgY29uc3QgdHJpdG9uRmFpbHVyZXNIaWdoID0gbmV3IGN3LkFsYXJtKHRoaXMsICdUcml0b25GYWlsdXJlc0hpZ2gnLCB7XG4gICAgICBtZXRyaWM6IHRyaXRvbkZhaWx1cmVzLFxuICAgICAgdGhyZXNob2xkOiAxLFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDEsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fT1JfRVFVQUxfVE9fVEhSRVNIT0xEXG4gICAgfSk7XG4gICAgdHJpdG9uRmFpbHVyZXNIaWdoLmFkZEFsYXJtQWN0aW9uKGFsYXJtQWN0aW9uKTtcblxuICAgIC8vIEFsYXJtIGZvciBETFEgZGVwdGggaW4gR1BVIHN0YWNrXG4gICAgY29uc3Qgam9ic0RscURlcHRoQWxhcm0gPSBuZXcgY3cuQWxhcm0odGhpcywgJ0pvYnNEbHFEZXB0aEFsYXJtJywge1xuICAgICAgbWV0cmljOiBkbHEubWV0cmljQXBwcm94aW1hdGVOdW1iZXJPZk1lc3NhZ2VzVmlzaWJsZSgpLFxuICAgICAgdGhyZXNob2xkOiAxLFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDEsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fT1JfRVFVQUxfVE9fVEhSRVNIT0xEXG4gICAgfSk7XG4gICAgam9ic0RscURlcHRoQWxhcm0uYWRkQWxhcm1BY3Rpb24oYWxhcm1BY3Rpb24pO1xuXG4gICAgLy8gRGFzaGJvYXJkIGZvciBHUFUgYW5kIFRyaXRvblxuICAgIGNvbnN0IGRhc2hib2FyZCA9IG5ldyBjd19kYXNoLkRhc2hib2FyZCh0aGlzLCAnQW1pcmFHcHVUcml0b25EYXNoYm9hcmQnLCB7IGRhc2hib2FyZE5hbWU6ICdBbWlyYUdwdVRyaXRvbicgfSk7XG4gICAgZGFzaGJvYXJkLmFkZFdpZGdldHMoXG4gICAgICBuZXcgY3dfZGFzaC5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnR1BVIFV0aWxpemF0aW9uJyxcbiAgICAgICAgbGVmdDogW2dwdVV0aWxNZXRyaWNdLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3X2Rhc2guR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ1RyaXRvbiBwOTUgTGF0ZW5jeSAodXMpJyxcbiAgICAgICAgbGVmdDogW3RyaXRvblA5NV0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSksXG4gICAgICBuZXcgY3dfZGFzaC5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnR1BVIE1lbW9yeSAoYnl0ZXMpJyxcbiAgICAgICAgbGVmdDogW2dwdU1lbVVzZWQsIGdwdU1lbVRvdGFsXSxcbiAgICAgICAgd2lkdGg6IDEyXG4gICAgICB9KSxcbiAgICAgIG5ldyBjd19kYXNoLkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdUcml0b24gUXVldWUgcDk1ICh1cyknLFxuICAgICAgICBsZWZ0OiBbdHJpdG9uUXVldWVQOTVdLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3X2Rhc2guR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ1RyaXRvbiBUaHJvdWdocHV0IChyZXEvbWluKSAmIEZhaWx1cmVzJyxcbiAgICAgICAgbGVmdDogW3RyaXRvblRocm91Z2hwdXRdLFxuICAgICAgICByaWdodDogW3RyaXRvbkZhaWx1cmVzXSxcbiAgICAgICAgd2lkdGg6IDI0XG4gICAgICB9KSxcbiAgICAgIG5ldyBjd19kYXNoLkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdJbmZlcmVuY2UgU0xPcyAocDk1IG1zKScsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQW1pcmEvSW5mZXJlbmNlJywgbWV0cmljTmFtZTogJ0luZmVyZW5jZVRvdGFsTXMnLCBzdGF0aXN0aWM6ICdwOTUnLCBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDEpIH0pLFxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3X2Rhc2guR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0FjdGl2aXR5IFNMT3MgKHA5NSBtcyknLFxuICAgICAgICBsZWZ0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0FtaXJhL0FjdGl2aXR5JywgbWV0cmljTmFtZTogJ0FjdGl2aXR5VG90YWxNcycsIHN0YXRpc3RpYzogJ3A5NScsIHBlcmlvZDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMSkgfSksXG4gICAgICAgIF0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSksXG4gICAgICBuZXcgY3dfZGFzaC5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnRUNTIERlc2lyZWQgdnMgUnVubmluZycsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnRUNTL0NvbnRhaW5lckluc2lnaHRzJywgbWV0cmljTmFtZTogJ1NlcnZpY2VEZXNpcmVkQ291bnQnLCBkaW1lbnNpb25zTWFwOiB7IENsdXN0ZXJOYW1lOiBjbHVzdGVyLmNsdXN0ZXJOYW1lLCBTZXJ2aWNlTmFtZTogc2VydmljZS5zZXJ2aWNlTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KSxcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnRUNTL0NvbnRhaW5lckluc2lnaHRzJywgbWV0cmljTmFtZTogJ1NlcnZpY2VSdW5uaW5nQ291bnQnLCBkaW1lbnNpb25zTWFwOiB7IENsdXN0ZXJOYW1lOiBjbHVzdGVyLmNsdXN0ZXJOYW1lLCBTZXJ2aWNlTmFtZTogc2VydmljZS5zZXJ2aWNlTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KVxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMjRcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3X2Rhc2guR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ1NRUyBEZXB0aCAmIE9sZGVzdCBBZ2UnLFxuICAgICAgICBsZWZ0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0FXUy9TUVMnLCBtZXRyaWNOYW1lOiAnQXBwcm94aW1hdGVOdW1iZXJPZk1lc3NhZ2VzVmlzaWJsZScsIGRpbWVuc2lvbnNNYXA6IHsgUXVldWVOYW1lOiBqb2JzUXVldWUucXVldWVOYW1lIH0sIHN0YXRpc3RpYzogJ0F2ZXJhZ2UnIH0pXG4gICAgICAgIF0sXG4gICAgICAgIHJpZ2h0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0FXUy9TUVMnLCBtZXRyaWNOYW1lOiAnQXBwcm94aW1hdGVBZ2VPZk9sZGVzdE1lc3NhZ2UnLCBkaW1lbnNpb25zTWFwOiB7IFF1ZXVlTmFtZTogam9ic1F1ZXVlLnF1ZXVlTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KVxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMjRcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3X2Rhc2guR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0FTRyBEZXNpcmVkIHZzIEluU2VydmljZScsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQVdTL0F1dG9TY2FsaW5nJywgbWV0cmljTmFtZTogJ0dyb3VwRGVzaXJlZENhcGFjaXR5JywgZGltZW5zaW9uc01hcDogeyBBdXRvU2NhbGluZ0dyb3VwTmFtZTogYXV0b1NjYWxpbmdHcm91cC5hdXRvU2NhbGluZ0dyb3VwTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KSxcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQVdTL0F1dG9TY2FsaW5nJywgbWV0cmljTmFtZTogJ0dyb3VwSW5TZXJ2aWNlSW5zdGFuY2VzJywgZGltZW5zaW9uc01hcDogeyBBdXRvU2NhbGluZ0dyb3VwTmFtZTogYXV0b1NjYWxpbmdHcm91cC5hdXRvU2NhbGluZ0dyb3VwTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KVxuICAgICAgICBdLFxuICAgICAgICByaWdodDogW1xuICAgICAgICAgIG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdBV1MvQXV0b1NjYWxpbmcnLCBtZXRyaWNOYW1lOiAnR3JvdXBEZXNpcmVkQ2FwYWNpdHknLCBkaW1lbnNpb25zTWFwOiB7IEF1dG9TY2FsaW5nR3JvdXBOYW1lOiBhc2dHNXhsYXJnZS5hdXRvU2NhbGluZ0dyb3VwTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KSxcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQVdTL0F1dG9TY2FsaW5nJywgbWV0cmljTmFtZTogJ0dyb3VwRGVzaXJlZENhcGFjaXR5JywgZGltZW5zaW9uc01hcDogeyBBdXRvU2NhbGluZ0dyb3VwTmFtZTogYXNnRzUyeGxhcmdlLmF1dG9TY2FsaW5nR3JvdXBOYW1lIH0sIHN0YXRpc3RpYzogJ0F2ZXJhZ2UnIH0pXG4gICAgICAgIF0sXG4gICAgICAgIHdpZHRoOiAyNFxuICAgICAgfSksXG4gICAgICBuZXcgY3dfZGFzaC5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnTGFtYmRhIEludm9jYXRpb25zL0Vycm9ycycsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBtYW51YWxFbnF1ZXVlRm4ubWV0cmljSW52b2NhdGlvbnMoKSxcbiAgICAgICAgICBtYW51YWxFbnF1ZXVlRm4ubWV0cmljRXJyb3JzKClcbiAgICAgICAgXSxcbiAgICAgICAgcmlnaHQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQVdTL0xhbWJkYScsIG1ldHJpY05hbWU6ICdJbnZvY2F0aW9ucycsIGRpbWVuc2lvbnNNYXA6IHsgRnVuY3Rpb25OYW1lOiAnRWNzRHJhaW5PblNwb3RGbicgfSwgc3RhdGlzdGljOiAnU3VtJyB9KSxcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQVdTL0xhbWJkYScsIG1ldHJpY05hbWU6ICdFcnJvcnMnLCBkaW1lbnNpb25zTWFwOiB7IEZ1bmN0aW9uTmFtZTogJ0Vjc0RyYWluT25TcG90Rm4nIH0sIHN0YXRpc3RpYzogJ1N1bScgfSlcbiAgICAgICAgXSxcbiAgICAgICAgd2lkdGg6IDI0XG4gICAgICB9KVxuICAgICk7XG5cbiAgICAvLyBTcG90IElUTi9SZWJhbGFuY2UgZHJhaW4gTGFtYmRhXG4gICAgY29uc3QgZHJhaW5GbiA9IG5ldyBsYW1iZGEuRnVuY3Rpb24odGhpcywgJ0Vjc0RyYWluT25TcG90Rm4nLCB7XG4gICAgICBydW50aW1lOiBsYW1iZGEuUnVudGltZS5QWVRIT05fM18xMixcbiAgICAgIGhhbmRsZXI6ICdpbmRleC5oYW5kbGVyJyxcbiAgICAgIGNvZGU6IGxhbWJkYS5Db2RlLmZyb21Bc3NldCgnLi4vbGFtYmRhL2Vjc19kcmFpbl9vbl9zcG90JyksXG4gICAgICB0aW1lb3V0OiBjZGsuRHVyYXRpb24uc2Vjb25kcyg2MCksXG4gICAgICBlbnZpcm9ubWVudDogeyBDTFVTVEVSX0FSTjogY2x1c3Rlci5jbHVzdGVyQXJuIH1cbiAgICB9KTtcbiAgICBkcmFpbkZuLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbJ2VjczpMaXN0Q29udGFpbmVySW5zdGFuY2VzJywgJ2VjczpEZXNjcmliZUNvbnRhaW5lckluc3RhbmNlcyddLFxuICAgICAgcmVzb3VyY2VzOiBbY2x1c3Rlci5jbHVzdGVyQXJuXVxuICAgIH0pKTtcbiAgICBkcmFpbkZuLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbJ2VjczpVcGRhdGVDb250YWluZXJJbnN0YW5jZXNTdGF0ZSddLFxuICAgICAgcmVzb3VyY2VzOiBbJyonXSxcbiAgICAgIGNvbmRpdGlvbnM6IHsgU3RyaW5nRXF1YWxzOiB7ICdlY3M6Y2x1c3Rlcic6IGNsdXN0ZXIuY2x1c3RlckFybiB9IH1cbiAgICB9KSk7XG4gICAgbmV3IGV2ZW50cy5SdWxlKHRoaXMsICdTcG90SW50ZXJydXB0aW9uRHJhaW5SdWxlJywge1xuICAgICAgZGVzY3JpcHRpb246ICdEcmFpbiBFQ1MgY29udGFpbmVyIGluc3RhbmNlcyBvbiBTcG90IGludGVycnVwdGlvbiB3YXJuaW5ncycsXG4gICAgICBldmVudFBhdHRlcm46IHtcbiAgICAgICAgc291cmNlOiBbJ2F3cy5lYzInXSxcbiAgICAgICAgZGV0YWlsVHlwZTogWydFQzIgU3BvdCBJbnN0YW5jZSBJbnRlcnJ1cHRpb24gV2FybmluZycsICdFQzIgSW5zdGFuY2UgUmViYWxhbmNlIFJlY29tbWVuZGF0aW9uJ11cbiAgICAgIH0sXG4gICAgICB0YXJnZXRzOiBbbmV3IHRhcmdldHMuTGFtYmRhRnVuY3Rpb24oZHJhaW5GbildXG4gICAgfSk7XG5cbiAgICAvLyBPdXRwdXRzIGZvciBHUFUgY2x1c3RlciBhbmQgZGFzaGJvYXJkIGxpbmtcbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnVHJpdG9uUmVwb3NpdG9yeVVyaScsIHtcbiAgICAgIHZhbHVlOiB0cml0b25SZXBvc2l0b3J5LnJlcG9zaXRvcnlVcmksXG4gICAgICBkZXNjcmlwdGlvbjogJ1RyaXRvbiBFQ1IgUmVwb3NpdG9yeSBVUkknLFxuICAgICAgY29uZGl0aW9uOiB1c2VUcml0b25Db25kaXRpb25cbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdHcHVDbHVzdGVyTmFtZScsIHtcbiAgICAgIHZhbHVlOiBjbHVzdGVyLmNsdXN0ZXJOYW1lLFxuICAgICAgZGVzY3JpcHRpb246ICdHUFUgRUNTIENsdXN0ZXIgTmFtZScsXG4gICAgICBjb25kaXRpb246IHVzZVRyaXRvbkNvbmRpdGlvblxuICAgIH0pO1xuXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1RyaXRvblNlcnZpY2VOYW1lJywge1xuICAgICAgdmFsdWU6IHNlcnZpY2Uuc2VydmljZU5hbWUsXG4gICAgICBkZXNjcmlwdGlvbjogJ1RyaXRvbiBFQ1MgU2VydmljZSBOYW1lJyxcbiAgICAgIGNvbmRpdGlvbjogdXNlVHJpdG9uQ29uZGl0aW9uXG4gICAgfSk7XG5cbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnR3B1RGFzaGJvYXJkVXJsJywge1xuICAgICAgdmFsdWU6IGBodHRwczovLyR7dGhpcy5yZWdpb259LmNvbnNvbGUuYXdzLmFtYXpvbi5jb20vY2xvdWR3YXRjaC9ob21lP3JlZ2lvbj0ke3RoaXMucmVnaW9ufSNkYXNoYm9hcmRzOm5hbWU9JHtkYXNoYm9hcmQuZGFzaGJvYXJkTmFtZX1gLFxuICAgICAgZGVzY3JpcHRpb246ICdDbG91ZFdhdGNoIEdQVS9Ucml0b24gRGFzaGJvYXJkIFVSTCcsXG4gICAgICBjb25kaXRpb246IHVzZVRyaXRvbkNvbmRpdGlvblxuICAgIH0pO1xuICB9XG59XG4iXX0=
