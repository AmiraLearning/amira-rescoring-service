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
        // Create multiple ASGs for diversified Spot capacity across different instance types
        const { asg: autoScalingGroup, capacityProvider: capacityProvider } = this.createAsgAndCapacityProvider(this, 'GpuG54xlarge', vpc, ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE4), ecsSecurityGroup, instanceRole);
        cluster.addAsgCapacityProvider(capacityProvider);
        const { asg: asgG5xlarge, capacityProvider: cpG5xlarge } = this.createAsgAndCapacityProvider(this, 'GpuG5xlarge', vpc, ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE), ecsSecurityGroup, instanceRole);
        cluster.addAsgCapacityProvider(cpG5xlarge);
        const { asg: asgG52xlarge, capacityProvider: cpG52xlarge } = this.createAsgAndCapacityProvider(this, 'GpuG52xlarge', vpc, ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE2), ecsSecurityGroup, instanceRole);
        cluster.addAsgCapacityProvider(cpG52xlarge);
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
        // Conditionally add Secrets Manager secrets to TLS proxy container using L2 constructs
        const targetCertProvided = new cdk.CfnCondition(this, 'TritonTargetCertProvided', {
            expression: cdk.Fn.conditionNot(cdk.Fn.conditionEquals(tritonTargetCertSecretArnParam.valueAsString, ''))
        });
        // Secrets are handled directly in CloudFormation due to conditional logic requirements
        // Apply condition to the underlying CloudFormation resources for the secrets
        const cfnTaskDef = taskDefinition.node.defaultChild;
        cfnTaskDef.addPropertyOverride('ContainerDefinitions.1.Secrets', cdk.Fn.conditionIf(targetCertProvided.logicalId, [
            { name: 'TLS_CERT', valueFrom: `${tritonTargetCertSecretArnParam.valueAsString}:cert::` },
            { name: 'TLS_KEY', valueFrom: `${tritonTargetCertSecretArnParam.valueAsString}:key::` }
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
        // GPU utilization-based scaling for optimal resource management
        // Scale out when GPU utilization is high to prevent bottlenecks
        // Scale in when GPU utilization is low to reduce costs
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
                { upper: 30, change: -1 } // Remove 1 task when GPU utilization < 30%
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
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiYW1pcmEtbGV0dGVyLXNjb3Jpbmctc3RhY2suanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi9saWIvYW1pcmEtbGV0dGVyLXNjb3Jpbmctc3RhY2sudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6Ijs7O0FBQUEsbUNBQW1DO0FBQ25DLDJDQUEyQztBQUMzQywyQ0FBMkM7QUFDM0MsMkNBQTJDO0FBQzNDLDJDQUEyQztBQUMzQyxpREFBaUQ7QUFDakQsMERBQTBEO0FBQzFELDJEQUEyRDtBQUMzRCw2Q0FBNkM7QUFDN0MseUNBQXlDO0FBQ3pDLDJDQUEyQztBQUMzQyxpREFBaUQ7QUFDakQsaURBQWlEO0FBQ2pELHFFQUFxRTtBQUNyRSxnRUFBZ0U7QUFDaEUsMkNBQTJDO0FBQzNDLDJDQUEyQztBQUMzQywyQ0FBMkM7QUFDM0Msc0RBQXNEO0FBQ3RELGdFQUFnRTtBQUdoRSx3REFBeUQ7QUFFekQsTUFBYSx1QkFBd0IsU0FBUSxHQUFHLENBQUMsS0FBSztJQUM1Qyw0QkFBNEIsQ0FBQyxLQUFnQixFQUFFLEVBQVUsRUFBRSxHQUFhLEVBQUUsWUFBOEIsRUFBRSxhQUFpQyxFQUFFLElBQWU7UUFDbEssTUFBTSxFQUFFLEdBQUcsSUFBSSxHQUFHLENBQUMsY0FBYyxDQUFDLEtBQUssRUFBRSxHQUFHLEVBQUUsZ0JBQWdCLEVBQUU7WUFDOUQsWUFBWTtZQUNaLFlBQVksRUFBRSxHQUFHLENBQUMsaUJBQWlCLENBQUMsWUFBWSxDQUFDLEdBQUcsQ0FBQyxlQUFlLENBQUMsR0FBRyxDQUFDO1lBQ3pFLFFBQVEsRUFBRSxHQUFHLENBQUMsUUFBUSxDQUFDLFFBQVEsRUFBRTtZQUNqQyxhQUFhO1lBQ2IsSUFBSTtZQUNKLGFBQWEsRUFBRSxJQUFJO1lBQ25CLFdBQVcsRUFBRSxFQUFFLFdBQVcsRUFBRSxHQUFHLENBQUMsZUFBZSxDQUFDLFFBQVEsRUFBRSxvQkFBb0IsRUFBRSxHQUFHLENBQUMsd0JBQXdCLENBQUMsSUFBSSxFQUFFO1NBQ3BILENB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WUFDMUIsV0FBVyxFQUFFLHlCQUF5QjtZQUN0QyxTQUFTLEVBQUUsa0JBQWtCO1NBQzlCLENBQUMsQ0FBQztRQUVILElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsaUJBQWlCLEVBQUU7WUFDekMsS0FBSyxFQUFFLFdBQVcsSUFBSSxDQUFDLE1BQU0sa0RBQWtELElBQUksQ0FBQyxNQUFNLG9CQUFvQixTQUFTLENBQUMsYUFBYSxFQUFFO1lBQ3ZJLFdBQVcsRUFBRSxxQ0FBcUM7WUFDbEQsU0FBUyxFQUFFLGtCQUFrQjtTQUM5QixDQUFDLENBQUM7SUFDTCxDQUFDO0NBQ0Y7QUFsaENELDBEQWtoQ0MiLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgKiBhcyBjZGsgZnJvbSAnYXdzLWNkay1saWInO1xuaW1wb3J0ICogYXMgZWMyIGZyb20gJ2F3cy1jZGstbGliL2F3cy1lYzInO1xuaW1wb3J0ICogYXMgZWNzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1lY3MnO1xuaW1wb3J0ICogYXMgZWNyIGZyb20gJ2F3cy1jZGstbGliL2F3cy1lY3InO1xuaW1wb3J0ICogYXMgaWFtIGZyb20gJ2F3cy1jZGstbGliL2F3cy1pYW0nO1xuaW1wb3J0ICogYXMgZXZlbnRzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1ldmVudHMnO1xuaW1wb3J0ICogYXMgdGFyZ2V0cyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZXZlbnRzLXRhcmdldHMnO1xuaW1wb3J0ICogYXMgYXV0b3NjYWxpbmcgZnJvbSAnYXdzLWNkay1saWIvYXdzLWF1dG9zY2FsaW5nJztcbmltcG9ydCAqIGFzIGxvZ3MgZnJvbSAnYXdzLWNkay1saWIvYXdzLWxvZ3MnO1xuaW1wb3J0ICogYXMgczMgZnJvbSAnYXdzLWNkay1saWIvYXdzLXMzJztcbmltcG9ydCAqIGFzIHNxcyBmcm9tICdhd3MtY2RrLWxpYi9hd3Mtc3FzJztcbmltcG9ydCAqIGFzIGxhbWJkYSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtbGFtYmRhJztcbmltcG9ydCAqIGFzIGN3IGZyb20gJ2F3cy1jZGstbGliL2F3cy1jbG91ZHdhdGNoJztcbmltcG9ydCAqIGFzIGFwcHNjYWxpbmcgZnJvbSAnYXdzLWNkay1saWIvYXdzLWFwcGxpY2F0aW9uYXV0b3NjYWxpbmcnO1xuaW1wb3J0ICogYXMgY3dhY3Rpb25zIGZyb20gJ2F3cy1jZGstbGliL2F3cy1jbG91ZHdhdGNoLWFjdGlvbnMnO1xuaW1wb3J0ICogYXMga21zIGZyb20gJ2F3cy1jZGstbGliL2F3cy1rbXMnO1xuaW1wb3J0ICogYXMgc25zIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zbnMnO1xuaW1wb3J0ICogYXMgc3NtIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zc20nO1xuaW1wb3J0ICogYXMgY3dfZGFzaCBmcm9tICdhd3MtY2RrLWxpYi9hd3MtY2xvdWR3YXRjaCc7XG5pbXBvcnQgKiBhcyBlbGJ2MiBmcm9tICdhd3MtY2RrLWxpYi9hd3MtZWxhc3RpY2xvYWRiYWxhbmNpbmd2Mic7XG5pbXBvcnQgKiBhcyBhY20gZnJvbSAnYXdzLWNkay1saWIvYXdzLWNlcnRpZmljYXRlbWFuYWdlcic7XG5pbXBvcnQgeyBDb25zdHJ1Y3QgfSBmcm9tICdjb25zdHJ1Y3RzJztcbmltcG9ydCBjd0FnZW50Q29uZmlnID0gcmVxdWlyZSgnLi9jdy1hZ2VudC1jb25maWcuanNvbicpO1xuXG5leHBvcnQgY2xhc3MgQW1pcmFMZXR0ZXJTY29yaW5nU3RhY2sgZXh0ZW5kcyBjZGsuU3RhY2sge1xuICBwcml2YXRlIGNyZWF0ZUFzZ0FuZENhcGFjaXR5UHJvdmlkZXIoc2NvcGU6IENvbnN0cnVjdCwgaWQ6IHN0cmluZywgdnBjOiBlYzIuSVZwYywgaW5zdGFuY2VUeXBlOiBlYzIuSW5zdGFuY2VUeXBlLCBzZWN1cml0eUdyb3VwOiBlYzIuSVNlY3VyaXR5R3JvdXAsIHJvbGU6IGlhbS5JUm9sZSk6IHsgYXNnOiBhdXRvc2NhbGluZy5BdXRvU2NhbGluZ0dyb3VwOyBjYXBhY2l0eVByb3ZpZGVyOiBlY3MuQXNnQ2FwYWNpdHlQcm92aWRlciB9IHtcbiAgICBjb25zdCBsdCA9IG5ldyBlYzIuTGF1bmNoVGVtcGxhdGUoc2NvcGUsIGAke2lkfUxhdW5jaFRlbXBsYXRlYCwge1xuICAgICAgaW5zdGFuY2VUeXBlLFxuICAgICAgbWFjaGluZUltYWdlOiBlY3MuRWNzT3B0aW1pemVkSW1hZ2UuYW1hem9uTGludXgyKGVjcy5BbWlIYXJkd2FyZVR5cGUuR1BVKSxcbiAgICAgIHVzZXJEYXRhOiBlYzIuVXNlckRhdGEuZm9yTGludXgoKSxcbiAgICAgIHNlY3VyaXR5R3JvdXAsXG4gICAgICByb2xlLFxuICAgICAgcmVxdWlyZUltZHN2MjogdHJ1ZSxcbiAgICAgIHNwb3RPcHRpb25zOiB7IHJlcXVlc3RUeXBlOiBlYzIuU3BvdFJlcXVlc3RUeXBlLk9ORV9USU1FLCBpbnRlcnJ1cHRpb25CZWhhdmlvcjogZWMyLlNwb3RJbnN0YW5jZUludGVycnVwdGlvbi5TVE9QIH1cbiAgICB9KTtcblxuICAgIGNvbnN0IGFzZyA9IG5ldyBhdXRvc2NhbGluZy5BdXRvU2NhbGluZ0dyb3VwKHNjb3BlLCBgJHtpZH1Bc2dgLCB7XG4gICAgICB2cGMsXG4gICAgICBsYXVuY2hUZW1wbGF0ZTogbHQsXG4gICAgICBtaW5DYXBhY2l0eTogMCxcbiAgICAgIG1heENhcGFjaXR5OiAxMCxcbiAgICAgIGRlc2lyZWRDYXBhY2l0eTogMCxcbiAgICAgIHZwY1N1Ym5ldHM6IHsgc3VibmV0VHlwZTogZWMyLlN1Ym5ldFR5cGUuUFJJVkFURV9XSVRIX0VHUkVTUyB9LFxuICAgICAgY2FwYWNpdHlSZWJhbGFuY2U6IHRydWVcbiAgICB9KTtcblxuICAgIGNvbnN0IGNhcGFjaXR5UHJvdmlkZXIgPSBuZXcgZWNzLkFzZ0NhcGFjaXR5UHJvdmlkZXIoc2NvcGUsIGAke2lkfUNhcGFjaXR5UHJvdmlkZXJgLCB7XG4gICAgICBhdXRvU2NhbGluZ0dyb3VwOiBhc2csXG4gICAgICBlbmFibGVNYW5hZ2VkU2NhbGluZzogdHJ1ZSxcbiAgICAgIGVuYWJsZU1hbmFnZWRUZXJtaW5hdGlvblByb3RlY3Rpb246IHRydWUsXG4gICAgICB0YXJnZXRDYXBhY2l0eVBlcmNlbnQ6IDEwMCxcbiAgICAgIG1hY2hpbmVJbWFnZVR5cGU6IGVjcy5NYWNoaW5lSW1hZ2VUeXBlLkFNQVpPTl9MSU5VWF8yXG4gICAgfSk7XG4gICAgcmV0dXJuIHsgYXNnLCBjYXBhY2l0eVByb3ZpZGVyIH07XG4gIH1cbiAgY29uc3RydWN0b3Ioc2NvcGU6IENvbnN0cnVjdCwgaWQ6IHN0cmluZywgcHJvcHM/OiBjZGsuU3RhY2tQcm9wcykge1xuICAgIHN1cGVyKHNjb3BlLCBpZCwgcHJvcHMpO1xuXG4gICAgLy8gRUNSIFJlcG9zaXRvcmllcyBmb3IgY29udGFpbmVyc1xuICAgIGNvbnN0IHJlcG9zaXRvcnkgPSBuZXcgZWNyLlJlcG9zaXRvcnkodGhpcywgJ0FtaXJhTGV0dGVyU2NvcmluZ1JlcG8nLCB7XG4gICAgICByZXBvc2l0b3J5TmFtZTogJ2FtaXJhLWxldHRlci1zY29yaW5nJyxcbiAgICAgIGltYWdlU2Nhbk9uUHVzaDogdHJ1ZSxcbiAgICAgIGxpZmVjeWNsZVJ1bGVzOiBbe1xuICAgICAgICBtYXhJbWFnZUNvdW50OiAxMCxcbiAgICAgICAgZGVzY3JpcHRpb246ICdLZWVwIG9ubHkgMTAgbW9zdCByZWNlbnQgaW1hZ2VzJ1xuICAgICAgfV1cbiAgICB9KTtcblxuICAgIC8vIEVDUiByZXBvc2l0b3JpZXMgZm9yIFRyaXRvbiBHUFUgY2x1c3RlciAoY29uZGl0aW9uYWwpXG4gICAgY29uc3QgdHJpdG9uUmVwb3NpdG9yeSA9IG5ldyBlY3IuUmVwb3NpdG9yeSh0aGlzLCAnVHJpdG9uU2VydmVyUmVwbycsIHtcbiAgICAgIHJlcG9zaXRvcnlOYW1lOiAndHJpdG9uLXNlcnZlcicsXG4gICAgICBpbWFnZVNjYW5PblB1c2g6IHRydWUsXG4gICAgICBsaWZlY3ljbGVSdWxlczogW3sgbWF4SW1hZ2VDb3VudDogNSB9XVxuICAgIH0pO1xuICAgIGNvbnN0IGN3QWdlbnRSZXBvc2l0b3J5ID0gbmV3IGVjci5SZXBvc2l0b3J5KHRoaXMsICdDbG91ZFdhdGNoQWdlbnRSZXBvJywge1xuICAgICAgcmVwb3NpdG9yeU5hbWU6ICdjbG91ZHdhdGNoLWFnZW50JyxcbiAgICAgIGltYWdlU2Nhbk9uUHVzaDogdHJ1ZSxcbiAgICAgIGxpZmVjeWNsZVJ1bGVzOiBbeyBtYXhJbWFnZUNvdW50OiA1IH1dXG4gICAgfSk7XG4gICAgY29uc3QgZGNnbUV4cG9ydGVyUmVwb3NpdG9yeSA9IG5ldyBlY3IuUmVwb3NpdG9yeSh0aGlzLCAnRGNnbUV4cG9ydGVyUmVwbycsIHtcbiAgICAgIHJlcG9zaXRvcnlOYW1lOiAnZGNnbS1leHBvcnRlcicsXG4gICAgICBpbWFnZVNjYW5PblB1c2g6IHRydWUsXG4gICAgICBsaWZlY3ljbGVSdWxlczogW3sgbWF4SW1hZ2VDb3VudDogNSB9XVxuICAgIH0pO1xuXG4gICAgLy8gUGFyYW1ldGVycyBmb3IgcnVudGltZSBjb25maWd1cmF0aW9uXG4gICAgY29uc3QgYXBwSW1hZ2VUYWdQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBcHBJbWFnZVRhZycsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3YwLjAuMCcsXG4gICAgICBkZXNjcmlwdGlvbjogJ0VDUiBpbWFnZSB0YWcgZm9yIGFwcGxpY2F0aW9uIGNvbnRhaW5lcidcbiAgICB9KTtcbiAgICBjb25zdCB0cml0b25JbWFnZVRhZ1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1RyaXRvbkltYWdlVGFnJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAndjAuMC4wJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRUNSIGltYWdlIHRhZyBmb3IgVHJpdG9uIGNvbnRhaW5lcidcbiAgICB9KTtcbiAgICBjb25zdCBjd0FnZW50SW1hZ2VUYWdQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdDd0FnZW50SW1hZ2VUYWcnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdsYXRlc3QnLFxuICAgICAgZGVzY3JpcHRpb246ICdFQ1IgaW1hZ2UgdGFnIGZvciBDbG91ZFdhdGNoIEFnZW50IGNvbnRhaW5lcidcbiAgICB9KTtcbiAgICBjb25zdCBkY2dtSW1hZ2VUYWdQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdEY2dtSW1hZ2VUYWcnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdsYXRlc3QnLFxuICAgICAgZGVzY3JpcHRpb246ICdFQ1IgaW1hZ2UgdGFnIGZvciBEQ0dNIGV4cG9ydGVyIGNvbnRhaW5lcidcbiAgICB9KTtcbiAgICBjb25zdCBtb2RlbFBhdGhQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdNb2RlbFBhdGgnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdmYWNlYm9vay93YXYydmVjMi1iYXNlLTk2MGgnLFxuICAgICAgZGVzY3JpcHRpb246ICdIRiBtb2RlbCBwYXRoIGZvciBXYXYyVmVjMidcbiAgICB9KTtcbiAgICBjb25zdCBpbmNsdWRlQ29uZmlkZW5jZVBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0luY2x1ZGVDb25maWRlbmNlJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAndHJ1ZScsXG4gICAgICBhbGxvd2VkVmFsdWVzOiBbJ3RydWUnLCAnZmFsc2UnXSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnV2hldGhlciB0byBjb21wdXRlIGNvbmZpZGVuY2UgaW4gd29ya2VyJ1xuICAgIH0pO1xuICAgIGNvbnN0IGF1ZGlvRGlyUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXVkaW9EaXInLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcvdG1wL2F1ZGlvJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnTG9jYWwgYXVkaW8gd29ya2luZyBkaXJlY3RvcnkgaW5zaWRlIGNvbnRhaW5lcidcbiAgICB9KTtcbiAgICBjb25zdCByZXN1bHRzUHJlZml4UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnUmVzdWx0c1ByZWZpeCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ3Jlc3VsdHMvJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnUzMga2V5IHByZWZpeCBmb3IgcmVzdWx0cyB3cml0ZXMnXG4gICAgfSk7XG5cbiAgICBjb25zdCBuYXRHYXRld2F5Q291bnRQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdOYXRHYXRld2F5Q291bnQnLCB7XG4gICAgICB0eXBlOiAnTnVtYmVyJyxcbiAgICAgIGRlZmF1bHQ6IDIsXG4gICAgICBkZXNjcmlwdGlvbjogJ051bWJlciBvZiBOQVQgR2F0ZXdheXMgdG8gY3JlYXRlIChzZXQgMCB0byBzYXZlIGNvc3Qgd2l0aCBWUEMgZW5kcG9pbnRzKSdcbiAgICB9KTtcblxuICAgIGNvbnN0IGF0aGVuYURiUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXRoZW5hRGF0YWJhc2UnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICdkZWZhdWx0JyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnQXRoZW5hIGRhdGFiYXNlIG5hbWUnXG4gICAgfSk7XG4gICAgY29uc3QgYXRoZW5hT3V0cHV0UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXRoZW5hT3V0cHV0Jywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnczM6Ly9hdGhlbmEtcXVlcnktcmVzdWx0cy8nLFxuICAgICAgZGVzY3JpcHRpb246ICdBdGhlbmEgcXVlcnkgb3V0cHV0IFMzIHBhdGgnXG4gICAgfSk7XG4gICAgY29uc3QgYXRoZW5hUXVlcnlQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFRdWVyeScsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ1NFTEVDVCBhY3Rpdml0eV9pZCBGUk9NIGFjdGl2aXRpZXMgV0hFUkUgcHJvY2Vzc19mbGFnID0gMScsXG4gICAgICBkZXNjcmlwdGlvbjogJ0F0aGVuYSBTUUwgdG8gcHJvZHVjZSBhY3Rpdml0eSBJRHMnXG4gICAgfSk7XG4gICAgY29uc3QgYXRoZW5hVGFibGVQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFUYWJsZScsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIHRhYmxlIG5hbWUgZm9yIGR5bmFtaWMgcXVlcnkgYnVpbGRpbmcnXG4gICAgfSk7XG4gICAgY29uc3QgYXRoZW5hV2hlcmVQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdBdGhlbmFXaGVyZScsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIFdIRVJFIGNsYXVzZSAod2l0aG91dCBXSEVSRSBrZXl3b3JkKSdcbiAgICB9KTtcbiAgICBjb25zdCBhdGhlbmFMaW1pdFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F0aGVuYUxpbWl0Jywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnT3B0aW9uYWwgTElNSVQgdmFsdWUgZm9yIHRoZSBxdWVyeSdcbiAgICB9KTtcbiAgICBjb25zdCBhdGhlbmFDb2x1bW5zUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQXRoZW5hQ29sdW1ucycsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2FjdGl2aXR5X2lkJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnT3B0aW9uYWwgY29sdW1ucyB0byBzZWxlY3QgZm9yIGR5bmFtaWMgcXVlcnknXG4gICAgfSk7XG5cbiAgICAvLyBUcml0b24gaW5mZXJlbmNlIGZlYXR1cmUgZmxhZ1xuICAgIGNvbnN0IHVzZVRyaXRvblBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ1VzZVRyaXRvbicsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2ZhbHNlJyxcbiAgICAgIGFsbG93ZWRWYWx1ZXM6IFsndHJ1ZScsICdmYWxzZSddLFxuICAgICAgZGVzY3JpcHRpb246ICdXaGV0aGVyIHRvIGVuYWJsZSBUcml0b24gaW5mZXJlbmNlIHNlcnZlciB3aXRoIEdQVSByZXNvdXJjZXMnXG4gICAgfSk7XG5cbiAgICBjb25zdCB0cml0b25DZXJ0QXJuUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnVHJpdG9uQ2VydGlmaWNhdGVBcm4nLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdBQ00gY2VydGlmaWNhdGUgQVJOIGZvciBIVFRQUyBvbiB0aGUgVHJpdG9uIEFMQiAocmVxdWlyZWQgZm9yIFRMUyknXG4gICAgfSk7XG5cbiAgICAvLyBPcHRpb25hbCBUTFMgY2VydC9rZXkgZm9yIHRhcmdldCBzaWRlY2FyIHZpYSBTZWNyZXRzIE1hbmFnZXIgKGV4cGVjdHMgSlNPTiB3aXRoIGZpZWxkczogY2VydCwga2V5KVxuICAgIGNvbnN0IHRyaXRvblRhcmdldENlcnRTZWNyZXRBcm5QYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdUcml0b25UYXJnZXRDZXJ0U2VjcmV0QXJuJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnT3B0aW9uYWwgU2VjcmV0cyBNYW5hZ2VyIEFSTiBjb250YWluaW5nIEpTT04gd2l0aCBmaWVsZHMge1wiY2VydFwiLFwia2V5XCJ9IGZvciBzaWRlY2FyIFRMUydcbiAgICB9KTtcblxuICAgIC8vIFRMUyB0dW5pbmcgZm9yIHNpZGVjYXJcbiAgICBjb25zdCBlbmFibGVUYXJnZXRIdHRwMlBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0VuYWJsZVRhcmdldEh0dHAyJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAndHJ1ZScsXG4gICAgICBhbGxvd2VkVmFsdWVzOiBbJ3RydWUnLCAnZmFsc2UnXSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRW5hYmxlIEhUVFAvMiBvbiBUTFMgc2lkZWNhciBsaXN0ZW5lcidcbiAgICB9KTtcbiAgICBjb25zdCB0YXJnZXRTc2xDaXBoZXJzUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnVGFyZ2V0U3NsQ2lwaGVycycsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIE9wZW5TU0wgY2lwaGVyIHN1aXRlIHN0cmluZyBmb3IgVExTIHNpZGVjYXIgKGVtcHR5IGZvciBkZWZhdWx0KSdcbiAgICB9KTtcblxuICAgIC8vIE9wdGlvbmFsIEF1ZGlvIGJ1Y2tldCBmb3IgcmVhZC1vbmx5IGFjY2Vzc1xuICAgIGNvbnN0IGF1ZGlvQnVja2V0TmFtZVBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F1ZGlvQnVja2V0TmFtZScsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIFMzIGJ1Y2tldCBuYW1lIGZvciBpbnB1dCBhdWRpbyAocmVhZC1vbmx5KS4gTGVhdmUgYmxhbmsgdG8gc2tpcC4nXG4gICAgfSk7XG5cbiAgICAvLyBUcml0b24gVVJMIHBhcmFtZXRlciBmb3IgcmVtb3RlIGluZmVyZW5jZSAod2hlbiBVc2VUcml0b249ZmFsc2UgYnV0IHdhbnQgdG8gY2FsbCBleHRlcm5hbCBUcml0b24pXG4gICAgY29uc3QgdHJpdG9uU2VydmVyVXJsUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnVHJpdG9uU2VydmVyVXJsJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnT3B0aW9uYWwgZXh0ZXJuYWwgVHJpdG9uIHNlcnZlciBVUkwgZm9yIHJlbW90ZSBpbmZlcmVuY2UuIExlYXZlIGJsYW5rIGZvciBsb2NhbCBzaWRlY2FyLidcbiAgICB9KTtcbiAgICBjb25zdCBhdWRpb0J1Y2tldFByZWZpeFBhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0F1ZGlvUHJlZml4Jywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAnJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnT3B0aW9uYWwgUzMga2V5IHByZWZpeCB3aXRoaW4gdGhlIGF1ZGlvIGJ1Y2tldC4nXG4gICAgfSk7XG5cbiAgICAvLyBWUEMgZm9yIHRoZSBFQ1MgY2x1c3RlclxuICAgIGNvbnN0IHZwYyA9IG5ldyBlYzIuVnBjKHRoaXMsICdBbWlyYUxldHRlclNjb3JpbmdWcGMnLCB7XG4gICAgICBtYXhBenM6IDIsXG4gICAgICBuYXRHYXRld2F5czogbmF0R2F0ZXdheUNvdW50UGFyYW0udmFsdWVBc051bWJlcixcbiAgICAgIHN1Ym5ldENvbmZpZ3VyYXRpb246IFtcbiAgICAgICAge1xuICAgICAgICAgIGNpZHJNYXNrOiAyNCxcbiAgICAgICAgICBuYW1lOiAnUHVibGljJyxcbiAgICAgICAgICBzdWJuZXRUeXBlOiBlYzIuU3VibmV0VHlwZS5QVUJMSUMsXG4gICAgICAgIH0sXG4gICAgICAgIHtcbiAgICAgICAgICBjaWRyTWFzazogMjQsXG4gICAgICAgICAgbmFtZTogJ1ByaXZhdGUnLFxuICAgICAgICAgIHN1Ym5ldFR5cGU6IGVjMi5TdWJuZXRUeXBlLlBSSVZBVEVfV0lUSF9FR1JFU1MsXG4gICAgICAgIH1cbiAgICAgIF1cbiAgICB9KTtcblxuICAgIC8vIFRvZ2dsZSBmb3IgSW50ZXJmYWNlIFZQQyBFbmRwb2ludHMgKGNhbiBiZSBkaXNhYmxlZCB0byByZWR1Y2UgY29zdHMpXG4gICAgY29uc3QgZW5hYmxlSW50ZXJmYWNlRW5kcG9pbnRzUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnRW5hYmxlSW50ZXJmYWNlRW5kcG9pbnRzJywge1xuICAgICAgdHlwZTogJ1N0cmluZycsXG4gICAgICBkZWZhdWx0OiAndHJ1ZScsXG4gICAgICBhbGxvd2VkVmFsdWVzOiBbJ3RydWUnLCAnZmFsc2UnXSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRW5hYmxlIGNyZWF0aW9uIG9mIEludGVyZmFjZSBWUEMgRW5kcG9pbnRzIChFQ1IsIENXIExvZ3MsIFNRUywgU1NNLCBTVFMsIFNlY3JldHMsIEtNUyknXG4gICAgfSk7XG4gICAgY29uc3QgZW5kcG9pbnRzRW5hYmxlZCA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdJbnRlcmZhY2VFbmRwb2ludHNFbmFibGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbkVxdWFscyhlbmFibGVJbnRlcmZhY2VFbmRwb2ludHNQYXJhbS52YWx1ZUFzU3RyaW5nLCAndHJ1ZScpXG4gICAgfSk7XG5cbiAgICAvLyBWUEMgRW5kcG9pbnRzIHRvIHJlZHVjZSBOQVQgZWdyZXNzXG4gICAgdnBjLmFkZEdhdGV3YXlFbmRwb2ludCgnUzNFbmRwb2ludCcsIHtcbiAgICAgIHNlcnZpY2U6IGVjMi5HYXRld2F5VnBjRW5kcG9pbnRBd3NTZXJ2aWNlLlMzLFxuICAgICAgc3VibmV0czogW3sgc3VibmV0czogdnBjLnByaXZhdGVTdWJuZXRzIH1dXG4gICAgfSk7XG4gICAgY29uc3QgZWNyQXBpRXAgPSB2cGMuYWRkSW50ZXJmYWNlRW5kcG9pbnQoJ0VjckFwaUVuZHBvaW50Jywge1xuICAgICAgc2VydmljZTogZWMyLkludGVyZmFjZVZwY0VuZHBvaW50QXdzU2VydmljZS5FQ1IsXG4gICAgICBzdWJuZXRzOiB7IHN1Ym5ldHM6IHZwYy5wcml2YXRlU3VibmV0cyB9XG4gICAgfSk7XG4gICAgY29uc3QgZWNyRG9ja2VyRXAgPSB2cGMuYWRkSW50ZXJmYWNlRW5kcG9pbnQoJ0VjckRvY2tlckVuZHBvaW50Jywge1xuICAgICAgc2VydmljZTogZWMyLkludGVyZmFjZVZwY0VuZHBvaW50QXdzU2VydmljZS5FQ1JfRE9DS0VSLFxuICAgICAgc3VibmV0czogeyBzdWJuZXRzOiB2cGMucHJpdmF0ZVN1Ym5ldHMgfVxuICAgIH0pO1xuICAgIGNvbnN0IGN3TG9nc0VwID0gdnBjLmFkZEludGVyZmFjZUVuZHBvaW50KCdDbG91ZFdhdGNoTG9nc0VuZHBvaW50Jywge1xuICAgICAgc2VydmljZTogZWMyLkludGVyZmFjZVZwY0VuZHBvaW50QXdzU2VydmljZS5DTE9VRFdBVENIX0xPR1MsXG4gICAgICBzdWJuZXRzOiB7IHN1Ym5ldHM6IHZwYy5wcml2YXRlU3VibmV0cyB9XG4gICAgfSk7XG4gICAgY29uc3Qgc3FzRXAgPSB2cGMuYWRkSW50ZXJmYWNlRW5kcG9pbnQoJ1Nxc0VuZHBvaW50Jywge1xuICAgICAgc2VydmljZTogZWMyLkludGVyZmFjZVZwY0VuZHBvaW50QXdzU2VydmljZS5TUVMsXG4gICAgICBzdWJuZXRzOiB7IHN1Ym5ldHM6IHZwYy5wcml2YXRlU3VibmV0cyB9XG4gICAgfSk7XG4gICAgY29uc3Qgc3NtRXAgPSB2cGMuYWRkSW50ZXJmYWNlRW5kcG9pbnQoJ1NzbUVuZHBvaW50Jywge1xuICAgICAgc2VydmljZTogZWMyLkludGVyZmFjZVZwY0VuZHBvaW50QXdzU2VydmljZS5TU00sXG4gICAgICBzdWJuZXRzOiB7IHN1Ym5ldHM6IHZwYy5wcml2YXRlU3VibmV0cyB9XG4gICAgfSk7XG4gICAgY29uc3Qgc3NtTXNnc0VwID0gdnBjLmFkZEludGVyZmFjZUVuZHBvaW50KCdTc21NZXNzYWdlc0VuZHBvaW50Jywge1xuICAgICAgc2VydmljZTogZWMyLkludGVyZmFjZVZwY0VuZHBvaW50QXdzU2VydmljZS5TU01fTUVTU0FHRVMsXG4gICAgICBzdWJuZXRzOiB7IHN1Ym5ldHM6IHZwYy5wcml2YXRlU3VibmV0cyB9XG4gICAgfSk7XG4gICAgY29uc3QgZWMyTXNnc0VwID0gdnBjLmFkZEludGVyZmFjZUVuZHBvaW50KCdFYzJNZXNzYWdlc0VuZHBvaW50Jywge1xuICAgICAgc2VydmljZTogZWMyLkludGVyZmFjZVZwY0VuZHBvaW50QXdzU2VydmljZS5FQzJfTUVTU0FHRVMsXG4gICAgICBzdWJuZXRzOiB7IHN1Ym5ldHM6IHZwYy5wcml2YXRlU3VibmV0cyB9XG4gICAgfSk7XG4gICAgY29uc3Qgc3RzRXAgPSB2cGMuYWRkSW50ZXJmYWNlRW5kcG9pbnQoJ1N0c0VuZHBvaW50Jywge1xuICAgICAgc2VydmljZTogZWMyLkludGVyZmFjZVZwY0VuZHBvaW50QXdzU2VydmljZS5TVFMsXG4gICAgICBzdWJuZXRzOiB7IHN1Ym5ldHM6IHZwYy5wcml2YXRlU3VibmV0cyB9XG4gICAgfSk7XG4gICAgY29uc3Qgc2VjcmV0c0VwID0gdnBjLmFkZEludGVyZmFjZUVuZHBvaW50KCdTZWNyZXRzTWFuYWdlckVuZHBvaW50Jywge1xuICAgICAgc2VydmljZTogZWMyLkludGVyZmFjZVZwY0VuZHBvaW50QXdzU2VydmljZS5TRUNSRVRTX01BTkFHRVIsXG4gICAgICBzdWJuZXRzOiB7IHN1Ym5ldHM6IHZwYy5wcml2YXRlU3VibmV0cyB9XG4gICAgfSk7XG4gICAgY29uc3Qga21zRXAgPSB2cGMuYWRkSW50ZXJmYWNlRW5kcG9pbnQoJ0ttc0VuZHBvaW50Jywge1xuICAgICAgc2VydmljZTogZWMyLkludGVyZmFjZVZwY0VuZHBvaW50QXdzU2VydmljZS5LTVMsXG4gICAgICBzdWJuZXRzOiB7IHN1Ym5ldHM6IHZwYy5wcml2YXRlU3VibmV0cyB9XG4gICAgfSk7XG5cbiAgICAoZWNyQXBpRXAubm9kZS5kZWZhdWx0Q2hpbGQgYXMgZWMyLkNmblZQQ0VuZHBvaW50KS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGVuZHBvaW50c0VuYWJsZWQ7XG4gICAgKGVjckRvY2tlckVwLm5vZGUuZGVmYXVsdENoaWxkIGFzIGVjMi5DZm5WUENFbmRwb2ludCkuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBlbmRwb2ludHNFbmFibGVkO1xuICAgIChjd0xvZ3NFcC5ub2RlLmRlZmF1bHRDaGlsZCBhcyBlYzIuQ2ZuVlBDRW5kcG9pbnQpLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gZW5kcG9pbnRzRW5hYmxlZDtcbiAgICAoc3FzRXAubm9kZS5kZWZhdWx0Q2hpbGQgYXMgZWMyLkNmblZQQ0VuZHBvaW50KS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGVuZHBvaW50c0VuYWJsZWQ7XG4gICAgKHNzbUVwLm5vZGUuZGVmYXVsdENoaWxkIGFzIGVjMi5DZm5WUENFbmRwb2ludCkuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBlbmRwb2ludHNFbmFibGVkO1xuICAgIChzc21Nc2dzRXAubm9kZS5kZWZhdWx0Q2hpbGQgYXMgZWMyLkNmblZQQ0VuZHBvaW50KS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGVuZHBvaW50c0VuYWJsZWQ7XG4gICAgKGVjMk1zZ3NFcC5ub2RlLmRlZmF1bHRDaGlsZCBhcyBlYzIuQ2ZuVlBDRW5kcG9pbnQpLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gZW5kcG9pbnRzRW5hYmxlZDtcbiAgICAoc3RzRXAubm9kZS5kZWZhdWx0Q2hpbGQgYXMgZWMyLkNmblZQQ0VuZHBvaW50KS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGVuZHBvaW50c0VuYWJsZWQ7XG4gICAgKHNlY3JldHNFcC5ub2RlLmRlZmF1bHRDaGlsZCBhcyBlYzIuQ2ZuVlBDRW5kcG9pbnQpLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gZW5kcG9pbnRzRW5hYmxlZDtcbiAgICAoa21zRXAubm9kZS5kZWZhdWx0Q2hpbGQgYXMgZWMyLkNmblZQQ0VuZHBvaW50KS5jZm5PcHRpb25zLmNvbmRpdGlvbiA9IGVuZHBvaW50c0VuYWJsZWQ7XG5cbiAgICAvLyBTZWN1cml0eSBncm91cHMgc3BsaXQ6IEFMQiBhbmQgRUNTIHRhc2tzXG4gICAgY29uc3QgYWxiU2VjdXJpdHlHcm91cCA9IG5ldyBlYzIuU2VjdXJpdHlHcm91cCh0aGlzLCAnQW1pcmFBbGJTZWN1cml0eUdyb3VwJywge1xuICAgICAgdnBjLFxuICAgICAgZGVzY3JpcHRpb246ICdTZWN1cml0eSBncm91cCBmb3IgaW50ZXJuYWwgQUxCIGZyb250aW5nIFRyaXRvbiBUTFMgcHJveHknLFxuICAgICAgYWxsb3dBbGxPdXRib3VuZDogdHJ1ZVxuICAgIH0pO1xuICAgIC8vIFJlc3RyaWN0IEFMQiBpbmdyZXNzOiBhbGxvdyBlaXRoZXIgZnJvbSBhIHNwZWNpZmljIGNsaWVudCBTRywgb3IgZmFsbGJhY2sgdG8gVlBDIENJRFJcbiAgICBjb25zdCBhbGJDbGllbnRTZ1BhcmFtID0gbmV3IGNkay5DZm5QYXJhbWV0ZXIodGhpcywgJ0FsYkNsaWVudFNlY3VyaXR5R3JvdXBJZCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIFNlY3VyaXR5IEdyb3VwIElEIGFsbG93ZWQgdG8gYWNjZXNzIEFMQjo0NDMuIExlYXZlIGJsYW5rIHRvIGRlZmF1bHQgdG8gVlBDIENJRFIuJ1xuICAgIH0pO1xuICAgIGNvbnN0IGNsaWVudFNnUHJvdmlkZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnQWxiQ2xpZW50U2dQcm92aWRlZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25Ob3QoY2RrLkZuLmNvbmRpdGlvbkVxdWFscyhhbGJDbGllbnRTZ1BhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSlcbiAgICB9KTtcbiAgICBjb25zdCBpbmdyZXNzRnJvbUNsaWVudFNnID0gbmV3IGVjMi5DZm5TZWN1cml0eUdyb3VwSW5ncmVzcyh0aGlzLCAnQWxiSW5ncmVzc0Zyb21DbGllbnRTZycsIHtcbiAgICAgIGdyb3VwSWQ6IGFsYlNlY3VyaXR5R3JvdXAuc2VjdXJpdHlHcm91cElkLFxuICAgICAgaXBQcm90b2NvbDogJ3RjcCcsXG4gICAgICBmcm9tUG9ydDogNDQzLFxuICAgICAgdG9Qb3J0OiA0NDMsXG4gICAgICBzb3VyY2VTZWN1cml0eUdyb3VwSWQ6IGFsYkNsaWVudFNnUGFyYW0udmFsdWVBc1N0cmluZ1xuICAgIH0pO1xuICAgIGluZ3Jlc3NGcm9tQ2xpZW50U2cuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBjbGllbnRTZ1Byb3ZpZGVkO1xuICAgIGNvbnN0IGluZ3Jlc3NGcm9tVnBjQ2lkciA9IG5ldyBlYzIuQ2ZuU2VjdXJpdHlHcm91cEluZ3Jlc3ModGhpcywgJ0FsYkluZ3Jlc3NGcm9tVnBjQ2lkcicsIHtcbiAgICAgIGdyb3VwSWQ6IGFsYlNlY3VyaXR5R3JvdXAuc2VjdXJpdHlHcm91cElkLFxuICAgICAgaXBQcm90b2NvbDogJ3RjcCcsXG4gICAgICBmcm9tUG9ydDogNDQzLFxuICAgICAgdG9Qb3J0OiA0NDMsXG4gICAgICBjaWRySXA6IHZwYy52cGNDaWRyQmxvY2tcbiAgICB9KTtcbiAgICBpbmdyZXNzRnJvbVZwY0NpZHIuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnQWxiQ2xpZW50U2dOb3RQcm92aWRlZCcsIHtcbiAgICAgIGV4cHJlc3Npb246IGNkay5Gbi5jb25kaXRpb25FcXVhbHMoYWxiQ2xpZW50U2dQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJylcbiAgICB9KTtcblxuICAgIGNvbnN0IGVjc1NlY3VyaXR5R3JvdXAgPSBuZXcgZWMyLlNlY3VyaXR5R3JvdXAodGhpcywgJ0FtaXJhRWNzU2VjdXJpdHlHcm91cCcsIHtcbiAgICAgIHZwYyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnU2VjdXJpdHkgZ3JvdXAgZm9yIEFtaXJhIExldHRlciBTY29yaW5nIEVDUyB0YXNrcycsXG4gICAgICBhbGxvd0FsbE91dGJvdW5kOiB0cnVlXG4gICAgfSk7XG4gICAgZWNzU2VjdXJpdHlHcm91cC5hZGRJbmdyZXNzUnVsZShhbGJTZWN1cml0eUdyb3VwLCBlYzIuUG9ydC50Y3AoODQ0MyksICdBbGxvdyBBTEIgdG8gcmVhY2ggVExTIHByb3h5IG92ZXIgODQ0MycpO1xuXG4gICAgLy8gRUNTIENsdXN0ZXIgd2l0aCBHUFUgaW5zdGFuY2VzXG4gICAgY29uc3QgY2x1c3RlciA9IG5ldyBlY3MuQ2x1c3Rlcih0aGlzLCAnQW1pcmFMZXR0ZXJTY29yaW5nQ2x1c3RlcicsIHtcbiAgICAgIHZwYyxcbiAgICAgIGNsdXN0ZXJOYW1lOiAnYW1pcmEtbGV0dGVyLXNjb3JpbmctY2x1c3RlcicsXG4gICAgICBjb250YWluZXJJbnNpZ2h0czogdHJ1ZVxuICAgIH0pO1xuXG4gICAgLy8gRUMyIGluc3RhbmNlIHJvbGUgZm9yIEVDUyBjbHVzdGVyIGluc3RhbmNlc1xuICAgIGNvbnN0IGluc3RhbmNlUm9sZSA9IG5ldyBpYW0uUm9sZSh0aGlzLCAnR3B1SW5zdGFuY2VSb2xlJywge1xuICAgICAgYXNzdW1lZEJ5OiBuZXcgaWFtLlNlcnZpY2VQcmluY2lwYWwoJ2VjMi5hbWF6b25hd3MuY29tJylcbiAgICB9KTtcbiAgICBpbnN0YW5jZVJvbGUuYWRkTWFuYWdlZFBvbGljeShpYW0uTWFuYWdlZFBvbGljeS5mcm9tQXdzTWFuYWdlZFBvbGljeU5hbWUoJ3NlcnZpY2Utcm9sZS9BbWF6b25FQzJDb250YWluZXJTZXJ2aWNlZm9yRUMyUm9sZScpKTtcbiAgICBpbnN0YW5jZVJvbGUuYWRkTWFuYWdlZFBvbGljeShpYW0uTWFuYWdlZFBvbGljeS5mcm9tQXdzTWFuYWdlZFBvbGljeU5hbWUoJ0FtYXpvblNTTU1hbmFnZWRJbnN0YW5jZUNvcmUnKSk7XG5cbiAgICAvLyBDcmVhdGUgbXVsdGlwbGUgQVNHcyBmb3IgZGl2ZXJzaWZpZWQgU3BvdCBjYXBhY2l0eSBhY3Jvc3MgZGlmZmVyZW50IGluc3RhbmNlIHR5cGVzXG4gICAgY29uc3QgeyBhc2c6IGF1dG9TY2FsaW5nR3JvdXAsIGNhcGFjaXR5UHJvdmlkZXI6IGNhcGFjaXR5UHJvdmlkZXIgfSA9IHRoaXMuY3JlYXRlQXNnQW5kQ2FwYWNpdHlQcm92aWRlcih0aGlzLCAnR3B1RzU0eGxhcmdlJywgdnBjLCBlYzIuSW5zdGFuY2VUeXBlLm9mKGVjMi5JbnN0YW5jZUNsYXNzLkc1LCBlYzIuSW5zdGFuY2VTaXplLlhMQVJHRTQpLCBlY3NTZWN1cml0eUdyb3VwLCBpbnN0YW5jZVJvbGUpO1xuICAgIGNsdXN0ZXIuYWRkQXNnQ2FwYWNpdHlQcm92aWRlcihjYXBhY2l0eVByb3ZpZGVyKTtcblxuICAgIGNvbnN0IHsgYXNnOiBhc2dHNXhsYXJnZSwgY2FwYWNpdHlQcm92aWRlcjogY3BHNXhsYXJnZSB9ID0gdGhpcy5jcmVhdGVBc2dBbmRDYXBhY2l0eVByb3ZpZGVyKHRoaXMsICdHcHVHNXhsYXJnZScsIHZwYywgZWMyLkluc3RhbmNlVHlwZS5vZihlYzIuSW5zdGFuY2VDbGFzcy5HNSwgZWMyLkluc3RhbmNlU2l6ZS5YTEFSR0UpLCBlY3NTZWN1cml0eUdyb3VwLCBpbnN0YW5jZVJvbGUpO1xuICAgIGNsdXN0ZXIuYWRkQXNnQ2FwYWNpdHlQcm92aWRlcihjcEc1eGxhcmdlKTtcblxuICAgIGNvbnN0IHsgYXNnOiBhc2dHNTJ4bGFyZ2UsIGNhcGFjaXR5UHJvdmlkZXI6IGNwRzUyeGxhcmdlIH0gPSB0aGlzLmNyZWF0ZUFzZ0FuZENhcGFjaXR5UHJvdmlkZXIodGhpcywgJ0dwdUc1MnhsYXJnZScsIHZwYywgZWMyLkluc3RhbmNlVHlwZS5vZihlYzIuSW5zdGFuY2VDbGFzcy5HNSwgZWMyLkluc3RhbmNlU2l6ZS5YTEFSR0UyKSwgZWNzU2VjdXJpdHlHcm91cCwgaW5zdGFuY2VSb2xlKTtcbiAgICBjbHVzdGVyLmFkZEFzZ0NhcGFjaXR5UHJvdmlkZXIoY3BHNTJ4bGFyZ2UpO1xuXG4gICAgLy8gUzMgYWNjZXNzIGxvZ3MgYnVja2V0XG4gICAgY29uc3QgYWNjZXNzTG9nc0J1Y2tldCA9IG5ldyBzMy5CdWNrZXQodGhpcywgJ0FjY2Vzc0xvZ3NCdWNrZXQnLCB7XG4gICAgICB2ZXJzaW9uZWQ6IGZhbHNlLFxuICAgICAgYmxvY2tQdWJsaWNBY2Nlc3M6IHMzLkJsb2NrUHVibGljQWNjZXNzLkJMT0NLX0FMTCxcbiAgICAgIGVuY3J5cHRpb246IHMzLkJ1Y2tldEVuY3J5cHRpb24uUzNfTUFOQUdFRCxcbiAgICAgIGVuZm9yY2VTU0w6IHRydWUsXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5SRVRBSU5cbiAgICB9KTtcblxuICAgIC8vIEtNUyBrZXkgZm9yIHJlc3VsdHMgYnVja2V0XG4gICAgY29uc3QgcmVzdWx0c0J1Y2tldEtleSA9IG5ldyBrbXMuS2V5KHRoaXMsICdSZXN1bHRzQnVja2V0S2V5Jywge1xuICAgICAgZW5hYmxlS2V5Um90YXRpb246IHRydWUsXG4gICAgICBhbGlhczogJ2FsaWFzL2FtaXJhLWxldHRlci1zY29yaW5nLXJlc3VsdHMnXG4gICAgfSk7XG5cbiAgICAvLyBSZXN1bHRzIGJ1Y2tldCAoc291cmNlIG9mIHRydXRoKSB3aXRoIFNTRS1LTVMsIGJ1Y2tldCBrZXksIGFjY2VzcyBsb2dzLCBhbmQgbGlmZWN5Y2xlXG4gICAgY29uc3QgcmVzdWx0c0J1Y2tldCA9IG5ldyBzMy5CdWNrZXQodGhpcywgJ1Jlc3VsdHNCdWNrZXQnLCB7XG4gICAgICB2ZXJzaW9uZWQ6IHRydWUsXG4gICAgICBibG9ja1B1YmxpY0FjY2VzczogczMuQmxvY2tQdWJsaWNBY2Nlc3MuQkxPQ0tfQUxMLFxuICAgICAgZW5jcnlwdGlvbjogczMuQnVja2V0RW5jcnlwdGlvbi5LTVMsXG4gICAgICBlbmNyeXB0aW9uS2V5OiByZXN1bHRzQnVja2V0S2V5LFxuICAgICAgYnVja2V0S2V5RW5hYmxlZDogdHJ1ZSxcbiAgICAgIHNlcnZlckFjY2Vzc0xvZ3NCdWNrZXQ6IGFjY2Vzc0xvZ3NCdWNrZXQsXG4gICAgICBzZXJ2ZXJBY2Nlc3NMb2dzUHJlZml4OiAnczMtYWNjZXNzLWxvZ3MvJyxcbiAgICAgIGxpZmVjeWNsZVJ1bGVzOiBbXG4gICAgICAgIHtcbiAgICAgICAgICBpZDogJ0ludGVsbGlnZW50VGllcmluZ05vdycsXG4gICAgICAgICAgZW5hYmxlZDogdHJ1ZSxcbiAgICAgICAgICB0cmFuc2l0aW9uczogW3sgc3RvcmFnZUNsYXNzOiBzMy5TdG9yYWdlQ2xhc3MuSU5URUxMSUdFTlRfVElFUklORywgdHJhbnNpdGlvbkFmdGVyOiBjZGsuRHVyYXRpb24uZGF5cygwKSB9XVxuICAgICAgICB9XG4gICAgICBdLFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuUkVUQUlOLFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZVxuICAgIH0pO1xuICAgIHJlc3VsdHNCdWNrZXQuYWRkVG9SZXNvdXJjZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBzaWQ6ICdEZW55SW5zZWN1cmVUcmFuc3BvcnQnLFxuICAgICAgZWZmZWN0OiBpYW0uRWZmZWN0LkRFTlksXG4gICAgICBwcmluY2lwYWxzOiBbbmV3IGlhbS5BbnlQcmluY2lwYWwoKV0sXG4gICAgICBhY3Rpb25zOiBbJ3MzOkdldE9iamVjdCcsICdzMzpQdXRPYmplY3QnLCAnczM6TGlzdEJ1Y2tldCcsICdzMzpEZWxldGVPYmplY3QnLCAnczM6RGVsZXRlT2JqZWN0VmVyc2lvbiddLFxuICAgICAgcmVzb3VyY2VzOiBbcmVzdWx0c0J1Y2tldC5idWNrZXRBcm4sIGAke3Jlc3VsdHNCdWNrZXQuYnVja2V0QXJufS8qYF0sXG4gICAgICBjb25kaXRpb25zOiB7IEJvb2w6IHsgJ2F3czpTZWN1cmVUcmFuc3BvcnQnOiAnZmFsc2UnIH0gfVxuICAgIH0pKTtcbiAgICByZXN1bHRzQnVja2V0LmFkZFRvUmVzb3VyY2VQb2xpY3kobmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgc2lkOiAnRGVueVVuRW5jcnlwdGVkT2JqZWN0VXBsb2FkcycsXG4gICAgICBlZmZlY3Q6IGlhbS5FZmZlY3QuREVOWSxcbiAgICAgIHByaW5jaXBhbHM6IFtuZXcgaWFtLkFueVByaW5jaXBhbCgpXSxcbiAgICAgIGFjdGlvbnM6IFsnczM6UHV0T2JqZWN0J10sXG4gICAgICByZXNvdXJjZXM6IFtgJHtyZXN1bHRzQnVja2V0LmJ1Y2tldEFybn0vKmBdLFxuICAgICAgY29uZGl0aW9uczogeyBTdHJpbmdOb3RFcXVhbHM6IHsgJ3MzOngtYW16LXNlcnZlci1zaWRlLWVuY3J5cHRpb24nOiAnYXdzOmttcycgfSB9XG4gICAgfSkpO1xuXG4gICAgLy8gR3JhbnQgd2lsbCBiZSBhdHRhY2hlZCBhZnRlciB0YXNrUm9sZSBpcyBkZWZpbmVkXG5cbiAgICAvLyBTUVMgcXVldWUgZm9yIGpvYnMgd2l0aCBETFFcbiAgICBjb25zdCBkbHEgPSBuZXcgc3FzLlF1ZXVlKHRoaXMsICdKb2JzRExRJywge1xuICAgICAgcmV0ZW50aW9uUGVyaW9kOiBjZGsuRHVyYXRpb24uZGF5cygxNCksXG4gICAgICBlbmNyeXB0aW9uOiBzcXMuUXVldWVFbmNyeXB0aW9uLktNU19NQU5BR0VELFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZVxuICAgIH0pO1xuICAgIGNvbnN0IGpvYnNRdWV1ZSA9IG5ldyBzcXMuUXVldWUodGhpcywgJ0pvYnNRdWV1ZScsIHtcbiAgICAgIHZpc2liaWxpdHlUaW1lb3V0OiBjZGsuRHVyYXRpb24ubWludXRlcygxNSksXG4gICAgICBkZWFkTGV0dGVyUXVldWU6IHsgcXVldWU6IGRscSwgbWF4UmVjZWl2ZUNvdW50OiAzIH0sXG4gICAgICBlbmNyeXB0aW9uOiBzcXMuUXVldWVFbmNyeXB0aW9uLktNU19NQU5BR0VELFxuICAgICAgZW5mb3JjZVNTTDogdHJ1ZVxuICAgIH0pO1xuXG4gICAgLy8gVGFzayBleGVjdXRpb24gcm9sZVxuICAgIGNvbnN0IHRhc2tFeGVjdXRpb25Sb2xlID0gbmV3IGlhbS5Sb2xlKHRoaXMsICdUYXNrRXhlY3V0aW9uUm9sZScsIHtcbiAgICAgIGFzc3VtZWRCeTogbmV3IGlhbS5TZXJ2aWNlUHJpbmNpcGFsKCdlY3MtdGFza3MuYW1hem9uYXdzLmNvbScpLFxuICAgICAgbWFuYWdlZFBvbGljaWVzOiBbXG4gICAgICAgIGlhbS5NYW5hZ2VkUG9saWN5LmZyb21Bd3NNYW5hZ2VkUG9saWN5TmFtZSgnc2VydmljZS1yb2xlL0FtYXpvbkVDU1Rhc2tFeGVjdXRpb25Sb2xlUG9saWN5JylcbiAgICAgIF1cbiAgICB9KTtcblxuICAgIC8vIFRhc2sgcm9sZSB3aXRoIG5lY2Vzc2FyeSBwZXJtaXNzaW9uc1xuICAgIGNvbnN0IHRhc2tSb2xlID0gbmV3IGlhbS5Sb2xlKHRoaXMsICdUYXNrUm9sZScsIHtcbiAgICAgIHJvbGVOYW1lOiBgYW1pcmEtbGV0dGVyLXNjb3JpbmctdGFzay0ke2Nkay5TdGFjay5vZih0aGlzKS5zdGFja05hbWV9YCxcbiAgICAgIGFzc3VtZWRCeTogbmV3IGlhbS5TZXJ2aWNlUHJpbmNpcGFsKCdlY3MtdGFza3MuYW1hem9uYXdzLmNvbScpLFxuICAgICAgaW5saW5lUG9saWNpZXM6IHtcbiAgICAgICAgUzNBY2Nlc3M6IG5ldyBpYW0uUG9saWN5RG9jdW1lbnQoe1xuICAgICAgICAgIHN0YXRlbWVudHM6IFtcbiAgICAgICAgICAgIG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgICAgICAgICAgYWN0aW9uczogWydzMzpMaXN0QnVja2V0J10sXG4gICAgICAgICAgICAgIHJlc291cmNlczogW3Jlc3VsdHNCdWNrZXQuYnVja2V0QXJuXVxuICAgICAgICAgICAgfSksXG4gICAgICAgICAgICBuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICAgICAgICAgIGFjdGlvbnM6IFsnczM6UHV0T2JqZWN0J10sXG4gICAgICAgICAgICAgIHJlc291cmNlczogW2Ake3Jlc3VsdHNCdWNrZXQuYnVja2V0QXJufS8qYF1cbiAgICAgICAgICAgIH0pXG4gICAgICAgICAgXVxuICAgICAgICB9KSxcbiAgICAgICAgU3FzQWNjZXNzOiBuZXcgaWFtLlBvbGljeURvY3VtZW50KHtcbiAgICAgICAgICBzdGF0ZW1lbnRzOiBbXG4gICAgICAgICAgICBuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICAgICAgICAgIGFjdGlvbnM6IFsnc3FzOlJlY2VpdmVNZXNzYWdlJywgJ3NxczpEZWxldGVNZXNzYWdlJywgJ3NxczpHZXRRdWV1ZUF0dHJpYnV0ZXMnXSxcbiAgICAgICAgICAgICAgcmVzb3VyY2VzOiBbam9ic1F1ZXVlLnF1ZXVlQXJuXVxuICAgICAgICAgICAgfSlcbiAgICAgICAgICBdXG4gICAgICAgIH0pXG4gICAgICB9XG4gICAgfSk7XG5cbiAgICAvLyBDb25kaXRpb25zIGZvciBjb25kaXRpb25hbCByZXNvdXJjZXNcbiAgICBjb25zdCBhdWRpb1Byb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ0F1ZGlvQnVja2V0UHJvdmlkZWQnLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uTm90KGNkay5Gbi5jb25kaXRpb25FcXVhbHMoYXVkaW9CdWNrZXROYW1lUGFyYW0udmFsdWVBc1N0cmluZywgJycpKVxuICAgIH0pO1xuICAgIGNvbnN0IHVzZVRyaXRvbkNvbmRpdGlvbiA9IG5ldyBjZGsuQ2ZuQ29uZGl0aW9uKHRoaXMsICdVc2VUcml0b25Db25kaXRpb24nLCB7XG4gICAgICBleHByZXNzaW9uOiBjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHVzZVRyaXRvblBhcmFtLnZhbHVlQXNTdHJpbmcsICd0cnVlJylcbiAgICB9KTtcbiAgICBjb25zdCBhdWRpb1BvbGljeURvYyA9IG5ldyBpYW0uUG9saWN5RG9jdW1lbnQoe1xuICAgICAgc3RhdGVtZW50czogW1xuICAgICAgICBuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICAgICAgYWN0aW9uczogWydzMzpMaXN0QnVja2V0J10sXG4gICAgICAgICAgcmVzb3VyY2VzOiBbY2RrLkFybi5mb3JtYXQoeyBzZXJ2aWNlOiAnczMnLCByZXNvdXJjZTogYXVkaW9CdWNrZXROYW1lUGFyYW0udmFsdWVBc1N0cmluZyB9LCB0aGlzKV0sXG4gICAgICAgICAgY29uZGl0aW9uczoge1xuICAgICAgICAgICAgU3RyaW5nTGlrZTogeyAnczM6cHJlZml4JzogW2F1ZGlvQnVja2V0UHJlZml4UGFyYW0udmFsdWVBc1N0cmluZ10gfVxuICAgICAgICAgIH1cbiAgICAgICAgfSksXG4gICAgICAgIG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgICAgICBhY3Rpb25zOiBbJ3MzOkdldE9iamVjdCddLFxuICAgICAgICAgIHJlc291cmNlczogW2Nkay5Bcm4uZm9ybWF0KHsgc2VydmljZTogJ3MzJywgcmVzb3VyY2U6IGAke2F1ZGlvQnVja2V0TmFtZVBhcmFtLnZhbHVlQXNTdHJpbmd9LyR7YXVkaW9CdWNrZXRQcmVmaXhQYXJhbS52YWx1ZUFzU3RyaW5nfSpgIH0sIHRoaXMpXVxuICAgICAgICB9KVxuICAgICAgXVxuICAgIH0pO1xuICAgIGNvbnN0IGF1ZGlvQ2ZuUG9saWN5ID0gbmV3IGlhbS5DZm5Qb2xpY3kodGhpcywgJ1Rhc2tSb2xlQXVkaW9SZWFkUG9saWN5Jywge1xuICAgICAgcG9saWN5RG9jdW1lbnQ6IGF1ZGlvUG9saWN5RG9jLFxuICAgICAgcm9sZXM6IFt0YXNrUm9sZS5yb2xlTmFtZSFdLFxuICAgICAgcG9saWN5TmFtZTogYFRhc2tSb2xlQXVkaW9SZWFkUG9saWN5LSR7Y2RrLlN0YWNrLm9mKHRoaXMpLnN0YWNrTmFtZX1gXG4gICAgfSk7XG4gICAgYXVkaW9DZm5Qb2xpY3kuY2ZuT3B0aW9ucy5jb25kaXRpb24gPSBhdWRpb1Byb3ZpZGVkO1xuXG4gICAgLy8gQWxsb3cgdGFzayByb2xlIHRvIHVzZSB0aGUgS01TIGtleSBmb3IgU1NFLUtNUyBvYmplY3RzXG4gICAgcmVzdWx0c0J1Y2tldEtleS5ncmFudEVuY3J5cHREZWNyeXB0KHRhc2tSb2xlKTtcblxuICAgIC8vIENsb3VkV2F0Y2ggTG9nIEdyb3VwXG4gICAgY29uc3QgbG9nR3JvdXAgPSBuZXcgbG9ncy5Mb2dHcm91cCh0aGlzLCAnQW1pcmFMZXR0ZXJTY29yaW5nTG9nR3JvdXAnLCB7XG4gICAgICBsb2dHcm91cE5hbWU6ICcvZWNzL2FtaXJhLWxldHRlci1zY29yaW5nJyxcbiAgICAgIHJldGVudGlvbjogbG9ncy5SZXRlbnRpb25EYXlzLk9ORV9NT05USCxcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNkay5SZW1vdmFsUG9saWN5LkRFU1RST1ksXG4gICAgICBlbmNyeXB0aW9uS2V5OiByZXN1bHRzQnVja2V0S2V5XG4gICAgfSk7XG5cbiAgICAvLyBWUEMgRmxvdyBMb2dzXG4gICAgY29uc3QgdnBjRmxvd0xvZ0dyb3VwID0gbmV3IGxvZ3MuTG9nR3JvdXAodGhpcywgJ1ZwY0Zsb3dMb2dzJywge1xuICAgICAgcmV0ZW50aW9uOiBsb2dzLlJldGVudGlvbkRheXMuT05FX01PTlRILFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuREVTVFJPWSxcbiAgICAgIGVuY3J5cHRpb25LZXk6IHJlc3VsdHNCdWNrZXRLZXlcbiAgICB9KTtcbiAgICB2cGMuYWRkRmxvd0xvZygnRmxvd0xvZ3NBbGwnLCB7XG4gICAgICBkZXN0aW5hdGlvbjogZWMyLkZsb3dMb2dEZXN0aW5hdGlvbi50b0Nsb3VkV2F0Y2hMb2dzKHZwY0Zsb3dMb2dHcm91cCksXG4gICAgICB0cmFmZmljVHlwZTogZWMyLkZsb3dMb2dUcmFmZmljVHlwZS5BTExcbiAgICB9KTtcblxuICAgIC8vIEVDUyBUYXNrIERlZmluaXRpb25cbiAgICBjb25zdCB0YXNrRGVmaW5pdGlvbiA9IG5ldyBlY3MuRWMyVGFza0RlZmluaXRpb24odGhpcywgJ0FtaXJhTGV0dGVyU2NvcmluZ1Rhc2tEZWYnLCB7XG4gICAgICBmYW1pbHk6IGBhbWlyYS1sZXR0ZXItc2NvcmluZy0ke2Nkay5TdGFjay5vZih0aGlzKS5zdGFja05hbWV9YCxcbiAgICAgIGV4ZWN1dGlvblJvbGU6IHRhc2tFeGVjdXRpb25Sb2xlLFxuICAgICAgdGFza1JvbGUsXG4gICAgICBuZXR3b3JrTW9kZTogZWNzLk5ldHdvcmtNb2RlLkFXU19WUENcbiAgICB9KTtcblxuICAgIC8vIFRyaXRvbiBHUFUgaW5mZXJlbmNlIHNlcnZlciBjb250YWluZXJcbiAgICBjb25zdCB0cml0b25Db250YWluZXIgPSB0YXNrRGVmaW5pdGlvbi5hZGRDb250YWluZXIoJ1RyaXRvblNlcnZlckNvbnRhaW5lcicsIHtcbiAgICAgIGltYWdlOiBlY3MuQ29udGFpbmVySW1hZ2UuZnJvbUVjclJlcG9zaXRvcnkodHJpdG9uUmVwb3NpdG9yeSwgdHJpdG9uSW1hZ2VUYWdQYXJhbS52YWx1ZUFzU3RyaW5nKSxcbiAgICAgIG1lbW9yeVJlc2VydmF0aW9uTWlCOiA0MDk2LFxuICAgICAgY3B1OiAxMDI0LFxuICAgICAgZ3B1Q291bnQ6IDEsXG4gICAgICBsb2dnaW5nOiBlY3MuTG9nRHJpdmVyLmF3c0xvZ3MoeyBsb2dHcm91cCwgc3RyZWFtUHJlZml4OiAndHJpdG9uLXNlcnZlcicgfSksXG4gICAgICBwb3J0TWFwcGluZ3M6IFt7IGNvbnRhaW5lclBvcnQ6IDgwMDAgfSwgeyBjb250YWluZXJQb3J0OiA4MDAxIH0sIHsgY29udGFpbmVyUG9ydDogODAwMiB9XSxcbiAgICAgIGhlYWx0aENoZWNrOiB7XG4gICAgICAgIGNvbW1hbmQ6IFsnQ01ELVNIRUxMJywgJ2N1cmwgLXNmIGh0dHA6Ly8xMjcuMC4wLjE6ODAwMC92Mi9oZWFsdGgvcmVhZHkgfHwgZXhpdCAxJ10sXG4gICAgICAgIGludGVydmFsOiBjZGsuRHVyYXRpb24uc2Vjb25kcygxNSksXG4gICAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDUpLFxuICAgICAgICByZXRyaWVzOiAzLFxuICAgICAgICBzdGFydFBlcmlvZDogY2RrLkR1cmF0aW9uLnNlY29uZHMoMzApXG4gICAgICB9XG4gICAgfSk7XG5cbiAgICAvLyBUTFMgcHJveHkgc2lkZWNhciB0byB0ZXJtaW5hdGUgVExTIGluc2lkZSB0aGUgdGFzayBhbmQgcHJveHkgdG8gVHJpdG9uIG92ZXIgbG9jYWxob3N0OjgwMDBcbiAgICBjb25zdCByZXF1aXJlVGFyZ2V0VGxzU2VjcmV0UGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnUmVxdWlyZVRhcmdldFRsc1NlY3JldCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJ2ZhbHNlJyxcbiAgICAgIGFsbG93ZWRWYWx1ZXM6IFsndHJ1ZScsICdmYWxzZSddLFxuICAgICAgZGVzY3JpcHRpb246ICdSZXF1aXJlIFRMUyBjZXJ0L2tleSBzZWNyZXQgZm9yIHNpZGVjYXIgKGRpc2FsbG93IHNlbGYtc2lnbmVkKSdcbiAgICB9KTtcblxuICAgIGNvbnN0IHRsc1Byb3h5Q29udGFpbmVyID0gdGFza0RlZmluaXRpb24uYWRkQ29udGFpbmVyKCdUbHNQcm94eUNvbnRhaW5lcicsIHtcbiAgICAgIGltYWdlOiBlY3MuQ29udGFpbmVySW1hZ2UuZnJvbVJlZ2lzdHJ5KCduZ2lueDoxLjI1LWFscGluZScpLFxuICAgICAgbWVtb3J5UmVzZXJ2YXRpb25NaUI6IDEyOCxcbiAgICAgIGNwdTogMTI4LFxuICAgICAgbG9nZ2luZzogZWNzLkxvZ0RyaXZlci5hd3NMb2dzKHsgbG9nR3JvdXAsIHN0cmVhbVByZWZpeDogJ3Rscy1wcm94eScgfSksXG4gICAgICBwb3J0TWFwcGluZ3M6IFt7IGNvbnRhaW5lclBvcnQ6IDg0NDMgfV0sXG4gICAgICBlbnZpcm9ubWVudDoge1xuICAgICAgICBFTkFCTEVfSFRUUDI6IGVuYWJsZVRhcmdldEh0dHAyUGFyYW0udmFsdWVBc1N0cmluZyxcbiAgICAgICAgU1NMX0NJUEhFUlM6IHRhcmdldFNzbENpcGhlcnNQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgICBSRVFVSVJFX1RMU19TRUNSRVQ6IHJlcXVpcmVUYXJnZXRUbHNTZWNyZXRQYXJhbS52YWx1ZUFzU3RyaW5nLFxuICAgICAgfSxcbiAgICAgIGNvbW1hbmQ6IFtcbiAgICAgICAgJ3NoJyxcbiAgICAgICAgJy1jJyxcbiAgICAgICAgW1xuICAgICAgICAgICdzZXQgLWUnLFxuICAgICAgICAgICdhcGsgYWRkIC0tbm8tY2FjaGUgb3BlbnNzbCcsXG4gICAgICAgICAgJ21rZGlyIC1wIC9ldGMvbmdpbngvY2VydHMgL2V0Yy9uZ2lueC9jb25mLmQnLFxuICAgICAgICAgICdpZiBbIFwiJFJFUVVJUkVfVExTX1NFQ1JFVFwiID0gXCJ0cnVlXCIgXSAmJiB7IFsgLXogXCIke1RMU19DRVJUOi19XCIgXSB8fCBbIC16IFwiJHtUTFNfS0VZOi19XCIgXTsgfTsgdGhlbiBlY2hvIFwiVExTIGNlcnQva2V5IHNlY3JldCByZXF1aXJlZFwiID4mMjsgZXhpdCAxOyBmaScsXG4gICAgICAgICAgJ2lmIFsgLW4gXCIke1RMU19DRVJUOi19XCIgXSAmJiBbIC1uIFwiJHtUTFNfS0VZOi19XCIgXTsgdGhlbiBlY2hvIFwiJFRMU19DRVJUXCIgPiAvZXRjL25naW54L2NlcnRzL3Rscy5jcnQgJiYgZWNobyBcIiRUTFNfS0VZXCIgPiAvZXRjL25naW54L2NlcnRzL3Rscy5rZXk7IGVsc2Ugb3BlbnNzbCByZXEgLXg1MDkgLW5vZGVzIC1kYXlzIDM2NTAgLW5ld2tleSByc2E6MjA0OCAtc3ViaiBcIi9DTj1sb2NhbGhvc3RcIiAta2V5b3V0IC9ldGMvbmdpbngvY2VydHMvdGxzLmtleSAtb3V0IC9ldGMvbmdpbngvY2VydHMvdGxzLmNydDsgZmknLFxuICAgICAgICAgIFwiSFRUUDJfRElSRUNUSVZFPScnICYmIFsgXFxcIiRFTkFCTEVfSFRUUDJcXFwiID0gXFxcInRydWVcXFwiIF0gJiYgSFRUUDJfRElSRUNUSVZFPScgaHR0cDInIHx8IHRydWVcIixcbiAgICAgICAgICBcIkNJUEhFUlNfRElSRUNUSVZFPScnICYmIFsgLW4gXFxcIiRTU0xfQ0lQSEVSU1xcXCIgXSAmJiBDSVBIRVJTX0RJUkVDVElWRT1cXFxcXFxcIiAgc3NsX2NpcGhlcnMgJFNTTF9DSVBIRVJTO1xcXFxcXFwiIHx8IHRydWVcIixcbiAgICAgICAgICBcInByaW50ZiAnc2VydmVyIHtcXFxcbiAgbGlzdGVuIDg0NDMgc3NsJXM7XFxcXG4gIHNzbF9jZXJ0aWZpY2F0ZSAvZXRjL25naW54L2NlcnRzL3Rscy5jcnQ7XFxcXG4gIHNzbF9jZXJ0aWZpY2F0ZV9rZXkgL2V0Yy9uZ2lueC9jZXJ0cy90bHMua2V5O1xcXFxuICBzc2xfcHJvdG9jb2xzIFRMU3YxLjIgVExTdjEuMztcXFxcbiVzXFxcXG4gIGxvY2F0aW9uIC8ge1xcXFxuICAgIHByb3h5X3Bhc3MgaHR0cDovLzEyNy4wLjAuMTo4MDAwO1xcXFxuICAgIHByb3h5X3NldF9oZWFkZXIgSG9zdCAkaG9zdDtcXFxcbiAgICBwcm94eV9zZXRfaGVhZGVyIFgtRm9yd2FyZGVkLVByb3RvICRzY2hlbWU7XFxcXG4gICAgcHJveHlfc2V0X2hlYWRlciBYLUZvcndhcmRlZC1Gb3IgJHJlbW90ZV9hZGRyO1xcXFxuICB9XFxcXG59XFxcXG4nIFxcXCIkSFRUUDJfRElSRUNUSVZFXFxcIiBcXFwiJENJUEhFUlNfRElSRUNUSVZFXFxcIiA+IC9ldGMvbmdpbngvY29uZi5kL2RlZmF1bHQuY29uZlwiLFxuICAgICAgICAgIFwibmdpbnggLWcgJ2RhZW1vbiBvZmY7J1wiLFxuICAgICAgICBdLmpvaW4oJyAmJiAnKSxcbiAgICAgIF0sXG4gICAgfSk7XG5cbiAgICAvLyBDb25kaXRpb25hbGx5IGFkZCBTZWNyZXRzIE1hbmFnZXIgc2VjcmV0cyB0byBUTFMgcHJveHkgY29udGFpbmVyIHVzaW5nIEwyIGNvbnN0cnVjdHNcbiAgICBjb25zdCB0YXJnZXRDZXJ0UHJvdmlkZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnVHJpdG9uVGFyZ2V0Q2VydFByb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHRyaXRvblRhcmdldENlcnRTZWNyZXRBcm5QYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpXG4gICAgfSk7XG5cbiAgICAvLyBTZWNyZXRzIGFyZSBoYW5kbGVkIGRpcmVjdGx5IGluIENsb3VkRm9ybWF0aW9uIGR1ZSB0byBjb25kaXRpb25hbCBsb2dpYyByZXF1aXJlbWVudHNcblxuICAgIC8vIEFwcGx5IGNvbmRpdGlvbiB0byB0aGUgdW5kZXJseWluZyBDbG91ZEZvcm1hdGlvbiByZXNvdXJjZXMgZm9yIHRoZSBzZWNyZXRzXG4gICAgY29uc3QgY2ZuVGFza0RlZiA9IHRhc2tEZWZpbml0aW9uLm5vZGUuZGVmYXVsdENoaWxkIGFzIGVjcy5DZm5UYXNrRGVmaW5pdGlvbjtcbiAgICBjZm5UYXNrRGVmLmFkZFByb3BlcnR5T3ZlcnJpZGUoJ0NvbnRhaW5lckRlZmluaXRpb25zLjEuU2VjcmV0cycsIGNkay5Gbi5jb25kaXRpb25JZihcbiAgICAgIHRhcmdldENlcnRQcm92aWRlZC5sb2dpY2FsSWQsXG4gICAgICBbXG4gICAgICAgIHsgbmFtZTogJ1RMU19DRVJUJywgdmFsdWVGcm9tOiBgJHt0cml0b25UYXJnZXRDZXJ0U2VjcmV0QXJuUGFyYW0udmFsdWVBc1N0cmluZ306Y2VydDo6YCB9LFxuICAgICAgICB7IG5hbWU6ICdUTFNfS0VZJywgdmFsdWVGcm9tOiBgJHt0cml0b25UYXJnZXRDZXJ0U2VjcmV0QXJuUGFyYW0udmFsdWVBc1N0cmluZ306a2V5OjpgIH1cbiAgICAgIF0sXG4gICAgICBjZGsuQXdzLk5PX1ZBTFVFXG4gICAgKSk7XG5cbiAgICAvLyBEQ0dNIGV4cG9ydGVyIGZvciBHUFUgbWV0cmljc1xuICAgIGNvbnN0IGRjZ21Db250YWluZXIgPSB0YXNrRGVmaW5pdGlvbi5hZGRDb250YWluZXIoJ0RjZ21FeHBvcnRlckNvbnRhaW5lcicsIHtcbiAgICAgIGltYWdlOiBlY3MuQ29udGFpbmVySW1hZ2UuZnJvbUVjclJlcG9zaXRvcnkoZGNnbUV4cG9ydGVyUmVwb3NpdG9yeSwgZGNnbUltYWdlVGFnUGFyYW0udmFsdWVBc1N0cmluZyksXG4gICAgICBtZW1vcnlSZXNlcnZhdGlvbk1pQjogMjU2LFxuICAgICAgY3B1OiAxMjgsXG4gICAgICBsb2dnaW5nOiBlY3MuTG9nRHJpdmVyLmF3c0xvZ3MoeyBsb2dHcm91cCwgc3RyZWFtUHJlZml4OiAnZGNnbS1leHBvcnRlcicgfSksXG4gICAgICBwb3J0TWFwcGluZ3M6IFt7IGNvbnRhaW5lclBvcnQ6IDk0MDAgfV0sXG4gICAgfSk7XG5cbiAgICAvLyBDbG91ZFdhdGNoIEFnZW50IHRvIHNjcmFwZSBEQ0dNIG1ldHJpY3NcbiAgICBjb25zdCBjd0FnZW50Q29uZmlnU3RyaW5nOiBzdHJpbmcgPSBKU09OLnN0cmluZ2lmeShjd0FnZW50Q29uZmlnKTtcbiAgICBjb25zdCBjd0FnZW50Q29uZmlnUGFyYW0gPSBuZXcgc3NtLlN0cmluZ1BhcmFtZXRlcih0aGlzLCAnU3NtQ3dBZ2VudENvbmZpZycsIHtcbiAgICAgIHBhcmFtZXRlck5hbWU6ICcvYW1pcmEvY3dhZ2VudF9jb25maWcnLFxuICAgICAgc3RyaW5nVmFsdWU6IGN3QWdlbnRDb25maWdTdHJpbmdcbiAgICB9KTtcbiAgICBjb25zdCBjd0FnZW50Q29udGFpbmVyID0gdGFza0RlZmluaXRpb24uYWRkQ29udGFpbmVyKCdDbG91ZFdhdGNoQWdlbnRDb250YWluZXInLCB7XG4gICAgICBpbWFnZTogZWNzLkNvbnRhaW5lckltYWdlLmZyb21FY3JSZXBvc2l0b3J5KGN3QWdlbnRSZXBvc2l0b3J5LCBjd0FnZW50SW1hZ2VUYWdQYXJhbS52YWx1ZUFzU3RyaW5nKSxcbiAgICAgIG1lbW9yeVJlc2VydmF0aW9uTWlCOiAyNTYsXG4gICAgICBjcHU6IDEyOCxcbiAgICAgIGxvZ2dpbmc6IGVjcy5Mb2dEcml2ZXIuYXdzTG9ncyh7IGxvZ0dyb3VwLCBzdHJlYW1QcmVmaXg6ICdjbG91ZHdhdGNoLWFnZW50JyB9KSxcbiAgICAgIGNvbW1hbmQ6IFsnL29wdC9hd3MvYW1hem9uLWNsb3Vkd2F0Y2gtYWdlbnQvYmluL2FtYXpvbi1jbG91ZHdhdGNoLWFnZW50JywgJy1hJywgJ2ZldGNoLWNvbmZpZycsICctbScsICdlYzInLCAnLWMnLCBgc3NtOiR7Y3dBZ2VudENvbmZpZ1BhcmFtLnBhcmFtZXRlck5hbWV9YCwgJy1zJ10sXG4gICAgICBlbnZpcm9ubWVudDogeyBBV1NfUkVHSU9OOiBjZGsuU3RhY2sub2YodGhpcykucmVnaW9uIH1cbiAgICB9KTtcblxuICAgIC8vIEdyYW50IENXIEFnZW50IGFjY2VzcyB0byByZWFkIGl0cyBTU00gY29uZmlnXG4gICAgY3dBZ2VudENvbmZpZ1BhcmFtLmdyYW50UmVhZCh0YXNrUm9sZSk7XG5cbiAgICAvLyBJbmNyZWFzZSBub2ZpbGUgbGltaXRzIGZvciBjb250YWluZXJzXG4gICAgdHJpdG9uQ29udGFpbmVyLmFkZFVsaW1pdHMoeyBuYW1lOiBlY3MuVWxpbWl0TmFtZS5OT0ZJTEUsIHNvZnRMaW1pdDogNjU1MzYsIGhhcmRMaW1pdDogNjU1MzYgfSk7XG5cbiAgICAvLyBBcHBsaWNhdGlvbiBMb2FkIEJhbGFuY2VyIGZvciBUcml0b24gR1BVIGNsdXN0ZXJcbiAgICBjb25zdCB0cml0b25BbGIgPSBuZXcgZWxidjIuQXBwbGljYXRpb25Mb2FkQmFsYW5jZXIodGhpcywgJ1RyaXRvbkxvYWRCYWxhbmNlcicsIHtcbiAgICAgIHZwYyxcbiAgICAgIGludGVybmV0RmFjaW5nOiBmYWxzZSwgLy8gSW50ZXJuYWwgQUxCIHNpbmNlIExhbWJkYSB3aWxsIGNhbGwgaXRcbiAgICAgIHNlY3VyaXR5R3JvdXA6IGFsYlNlY3VyaXR5R3JvdXAsXG4gICAgICBkZWxldGlvblByb3RlY3Rpb246IHRydWVcbiAgICB9KTtcbiAgICB0cml0b25BbGIubG9nQWNjZXNzTG9ncyhhY2Nlc3NMb2dzQnVja2V0LCAnYWxiLWFjY2Vzcy1sb2dzLycpO1xuXG4gICAgY29uc3QgdHJpdG9uVGFyZ2V0R3JvdXAgPSBuZXcgZWxidjIuQXBwbGljYXRpb25UYXJnZXRHcm91cCh0aGlzLCAnVHJpdG9uVGFyZ2V0R3JvdXAnLCB7XG4gICAgICB2cGMsXG4gICAgICBwb3J0OiA4NDQzLFxuICAgICAgcHJvdG9jb2w6IGVsYnYyLkFwcGxpY2F0aW9uUHJvdG9jb2wuSFRUUFMsXG4gICAgICB0YXJnZXRUeXBlOiBlbGJ2Mi5UYXJnZXRUeXBlLklQLFxuICAgICAgaGVhbHRoQ2hlY2s6IHtcbiAgICAgICAgcGF0aDogJy92Mi9oZWFsdGgvcmVhZHknLFxuICAgICAgICBwcm90b2NvbDogZWxidjIuUHJvdG9jb2wuSFRUUFMsXG4gICAgICAgIGhlYWx0aHlUaHJlc2hvbGRDb3VudDogMyxcbiAgICAgICAgdW5oZWFsdGh5VGhyZXNob2xkQ291bnQ6IDUsXG4gICAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDEwKSxcbiAgICAgICAgaW50ZXJ2YWw6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDMwKVxuICAgICAgfVxuICAgIH0pO1xuXG4gICAgY29uc3QgdHJpdG9uTGlzdGVuZXIgPSB0cml0b25BbGIuYWRkTGlzdGVuZXIoJ1RyaXRvbkxpc3RlbmVySHR0cHMnLCB7XG4gICAgICBwb3J0OiA0NDMsXG4gICAgICBwcm90b2NvbDogZWxidjIuQXBwbGljYXRpb25Qcm90b2NvbC5IVFRQUyxcbiAgICAgIHNzbFBvbGljeTogZWxidjIuU3NsUG9saWN5LlRMUzEyX0VYVCxcbiAgICAgIGNlcnRpZmljYXRlczogW2VsYnYyLkxpc3RlbmVyQ2VydGlmaWNhdGUuZnJvbUFybih0cml0b25DZXJ0QXJuUGFyYW0udmFsdWVBc1N0cmluZyldLFxuICAgICAgZGVmYXVsdFRhcmdldEdyb3VwczogW3RyaXRvblRhcmdldEdyb3VwXVxuICAgIH0pO1xuXG4gICAgLy8gRUNTIHNlcnZpY2UgZm9yIFRyaXRvbiBHUFUgaW5mZXJlbmNlIChzY2FsZXMgYmFzZWQgb24gZGVtYW5kKVxuICAgIGNvbnN0IHNlcnZpY2UgPSBuZXcgZWNzLkVjMlNlcnZpY2UodGhpcywgJ1RyaXRvbkluZmVyZW5jZVNlcnZpY2UnLCB7XG4gICAgICBjbHVzdGVyLFxuICAgICAgdGFza0RlZmluaXRpb24sXG4gICAgICBzZXJ2aWNlTmFtZTogJ3RyaXRvbi1pbmZlcmVuY2Utc2VydmljZScsXG4gICAgICBkZXNpcmVkQ291bnQ6IDAsIC8vIFN0YXJ0IHdpdGggMCwgc2NhbGUgdXAgYmFzZWQgb24gQUxCIHJlcXVlc3RzXG4gICAgICBzZWN1cml0eUdyb3VwczogW2Vjc1NlY3VyaXR5R3JvdXBdLFxuICAgICAgdnBjU3VibmV0czogeyBzdWJuZXRUeXBlOiBlYzIuU3VibmV0VHlwZS5QUklWQVRFX1dJVEhfRUdSRVNTIH0sXG4gICAgICBjYXBhY2l0eVByb3ZpZGVyU3RyYXRlZ2llczogW3tcbiAgICAgICAgY2FwYWNpdHlQcm92aWRlcjogY2FwYWNpdHlQcm92aWRlci5jYXBhY2l0eVByb3ZpZGVyTmFtZSxcbiAgICAgICAgd2VpZ2h0OiAxXG4gICAgICB9LHtcbiAgICAgICAgY2FwYWNpdHlQcm92aWRlcjogY3BHNXhsYXJnZS5jYXBhY2l0eVByb3ZpZGVyTmFtZSxcbiAgICAgICAgd2VpZ2h0OiAxXG4gICAgICB9LHtcbiAgICAgICAgY2FwYWNpdHlQcm92aWRlcjogY3BHNTJ4bGFyZ2UuY2FwYWNpdHlQcm92aWRlck5hbWUsXG4gICAgICAgIHdlaWdodDogMVxuICAgICAgfV0sXG4gICAgICBwbGFjZW1lbnRTdHJhdGVnaWVzOiBbXG4gICAgICAgIGVjcy5QbGFjZW1lbnRTdHJhdGVneS5zcHJlYWRBY3Jvc3NJbnN0YW5jZXMoKVxuICAgICAgXSxcbiAgICAgIG1pbkhlYWx0aHlQZXJjZW50OiAxMDAsXG4gICAgICBtYXhIZWFsdGh5UGVyY2VudDogMjAwLFxuICAgICAgZW5hYmxlRXhlY3V0ZUNvbW1hbmQ6IHRydWVcbiAgICB9KTtcblxuICAgIC8vIEF0dGFjaCBFQ1Mgc2VydmljZSB0byBBTEIgdGFyZ2V0IGdyb3VwXG4gICAgc2VydmljZS5hdHRhY2hUb0FwcGxpY2F0aW9uVGFyZ2V0R3JvdXAodHJpdG9uVGFyZ2V0R3JvdXApO1xuXG4gICAgLy8gQXV0b3NjYWxlIFRyaXRvbiBzZXJ2aWNlIGJhc2VkIG9uIEFMQiByZXF1ZXN0IG1ldHJpY3NcbiAgICBjb25zdCBzY2FsYWJsZVRhcmdldCA9IG5ldyBhcHBzY2FsaW5nLlNjYWxhYmxlVGFyZ2V0KHRoaXMsICdUcml0b25TY2FsYWJsZVRhcmdldCcsIHtcbiAgICAgIHNlcnZpY2VOYW1lc3BhY2U6IGFwcHNjYWxpbmcuU2VydmljZU5hbWVzcGFjZS5FQ1MsXG4gICAgICBtYXhDYXBhY2l0eTogMTAsXG4gICAgICBtaW5DYXBhY2l0eTogMCxcbiAgICAgIHJlc291cmNlSWQ6IGBzZXJ2aWNlLyR7Y2x1c3Rlci5jbHVzdGVyTmFtZX0vJHtzZXJ2aWNlLnNlcnZpY2VOYW1lfWAsXG4gICAgICBzY2FsYWJsZURpbWVuc2lvbjogJ2VjczpzZXJ2aWNlOkRlc2lyZWRDb3VudCdcbiAgICB9KTtcblxuICAgIC8vIFNjYWxlIGJhc2VkIG9uIEFMQiByZXF1ZXN0IGNvdW50IHBlciB0YXJnZXRcbiAgICBjb25zdCBhbGJSZXF1ZXN0TWV0cmljID0gbmV3IGN3Lk1ldHJpYyh7XG4gICAgICBuYW1lc3BhY2U6ICdBV1MvQXBwbGljYXRpb25FTEInLFxuICAgICAgbWV0cmljTmFtZTogJ1JlcXVlc3RDb3VudFBlclRhcmdldCcsXG4gICAgICBkaW1lbnNpb25zTWFwOiB7XG4gICAgICAgIExvYWRCYWxhbmNlcjogdHJpdG9uQWxiLmxvYWRCYWxhbmNlckZ1bGxOYW1lLFxuICAgICAgICBUYXJnZXRHcm91cDogdHJpdG9uVGFyZ2V0R3JvdXAudGFyZ2V0R3JvdXBGdWxsTmFtZVxuICAgICAgfSxcbiAgICAgIHN0YXRpc3RpYzogJ1N1bScsXG4gICAgICBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDEpXG4gICAgfSk7XG5cbiAgICBzY2FsYWJsZVRhcmdldC5zY2FsZVRvVHJhY2tNZXRyaWMoJ1RyaXRvblNjYWxpbmcnLCB7XG4gICAgICBjdXN0b21NZXRyaWM6IGFsYlJlcXVlc3RNZXRyaWMsXG4gICAgICB0YXJnZXRWYWx1ZTogNTAsXG4gICAgICBzY2FsZUluQ29vbGRvd246IGNkay5EdXJhdGlvbi5taW51dGVzKDIpLFxuICAgICAgc2NhbGVPdXRDb29sZG93bjogY2RrLkR1cmF0aW9uLnNlY29uZHMoMzApXG4gICAgfSk7XG5cbiAgICAvLyBBZGRpdGlvbmFsIHNjYWxpbmcgcG9saWN5IGJhc2VkIG9uIFRyaXRvbiBwOTUgbGF0ZW5jeVxuICAgIGNvbnN0IHRyaXRvbkxhdGVuY3lQOTVNZXRyaWMgPSBuZXcgY3cuTWV0cmljKHtcbiAgICAgIG5hbWVzcGFjZTogJ0NXQWdlbnQnLFxuICAgICAgbWV0cmljTmFtZTogJ252X2luZmVyZW5jZV9yZXF1ZXN0X2R1cmF0aW9uX3VzJyxcbiAgICAgIHN0YXRpc3RpYzogJ3A5NScsXG4gICAgICBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDEpXG4gICAgfSk7XG4gICAgY29uc3QgbGF0ZW5jeUhpZ2ggPSBuZXcgY3cuQWxhcm0odGhpcywgJ1RyaXRvbkxhdGVuY3lIaWdoRm9yU2NhbGluZycsIHtcbiAgICAgIG1ldHJpYzogdHJpdG9uTGF0ZW5jeVA5NU1ldHJpYyxcbiAgICAgIHRocmVzaG9sZDogNTAwMDAwLCAvLyA1MDBtc1xuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDMsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fVEhSRVNIT0xEXG4gICAgfSk7XG4gICAgY29uc3QgbGF0ZW5jeUxvdyA9IG5ldyBjdy5BbGFybSh0aGlzLCAnVHJpdG9uTGF0ZW5jeUxvd0ZvclNjYWxpbmcnLCB7XG4gICAgICBtZXRyaWM6IHRyaXRvbkxhdGVuY3lQOTVNZXRyaWMsXG4gICAgICB0aHJlc2hvbGQ6IDIwMDAwMCwgLy8gMjAwbXNcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiA1LFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuTEVTU19USEFOX09SX0VRVUFMX1RPX1RIUkVTSE9MRFxuICAgIH0pO1xuICAgIHNjYWxhYmxlVGFyZ2V0LnNjYWxlT25NZXRyaWMoJ0xhdGVuY3lTY2FsZU91dCcsIHtcbiAgICAgIG1ldHJpYzogdHJpdG9uTGF0ZW5jeVA5NU1ldHJpYyxcbiAgICAgIHNjYWxpbmdTdGVwczogW1xuICAgICAgICB7IGxvd2VyOiA1MDAwMDAsIGNoYW5nZTogKzEgfSxcbiAgICAgICAgeyBsb3dlcjogODAwMDAwLCBjaGFuZ2U6ICsyIH1cbiAgICAgIF0sXG4gICAgICBhZGp1c3RtZW50VHlwZTogYXBwc2NhbGluZy5BZGp1c3RtZW50VHlwZS5DSEFOR0VfSU5fQ0FQQUNJVFksXG4gICAgICBjb29sZG93bjogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMiksXG4gICAgICBtaW5BZGp1c3RtZW50TWFnbml0dWRlOiAxXG4gICAgfSk7XG5cbiAgICAvLyBHUFUgdXRpbGl6YXRpb24tYmFzZWQgc2NhbGluZyBmb3Igb3B0aW1hbCByZXNvdXJjZSBtYW5hZ2VtZW50XG4gICAgLy8gU2NhbGUgb3V0IHdoZW4gR1BVIHV0aWxpemF0aW9uIGlzIGhpZ2ggdG8gcHJldmVudCBib3R0bGVuZWNrc1xuICAgIC8vIFNjYWxlIGluIHdoZW4gR1BVIHV0aWxpemF0aW9uIGlzIGxvdyB0byByZWR1Y2UgY29zdHNcbiAgICBjb25zdCBncHVVdGlsRm9yU2NhbGluZyA9IG5ldyBjdy5NZXRyaWMoe1xuICAgICAgbmFtZXNwYWNlOiAnQ1dBZ2VudCcsXG4gICAgICBtZXRyaWNOYW1lOiAnRENHTV9GSV9ERVZfR1BVX1VUSUwnLFxuICAgICAgc3RhdGlzdGljOiAnQXZlcmFnZScsXG4gICAgICBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDEpXG4gICAgfSk7XG4gICAgc2NhbGFibGVUYXJnZXQuc2NhbGVPbk1ldHJpYygnR3B1VXRpbFNjYWxpbmcnLCB7XG4gICAgICBtZXRyaWM6IGdwdVV0aWxGb3JTY2FsaW5nLFxuICAgICAgc2NhbGluZ1N0ZXBzOiBbXG4gICAgICAgIHsgbG93ZXI6IDcwLCBjaGFuZ2U6ICsxIH0sICAgLy8gQWRkIDEgdGFzayB3aGVuIEdQVSB1dGlsaXphdGlvbiA+IDcwJVxuICAgICAgICB7IGxvd2VyOiA5MCwgY2hhbmdlOiArMiB9LCAgIC8vIEFkZCAyIG1vcmUgdGFza3Mgd2hlbiBHUFUgdXRpbGl6YXRpb24gPiA5MCVcbiAgICAgICAgeyB1cHBlcjogMzAsIGNoYW5nZTogLTEgfSAgICAvLyBSZW1vdmUgMSB0YXNrIHdoZW4gR1BVIHV0aWxpemF0aW9uIDwgMzAlXG4gICAgICBdLFxuICAgICAgYWRqdXN0bWVudFR5cGU6IGFwcHNjYWxpbmcuQWRqdXN0bWVudFR5cGUuQ0hBTkdFX0lOX0NBUEFDSVRZLFxuICAgICAgY29vbGRvd246IGNkay5EdXJhdGlvbi5taW51dGVzKDIpLFxuICAgICAgbWluQWRqdXN0bWVudE1hZ25pdHVkZTogMVxuICAgIH0pO1xuXG4gICAgLy8gQWRkIG91dHB1dCBmb3IgVHJpdG9uIGNsdXN0ZXIgVVJMXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1RyaXRvbkNsdXN0ZXJVcmwnLCB7XG4gICAgICB2YWx1ZTogYGh0dHBzOi8vJHt0cml0b25BbGIubG9hZEJhbGFuY2VyRG5zTmFtZX1gLFxuICAgICAgZGVzY3JpcHRpb246ICdIVFRQUyBVUkwgZm9yIFRyaXRvbiBHUFUgaW5mZXJlbmNlIGNsdXN0ZXInLFxuICAgICAgY29uZGl0aW9uOiB1c2VUcml0b25Db25kaXRpb25cbiAgICB9KTtcblxuICAgIC8vIFB1Ymxpc2ggVHJpdG9uIFVSTCB0byBTU00gZm9yIGNyb3NzLXN0YWNrIGxpbmtpbmdcbiAgICBuZXcgc3NtLlN0cmluZ1BhcmFtZXRlcih0aGlzLCAnVHJpdG9uQWxiVXJsUGFyYW0nLCB7XG4gICAgICBwYXJhbWV0ZXJOYW1lOiAnL2FtaXJhL3RyaXRvbl9hbGJfdXJsJyxcbiAgICAgIHN0cmluZ1ZhbHVlOiBgaHR0cHM6Ly8ke3RyaXRvbkFsYi5sb2FkQmFsYW5jZXJEbnNOYW1lfWBcbiAgICB9KTtcblxuICAgIC8vIFB1Ymxpc2ggVlBDIGFuZCBzdWJuZXQgYXR0cmlidXRlcyBmb3IgY3Jvc3Mtc3RhY2sgTGFtYmRhIGF0dGFjaG1lbnRcbiAgICBuZXcgc3NtLlN0cmluZ1BhcmFtZXRlcih0aGlzLCAnVnBjSWRQYXJhbScsIHtcbiAgICAgIHBhcmFtZXRlck5hbWU6ICcvYW1pcmEvdnBjX2lkJyxcbiAgICAgIHN0cmluZ1ZhbHVlOiB2cGMudnBjSWRcbiAgICB9KTtcbiAgICBuZXcgc3NtLlN0cmluZ1BhcmFtZXRlcih0aGlzLCAnVnBjUHJpdmF0ZVN1Ym5ldElkc1BhcmFtJywge1xuICAgICAgcGFyYW1ldGVyTmFtZTogJy9hbWlyYS92cGNfcHJpdmF0ZV9zdWJuZXRfaWRzJyxcbiAgICAgIHN0cmluZ1ZhbHVlOiB2cGMucHJpdmF0ZVN1Ym5ldHMubWFwKHMgPT4gcy5zdWJuZXRJZCkuam9pbignLCcpXG4gICAgfSk7XG4gICAgbmV3IHNzbS5TdHJpbmdQYXJhbWV0ZXIodGhpcywgJ0FsYlNlY3VyaXR5R3JvdXBJZFBhcmFtJywge1xuICAgICAgcGFyYW1ldGVyTmFtZTogJy9hbWlyYS9hbGJfc2dfaWQnLFxuICAgICAgc3RyaW5nVmFsdWU6IGFsYlNlY3VyaXR5R3JvdXAuc2VjdXJpdHlHcm91cElkXG4gICAgfSk7XG5cbiAgICAvLyBHUFUgY2x1c3RlciBtb25pdG9yaW5nIGFuZCBhbGFybXNcblxuICAgIC8vIFNOUyBub3RpZmljYXRpb25zIGZvciBhbGFybXNcbiAgICBjb25zdCBhbGFybVRvcGljID0gbmV3IHNucy5Ub3BpYyh0aGlzLCAnT3BzQWxhcm1Ub3BpYycsIHsgZGlzcGxheU5hbWU6ICdUcml0b24gR1BVIENsdXN0ZXIgQWxhcm1zJyB9KTtcbiAgICBjb25zdCBhbGFybUFjdGlvbiA9IG5ldyBjd2FjdGlvbnMuU25zQWN0aW9uKGFsYXJtVG9waWMpO1xuICAgIC8vIE9wdGlvbmFsIFNlY3JldHMgTWFuYWdlciByb3RhdGlvbiBhbGFybSAoaWYgc2VjcmV0IEFSTiBwcm92aWRlZClcbiAgICBjb25zdCByb3RhdGlvbkFsYXJtRW1haWxQYXJhbSA9IG5ldyBjZGsuQ2ZuUGFyYW1ldGVyKHRoaXMsICdSb3RhdGlvbkFsYXJtRW1haWwnLCB7XG4gICAgICB0eXBlOiAnU3RyaW5nJyxcbiAgICAgIGRlZmF1bHQ6ICcnLFxuICAgICAgZGVzY3JpcHRpb246ICdPcHRpb25hbCBlbWFpbCBmb3IgU2VjcmV0cyBNYW5hZ2VyIHJvdGF0aW9uIGZhaWx1cmUgYWxhcm1zJ1xuICAgIH0pO1xuICAgIGNvbnN0IHJvdGF0aW9uQWxhcm1FbWFpbFByb3ZpZGVkID0gbmV3IGNkay5DZm5Db25kaXRpb24odGhpcywgJ1JvdGF0aW9uQWxhcm1FbWFpbFByb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKHJvdGF0aW9uQWxhcm1FbWFpbFBhcmFtLnZhbHVlQXNTdHJpbmcsICcnKSlcbiAgICB9KTtcbiAgICBjb25zdCByb3RhdGlvbkZhaWx1cmVzTWV0cmljID0gbmV3IGN3Lk1ldHJpYyh7XG4gICAgICBuYW1lc3BhY2U6ICdBV1MvU2VjcmV0c01hbmFnZXInLFxuICAgICAgbWV0cmljTmFtZTogJ1JvdGF0aW9uRW5hYmxlZCcsXG4gICAgICBzdGF0aXN0aWM6ICdNaW5pbXVtJyxcbiAgICAgIHBlcmlvZDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoNSlcbiAgICB9KTtcbiAgICBjb25zdCByb3RhdGlvbkFsYXJtID0gbmV3IGN3LkFsYXJtKHRoaXMsICdTZWNyZXRzUm90YXRpb25EaXNhYmxlZCcsIHtcbiAgICAgIG1ldHJpYzogcm90YXRpb25GYWlsdXJlc01ldHJpYyxcbiAgICAgIHRocmVzaG9sZDogMSxcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAxLFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuTEVTU19USEFOX1RIUkVTSE9MRCxcbiAgICAgIGFsYXJtRGVzY3JpcHRpb246ICdTZWNyZXRzIE1hbmFnZXIgcm90YXRpb24gZGlzYWJsZWQgb3IgZmFpbGluZydcbiAgICB9KTtcbiAgICByb3RhdGlvbkFsYXJtLmFkZEFsYXJtQWN0aW9uKGFsYXJtQWN0aW9uKTtcblxuICAgIC8vIE9wdGlvbmFsIGVtYWlsIHN1YnNjcmlwdGlvbiBwYXJhbWV0ZXJcbiAgICBjb25zdCBhbGFybUVtYWlsUGFyYW0gPSBuZXcgY2RrLkNmblBhcmFtZXRlcih0aGlzLCAnQWxhcm1FbWFpbCcsIHtcbiAgICAgIHR5cGU6ICdTdHJpbmcnLFxuICAgICAgZGVmYXVsdDogJycsXG4gICAgICBkZXNjcmlwdGlvbjogJ09wdGlvbmFsIGVtYWlsIHRvIHN1YnNjcmliZSB0byBPcHMgYWxhcm1zJ1xuICAgIH0pO1xuICAgIGNvbnN0IGVtYWlsUHJvdmlkZWQgPSBuZXcgY2RrLkNmbkNvbmRpdGlvbih0aGlzLCAnQWxhcm1FbWFpbFByb3ZpZGVkJywge1xuICAgICAgZXhwcmVzc2lvbjogY2RrLkZuLmNvbmRpdGlvbk5vdChjZGsuRm4uY29uZGl0aW9uRXF1YWxzKGFsYXJtRW1haWxQYXJhbS52YWx1ZUFzU3RyaW5nLCAnJykpXG4gICAgfSk7XG4gICAgY29uc3Qgc3Vic2NyaXB0aW9uID0gbmV3IHNucy5DZm5TdWJzY3JpcHRpb24odGhpcywgJ09wc0FsYXJtRW1haWxTdWJzY3JpcHRpb24nLCB7XG4gICAgICBwcm90b2NvbDogJ2VtYWlsJyxcbiAgICAgIHRvcGljQXJuOiBhbGFybVRvcGljLnRvcGljQXJuLFxuICAgICAgZW5kcG9pbnQ6IGFsYXJtRW1haWxQYXJhbS52YWx1ZUFzU3RyaW5nXG4gICAgfSk7XG4gICAgc3Vic2NyaXB0aW9uLmNmbk9wdGlvbnMuY29uZGl0aW9uID0gZW1haWxQcm92aWRlZDtcblxuICAgIC8vIE1hbnVhbCBlbnF1ZXVlIExhbWJkYSBmb3IgdGVzdGluZyAoYWNjZXB0cyBKU09OIHtcImFjdGl2aXR5X2lkc1wiOiBbXCIuLi5cIiwgLi4uXX0pXG4gICAgY29uc3QgbWFudWFsRW5xdWV1ZUZuID0gbmV3IGxhbWJkYS5GdW5jdGlvbih0aGlzLCAnTWFudWFsRW5xdWV1ZUZ1bmN0aW9uJywge1xuICAgICAgcnVudGltZTogbGFtYmRhLlJ1bnRpbWUuUFlUSE9OXzNfMTIsXG4gICAgICBoYW5kbGVyOiAnaW5kZXguaGFuZGxlcicsXG4gICAgICBjb2RlOiBsYW1iZGEuQ29kZS5mcm9tQXNzZXQoJy4uL2xhbWJkYS9tYW51YWxfZW5xdWV1ZScpLFxuICAgICAgdGltZW91dDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMSksXG4gICAgICB0cmFjaW5nOiBsYW1iZGEuVHJhY2luZy5BQ1RJVkUsXG4gICAgICBlbnZpcm9ubWVudDogeyBKT0JTX1FVRVVFX1VSTDogam9ic1F1ZXVlLnF1ZXVlVXJsIH1cbiAgICB9KTtcbiAgICBqb2JzUXVldWUuZ3JhbnRTZW5kTWVzc2FnZXMobWFudWFsRW5xdWV1ZUZuKTtcbiAgICBjb25zdCBtYW51YWxVcmwgPSBtYW51YWxFbnF1ZXVlRm4uYWRkRnVuY3Rpb25Vcmwoe1xuICAgICAgYXV0aFR5cGU6IGxhbWJkYS5GdW5jdGlvblVybEF1dGhUeXBlLkFXU19JQU0sXG4gICAgICBjb3JzOiB7IGFsbG93ZWRPcmlnaW5zOiBbJyonXSwgYWxsb3dlZE1ldGhvZHM6IFtsYW1iZGEuSHR0cE1ldGhvZC5QT1NULCBsYW1iZGEuSHR0cE1ldGhvZC5PUFRJT05TXSB9XG4gICAgfSk7XG5cbiAgICAvLyBBbGFybXM6IEdQVSB1dGlsaXphdGlvbiBsb3cvaGlnaCAoZnJvbSBEQ0dNIHZpYSBDV0FnZW50IFByb21ldGhldXMpXG4gICAgY29uc3QgZ3B1VXRpbE1ldHJpYyA9IG5ldyBjdy5NZXRyaWMoe1xuICAgICAgbmFtZXNwYWNlOiAnQ1dBZ2VudCcsXG4gICAgICBtZXRyaWNOYW1lOiAnRENHTV9GSV9ERVZfR1BVX1VUSUwnLFxuICAgICAgc3RhdGlzdGljOiAnQXZlcmFnZScsXG4gICAgICBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDEpXG4gICAgfSk7XG4gICAgY29uc3QgZ3B1TWVtVXNlZCA9IG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdDV0FnZW50JywgbWV0cmljTmFtZTogJ0RDR01fRklfREVWX0ZCX1VTRUQnLCBzdGF0aXN0aWM6ICdBdmVyYWdlJywgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKSB9KTtcbiAgICBjb25zdCBncHVNZW1Ub3RhbCA9IG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdDV0FnZW50JywgbWV0cmljTmFtZTogJ0RDR01fRklfREVWX0ZCX1RPVEFMJywgc3RhdGlzdGljOiAnQXZlcmFnZScsIHBlcmlvZDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMSkgfSk7XG4gICAgY29uc3QgZ3B1VXRpbExvdyA9IG5ldyBjdy5BbGFybSh0aGlzLCAnR3B1VXRpbExvdycsIHtcbiAgICAgIG1ldHJpYzogZ3B1VXRpbE1ldHJpYyxcbiAgICAgIHRocmVzaG9sZDogMjAsXG4gICAgICBldmFsdWF0aW9uUGVyaW9kczogNSxcbiAgICAgIGNvbXBhcmlzb25PcGVyYXRvcjogY3cuQ29tcGFyaXNvbk9wZXJhdG9yLkxFU1NfVEhBTl9USFJFU0hPTERcbiAgICB9KTtcbiAgICBncHVVdGlsTG93LmFkZEFsYXJtQWN0aW9uKGFsYXJtQWN0aW9uKTtcbiAgICBjb25zdCBncHVVdGlsSGlnaCA9IG5ldyBjdy5BbGFybSh0aGlzLCAnR3B1VXRpbEhpZ2gnLCB7XG4gICAgICBtZXRyaWM6IGdwdVV0aWxNZXRyaWMsXG4gICAgICB0aHJlc2hvbGQ6IDk1LFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDMsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fVEhSRVNIT0xEXG4gICAgfSk7XG4gICAgZ3B1VXRpbEhpZ2guYWRkQWxhcm1BY3Rpb24oYWxhcm1BY3Rpb24pO1xuXG4gICAgLy8gVHJpdG9uIHJlcXVlc3QgbGF0ZW5jeSAoOTV0aCBwZXJjZW50aWxlLCBtaWNyb3NlY29uZHMpXG4gICAgY29uc3QgdHJpdG9uUDk1ID0gbmV3IGN3Lk1ldHJpYyh7XG4gICAgICBuYW1lc3BhY2U6ICdDV0FnZW50JyxcbiAgICAgIG1ldHJpY05hbWU6ICdudl9pbmZlcmVuY2VfcmVxdWVzdF9kdXJhdGlvbl91cycsXG4gICAgICBzdGF0aXN0aWM6ICdwOTUnLFxuICAgICAgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKVxuICAgIH0pO1xuICAgIGNvbnN0IHRyaXRvblF1ZXVlUDk1ID0gbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0NXQWdlbnQnLCBtZXRyaWNOYW1lOiAnbnZfaW5mZXJlbmNlX3F1ZXVlX2R1cmF0aW9uX3VzJywgc3RhdGlzdGljOiAncDk1JywgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKSB9KTtcbiAgICBjb25zdCB0cml0b25UaHJvdWdocHV0ID0gbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0NXQWdlbnQnLCBtZXRyaWNOYW1lOiAnbnZfaW5mZXJlbmNlX2NvdW50Jywgc3RhdGlzdGljOiAnU3VtJywgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKSB9KTtcbiAgICBjb25zdCB0cml0b25GYWlsdXJlcyA9IG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdDV0FnZW50JywgbWV0cmljTmFtZTogJ252X2luZmVyZW5jZV9mYWlsJywgc3RhdGlzdGljOiAnU3VtJywgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcygxKSB9KTtcbiAgICBjb25zdCB0cml0b25MYXRlbmN5SGlnaCA9IG5ldyBjdy5BbGFybSh0aGlzLCAnVHJpdG9uTGF0ZW5jeUhpZ2gnLCB7XG4gICAgICBtZXRyaWM6IHRyaXRvblA5NSxcbiAgICAgIHRocmVzaG9sZDogNTAwMDAwLCAvLyA1MDBtc1xuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDUsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fVEhSRVNIT0xEXG4gICAgfSk7XG4gICAgdHJpdG9uTGF0ZW5jeUhpZ2guYWRkQWxhcm1BY3Rpb24oYWxhcm1BY3Rpb24pO1xuICAgIGNvbnN0IHRyaXRvblF1ZXVlSGlnaCA9IG5ldyBjdy5BbGFybSh0aGlzLCAnVHJpdG9uUXVldWVMYXRlbmN5SGlnaCcsIHtcbiAgICAgIG1ldHJpYzogdHJpdG9uUXVldWVQOTUsXG4gICAgICB0aHJlc2hvbGQ6IDIwMDAwMCwgLy8gMjAwbXNcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiA1LFxuICAgICAgY29tcGFyaXNvbk9wZXJhdG9yOiBjdy5Db21wYXJpc29uT3BlcmF0b3IuR1JFQVRFUl9USEFOX1RIUkVTSE9MRFxuICAgIH0pO1xuICAgIHRyaXRvblF1ZXVlSGlnaC5hZGRBbGFybUFjdGlvbihhbGFybUFjdGlvbik7XG4gICAgY29uc3QgdHJpdG9uRmFpbHVyZXNIaWdoID0gbmV3IGN3LkFsYXJtKHRoaXMsICdUcml0b25GYWlsdXJlc0hpZ2gnLCB7XG4gICAgICBtZXRyaWM6IHRyaXRvbkZhaWx1cmVzLFxuICAgICAgdGhyZXNob2xkOiAxLFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDEsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fT1JfRVFVQUxfVE9fVEhSRVNIT0xEXG4gICAgfSk7XG4gICAgdHJpdG9uRmFpbHVyZXNIaWdoLmFkZEFsYXJtQWN0aW9uKGFsYXJtQWN0aW9uKTtcblxuICAgIC8vIEFsYXJtIGZvciBETFEgZGVwdGggaW4gR1BVIHN0YWNrXG4gICAgY29uc3Qgam9ic0RscURlcHRoQWxhcm0gPSBuZXcgY3cuQWxhcm0odGhpcywgJ0pvYnNEbHFEZXB0aEFsYXJtJywge1xuICAgICAgbWV0cmljOiBkbHEubWV0cmljQXBwcm94aW1hdGVOdW1iZXJPZk1lc3NhZ2VzVmlzaWJsZSgpLFxuICAgICAgdGhyZXNob2xkOiAxLFxuICAgICAgZXZhbHVhdGlvblBlcmlvZHM6IDEsXG4gICAgICBjb21wYXJpc29uT3BlcmF0b3I6IGN3LkNvbXBhcmlzb25PcGVyYXRvci5HUkVBVEVSX1RIQU5fT1JfRVFVQUxfVE9fVEhSRVNIT0xEXG4gICAgfSk7XG4gICAgam9ic0RscURlcHRoQWxhcm0uYWRkQWxhcm1BY3Rpb24oYWxhcm1BY3Rpb24pO1xuXG4gICAgLy8gRGFzaGJvYXJkIGZvciBHUFUgYW5kIFRyaXRvblxuICAgIGNvbnN0IGRhc2hib2FyZCA9IG5ldyBjd19kYXNoLkRhc2hib2FyZCh0aGlzLCAnQW1pcmFHcHVUcml0b25EYXNoYm9hcmQnLCB7IGRhc2hib2FyZE5hbWU6ICdBbWlyYUdwdVRyaXRvbicgfSk7XG4gICAgZGFzaGJvYXJkLmFkZFdpZGdldHMoXG4gICAgICBuZXcgY3dfZGFzaC5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnR1BVIFV0aWxpemF0aW9uJyxcbiAgICAgICAgbGVmdDogW2dwdVV0aWxNZXRyaWNdLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3X2Rhc2guR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ1RyaXRvbiBwOTUgTGF0ZW5jeSAodXMpJyxcbiAgICAgICAgbGVmdDogW3RyaXRvblA5NV0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSksXG4gICAgICBuZXcgY3dfZGFzaC5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnR1BVIE1lbW9yeSAoYnl0ZXMpJyxcbiAgICAgICAgbGVmdDogW2dwdU1lbVVzZWQsIGdwdU1lbVRvdGFsXSxcbiAgICAgICAgd2lkdGg6IDEyXG4gICAgICB9KSxcbiAgICAgIG5ldyBjd19kYXNoLkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdUcml0b24gUXVldWUgcDk1ICh1cyknLFxuICAgICAgICBsZWZ0OiBbdHJpdG9uUXVldWVQOTVdLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3X2Rhc2guR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ1RyaXRvbiBUaHJvdWdocHV0IChyZXEvbWluKSAmIEZhaWx1cmVzJyxcbiAgICAgICAgbGVmdDogW3RyaXRvblRocm91Z2hwdXRdLFxuICAgICAgICByaWdodDogW3RyaXRvbkZhaWx1cmVzXSxcbiAgICAgICAgd2lkdGg6IDI0XG4gICAgICB9KSxcbiAgICAgIG5ldyBjd19kYXNoLkdyYXBoV2lkZ2V0KHtcbiAgICAgICAgdGl0bGU6ICdJbmZlcmVuY2UgU0xPcyAocDk1IG1zKScsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQW1pcmEvSW5mZXJlbmNlJywgbWV0cmljTmFtZTogJ0luZmVyZW5jZVRvdGFsTXMnLCBzdGF0aXN0aWM6ICdwOTUnLCBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDEpIH0pLFxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMTJcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3X2Rhc2guR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0FjdGl2aXR5IFNMT3MgKHA5NSBtcyknLFxuICAgICAgICBsZWZ0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0FtaXJhL0FjdGl2aXR5JywgbWV0cmljTmFtZTogJ0FjdGl2aXR5VG90YWxNcycsIHN0YXRpc3RpYzogJ3A5NScsIHBlcmlvZDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMSkgfSksXG4gICAgICAgIF0sXG4gICAgICAgIHdpZHRoOiAxMlxuICAgICAgfSksXG4gICAgICBuZXcgY3dfZGFzaC5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnRUNTIERlc2lyZWQgdnMgUnVubmluZycsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnRUNTL0NvbnRhaW5lckluc2lnaHRzJywgbWV0cmljTmFtZTogJ1NlcnZpY2VEZXNpcmVkQ291bnQnLCBkaW1lbnNpb25zTWFwOiB7IENsdXN0ZXJOYW1lOiBjbHVzdGVyLmNsdXN0ZXJOYW1lLCBTZXJ2aWNlTmFtZTogc2VydmljZS5zZXJ2aWNlTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KSxcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnRUNTL0NvbnRhaW5lckluc2lnaHRzJywgbWV0cmljTmFtZTogJ1NlcnZpY2VSdW5uaW5nQ291bnQnLCBkaW1lbnNpb25zTWFwOiB7IENsdXN0ZXJOYW1lOiBjbHVzdGVyLmNsdXN0ZXJOYW1lLCBTZXJ2aWNlTmFtZTogc2VydmljZS5zZXJ2aWNlTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KVxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMjRcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3X2Rhc2guR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ1NRUyBEZXB0aCAmIE9sZGVzdCBBZ2UnLFxuICAgICAgICBsZWZ0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0FXUy9TUVMnLCBtZXRyaWNOYW1lOiAnQXBwcm94aW1hdGVOdW1iZXJPZk1lc3NhZ2VzVmlzaWJsZScsIGRpbWVuc2lvbnNNYXA6IHsgUXVldWVOYW1lOiBqb2JzUXVldWUucXVldWVOYW1lIH0sIHN0YXRpc3RpYzogJ0F2ZXJhZ2UnIH0pXG4gICAgICAgIF0sXG4gICAgICAgIHJpZ2h0OiBbXG4gICAgICAgICAgbmV3IGN3Lk1ldHJpYyh7IG5hbWVzcGFjZTogJ0FXUy9TUVMnLCBtZXRyaWNOYW1lOiAnQXBwcm94aW1hdGVBZ2VPZk9sZGVzdE1lc3NhZ2UnLCBkaW1lbnNpb25zTWFwOiB7IFF1ZXVlTmFtZTogam9ic1F1ZXVlLnF1ZXVlTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KVxuICAgICAgICBdLFxuICAgICAgICB3aWR0aDogMjRcbiAgICAgIH0pLFxuICAgICAgbmV3IGN3X2Rhc2guR3JhcGhXaWRnZXQoe1xuICAgICAgICB0aXRsZTogJ0FTRyBEZXNpcmVkIHZzIEluU2VydmljZScsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQVdTL0F1dG9TY2FsaW5nJywgbWV0cmljTmFtZTogJ0dyb3VwRGVzaXJlZENhcGFjaXR5JywgZGltZW5zaW9uc01hcDogeyBBdXRvU2NhbGluZ0dyb3VwTmFtZTogYXV0b1NjYWxpbmdHcm91cC5hdXRvU2NhbGluZ0dyb3VwTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KSxcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQVdTL0F1dG9TY2FsaW5nJywgbWV0cmljTmFtZTogJ0dyb3VwSW5TZXJ2aWNlSW5zdGFuY2VzJywgZGltZW5zaW9uc01hcDogeyBBdXRvU2NhbGluZ0dyb3VwTmFtZTogYXV0b1NjYWxpbmdHcm91cC5hdXRvU2NhbGluZ0dyb3VwTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KVxuICAgICAgICBdLFxuICAgICAgICByaWdodDogW1xuICAgICAgICAgIG5ldyBjdy5NZXRyaWMoeyBuYW1lc3BhY2U6ICdBV1MvQXV0b1NjYWxpbmcnLCBtZXRyaWNOYW1lOiAnR3JvdXBEZXNpcmVkQ2FwYWNpdHknLCBkaW1lbnNpb25zTWFwOiB7IEF1dG9TY2FsaW5nR3JvdXBOYW1lOiBhc2dHNXhsYXJnZS5hdXRvU2NhbGluZ0dyb3VwTmFtZSB9LCBzdGF0aXN0aWM6ICdBdmVyYWdlJyB9KSxcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQVdTL0F1dG9TY2FsaW5nJywgbWV0cmljTmFtZTogJ0dyb3VwRGVzaXJlZENhcGFjaXR5JywgZGltZW5zaW9uc01hcDogeyBBdXRvU2NhbGluZ0dyb3VwTmFtZTogYXNnRzUyeGxhcmdlLmF1dG9TY2FsaW5nR3JvdXBOYW1lIH0sIHN0YXRpc3RpYzogJ0F2ZXJhZ2UnIH0pXG4gICAgICAgIF0sXG4gICAgICAgIHdpZHRoOiAyNFxuICAgICAgfSksXG4gICAgICBuZXcgY3dfZGFzaC5HcmFwaFdpZGdldCh7XG4gICAgICAgIHRpdGxlOiAnTGFtYmRhIEludm9jYXRpb25zL0Vycm9ycycsXG4gICAgICAgIGxlZnQ6IFtcbiAgICAgICAgICBtYW51YWxFbnF1ZXVlRm4ubWV0cmljSW52b2NhdGlvbnMoKSxcbiAgICAgICAgICBtYW51YWxFbnF1ZXVlRm4ubWV0cmljRXJyb3JzKClcbiAgICAgICAgXSxcbiAgICAgICAgcmlnaHQ6IFtcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQVdTL0xhbWJkYScsIG1ldHJpY05hbWU6ICdJbnZvY2F0aW9ucycsIGRpbWVuc2lvbnNNYXA6IHsgRnVuY3Rpb25OYW1lOiAnRWNzRHJhaW5PblNwb3RGbicgfSwgc3RhdGlzdGljOiAnU3VtJyB9KSxcbiAgICAgICAgICBuZXcgY3cuTWV0cmljKHsgbmFtZXNwYWNlOiAnQVdTL0xhbWJkYScsIG1ldHJpY05hbWU6ICdFcnJvcnMnLCBkaW1lbnNpb25zTWFwOiB7IEZ1bmN0aW9uTmFtZTogJ0Vjc0RyYWluT25TcG90Rm4nIH0sIHN0YXRpc3RpYzogJ1N1bScgfSlcbiAgICAgICAgXSxcbiAgICAgICAgd2lkdGg6IDI0XG4gICAgICB9KVxuICAgICk7XG5cbiAgICAvLyBTcG90IElUTi9SZWJhbGFuY2UgZHJhaW4gTGFtYmRhXG4gICAgY29uc3QgZHJhaW5GbiA9IG5ldyBsYW1iZGEuRnVuY3Rpb24odGhpcywgJ0Vjc0RyYWluT25TcG90Rm4nLCB7XG4gICAgICBydW50aW1lOiBsYW1iZGEuUnVudGltZS5QWVRIT05fM18xMixcbiAgICAgIGhhbmRsZXI6ICdpbmRleC5oYW5kbGVyJyxcbiAgICAgIGNvZGU6IGxhbWJkYS5Db2RlLmZyb21Bc3NldCgnLi4vbGFtYmRhL2Vjc19kcmFpbl9vbl9zcG90JyksXG4gICAgICB0aW1lb3V0OiBjZGsuRHVyYXRpb24uc2Vjb25kcyg2MCksXG4gICAgICBlbnZpcm9ubWVudDogeyBDTFVTVEVSX0FSTjogY2x1c3Rlci5jbHVzdGVyQXJuIH1cbiAgICB9KTtcbiAgICBkcmFpbkZuLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbJ2VjczpMaXN0Q29udGFpbmVySW5zdGFuY2VzJywgJ2VjczpEZXNjcmliZUNvbnRhaW5lckluc3RhbmNlcyddLFxuICAgICAgcmVzb3VyY2VzOiBbY2x1c3Rlci5jbHVzdGVyQXJuXVxuICAgIH0pKTtcbiAgICBkcmFpbkZuLmFkZFRvUm9sZVBvbGljeShuZXcgaWFtLlBvbGljeVN0YXRlbWVudCh7XG4gICAgICBhY3Rpb25zOiBbJ2VjczpVcGRhdGVDb250YWluZXJJbnN0YW5jZXNTdGF0ZSddLFxuICAgICAgcmVzb3VyY2VzOiBbJyonXSxcbiAgICAgIGNvbmRpdGlvbnM6IHsgU3RyaW5nRXF1YWxzOiB7ICdlY3M6Y2x1c3Rlcic6IGNsdXN0ZXIuY2x1c3RlckFybiB9IH1cbiAgICB9KSk7XG4gICAgbmV3IGV2ZW50cy5SdWxlKHRoaXMsICdTcG90SW50ZXJydXB0aW9uRHJhaW5SdWxlJywge1xuICAgICAgZGVzY3JpcHRpb246ICdEcmFpbiBFQ1MgY29udGFpbmVyIGluc3RhbmNlcyBvbiBTcG90IGludGVycnVwdGlvbiB3YXJuaW5ncycsXG4gICAgICBldmVudFBhdHRlcm46IHtcbiAgICAgICAgc291cmNlOiBbJ2F3cy5lYzInXSxcbiAgICAgICAgZGV0YWlsVHlwZTogWydFQzIgU3BvdCBJbnN0YW5jZSBJbnRlcnJ1cHRpb24gV2FybmluZycsICdFQzIgSW5zdGFuY2UgUmViYWxhbmNlIFJlY29tbWVuZGF0aW9uJ11cbiAgICAgIH0sXG4gICAgICB0YXJnZXRzOiBbbmV3IHRhcmdldHMuTGFtYmRhRnVuY3Rpb24oZHJhaW5GbildXG4gICAgfSk7XG5cbiAgICAvLyBPdXRwdXRzIGZvciBHUFUgY2x1c3RlciBhbmQgZGFzaGJvYXJkIGxpbmtcbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnVHJpdG9uUmVwb3NpdG9yeVVyaScsIHtcbiAgICAgIHZhbHVlOiB0cml0b25SZXBvc2l0b3J5LnJlcG9zaXRvcnlVcmksXG4gICAgICBkZXNjcmlwdGlvbjogJ1RyaXRvbiBFQ1IgUmVwb3NpdG9yeSBVUkknLFxuICAgICAgY29uZGl0aW9uOiB1c2VUcml0b25Db25kaXRpb25cbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdHcHVDbHVzdGVyTmFtZScsIHtcbiAgICAgIHZhbHVlOiBjbHVzdGVyLmNsdXN0ZXJOYW1lLFxuICAgICAgZGVzY3JpcHRpb246ICdHUFUgRUNTIENsdXN0ZXIgTmFtZScsXG4gICAgICBjb25kaXRpb246IHVzZVRyaXRvbkNvbmRpdGlvblxuICAgIH0pO1xuXG4gICAgbmV3IGNkay5DZm5PdXRwdXQodGhpcywgJ1RyaXRvblNlcnZpY2VOYW1lJywge1xuICAgICAgdmFsdWU6IHNlcnZpY2Uuc2VydmljZU5hbWUsXG4gICAgICBkZXNjcmlwdGlvbjogJ1RyaXRvbiBFQ1MgU2VydmljZSBOYW1lJyxcbiAgICAgIGNvbmRpdGlvbjogdXNlVHJpdG9uQ29uZGl0aW9uXG4gICAgfSk7XG5cbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnR3B1RGFzaGJvYXJkVXJsJywge1xuICAgICAgdmFsdWU6IGBodHRwczovLyR7dGhpcy5yZWdpb259LmNvbnNvbGUuYXdzLmFtYXpvbi5jb20vY2xvdWR3YXRjaC9ob21lP3JlZ2lvbj0ke3RoaXMucmVnaW9ufSNkYXNoYm9hcmRzOm5hbWU9JHtkYXNoYm9hcmQuZGFzaGJvYXJkTmFtZX1gLFxuICAgICAgZGVzY3JpcHRpb246ICdDbG91ZFdhdGNoIEdQVS9Ucml0b24gRGFzaGJvYXJkIFVSTCcsXG4gICAgICBjb25kaXRpb246IHVzZVRyaXRvbkNvbmRpdGlvblxuICAgIH0pO1xuICB9XG59XG4iXX0=
