import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface NetworkingConstructProps {
  readonly natGatewayCount: number;
  readonly enableInterfaceEndpoints: boolean;
  readonly albClientSecurityGroupId?: string;
  readonly encryptionKey?: kms.IKey;
  readonly useNatInstances?: boolean; // Cost optimization: NAT instances instead of gateways
  readonly stageName?: string; // Optional stage name for dev environment exceptions
}

export class NetworkingConstruct extends Construct {
  public readonly vpc: ec2.Vpc;
  public readonly privateSubnets: ec2.ISubnet[];
  public readonly albSecurityGroup: ec2.SecurityGroup;
  public readonly ecsSecurityGroup: ec2.SecurityGroup;
  public readonly lambdaSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: NetworkingConstructProps) {
    super(scope, id);

    // VPC with cost-optimized NAT configuration
    this.vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: props.useNatInstances ? 0 : props.natGatewayCount,
      natGatewayProvider: props.useNatInstances
        ? ec2.NatProvider.instanceV2({
            instanceType: ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.NANO),
            defaultAllowedTraffic: ec2.NatTrafficDirection.OUTBOUND_ONLY
          })
        : ec2.NatProvider.gateway(),
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

    this.privateSubnets = this.vpc.privateSubnets;

    // VPC Endpoints
    this.setupVpcEndpoints(props.enableInterfaceEndpoints);

    // Security Groups
    const { albSecurityGroup, ecsSecurityGroup, lambdaSecurityGroup } = this.setupSecurityGroups(props);
    this.albSecurityGroup = albSecurityGroup;
    this.ecsSecurityGroup = ecsSecurityGroup;
    this.lambdaSecurityGroup = lambdaSecurityGroup;

    // VPC Flow Logs
    if (props.encryptionKey) {
      this.setupVpcFlowLogs(props.encryptionKey);
    }
  }

  private setupVpcEndpoints(enableInterfaceEndpoints: boolean): void {
    // S3 Gateway Endpoint (always create)
    this.vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
      subnets: [{ subnets: this.vpc.privateSubnets }]
    });

    if (!enableInterfaceEndpoints) {
      return;
    }

    // Interface endpoints for cost optimization when NAT gateways are disabled
    const endpoints = [
      { id: 'EcrApi', service: ec2.InterfaceVpcEndpointAwsService.ECR },
      { id: 'EcrDocker', service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER },
      { id: 'CloudWatchLogs', service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS },
      { id: 'Sqs', service: ec2.InterfaceVpcEndpointAwsService.SQS },
      { id: 'Ssm', service: ec2.InterfaceVpcEndpointAwsService.SSM },
      { id: 'SsmMessages', service: ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES },
      { id: 'Ec2Messages', service: ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES },
      { id: 'Sts', service: ec2.InterfaceVpcEndpointAwsService.STS },
      { id: 'SecretsManager', service: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER },
      { id: 'Kms', service: ec2.InterfaceVpcEndpointAwsService.KMS },
    ];

    endpoints.forEach(({ id, service }) => {
      const vpcEndpoint = this.vpc.addInterfaceEndpoint(`${id}Endpoint`, {
        service,
        subnets: { subnets: this.vpc.privateSubnets }
      });

      // Apply minimal privilege policy to VPC endpoints
      this.applyVpcEndpointPolicy(vpcEndpoint, service);
    });
  }

  private setupSecurityGroups(props: NetworkingConstructProps): {
    albSecurityGroup: ec2.SecurityGroup;
    ecsSecurityGroup: ec2.SecurityGroup;
    lambdaSecurityGroup: ec2.SecurityGroup;
  } {
    const { albClientSecurityGroupId } = props;

    // ALB Security Group
    const albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc: this.vpc,
      description: 'Security group for internal ALB fronting services',
      allowAllOutbound: true
    });

    // ECS Security Group
    const ecsSecurityGroup = new ec2.SecurityGroup(this, 'EcsSecurityGroup', {
      vpc: this.vpc,
      description: 'Security group for ECS tasks',
      allowAllOutbound: true
    });

    // Lambda Security Group (least-privilege egress)
    const lambdaSecurityGroup = new ec2.SecurityGroup(this, 'LambdaSecurityGroup', {
      vpc: this.vpc,
      description: 'Security group for Lambda functions with least-privilege egress',
      allowAllOutbound: false // Explicit egress rules only
    });

    // Configure ALB ingress rules
    if (albClientSecurityGroupId) {
      // Allow specific security group
      albSecurityGroup.addIngressRule(
        ec2.SecurityGroup.fromSecurityGroupId(this, 'ClientSg', albClientSecurityGroupId),
        ec2.Port.tcp(443),
        'Allow client security group to access ALB'
      );
    } else if (props.stageName === 'dev' || props.stageName === 'development') {
      // DEV ONLY: Allow VPC CIDR access for development environments
      albSecurityGroup.addIngressRule(
        ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
        ec2.Port.tcp(443),
        'Allow VPC CIDR access in dev environment'
      );
    } else {
      // SECURITY: Fail deployment for production without specific security group
      throw new Error(
        'ALB client security group ID is required for production security. ' +
        'Allowing entire VPC CIDR access is not permitted. ' +
        'Please specify albClientSecurityGroupId in NetworkingConstructProps.'
      );
    }

    // Allow ALB to reach ECS tasks
    ecsSecurityGroup.addIngressRule(
      albSecurityGroup,
      ec2.Port.tcp(8443),
      'Allow ALB to reach services over HTTPS'
    );

    // Lambda least-privilege egress rules
    this.configureLambdaEgressRules(lambdaSecurityGroup, albSecurityGroup);

    return { albSecurityGroup, ecsSecurityGroup, lambdaSecurityGroup };
  }

  private configureLambdaEgressRules(lambdaSecurityGroup: ec2.SecurityGroup, albSecurityGroup: ec2.SecurityGroup): void {
    // Allow Lambda to reach ALB on port 443 (for Triton calls)
    lambdaSecurityGroup.addEgressRule(
      albSecurityGroup,
      ec2.Port.tcp(443),
      'Allow Lambda to call internal ALB for Triton inference'
    );

    // Allow Lambda to reach VPC endpoints on port 443 (AWS API calls)
    lambdaSecurityGroup.addEgressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(443),
      'Allow Lambda to reach VPC endpoints for AWS API calls'
    );

    // Allow DNS resolution (UDP and TCP fallback)
    lambdaSecurityGroup.addEgressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.udp(53),
      'Allow DNS resolution'
    );
    lambdaSecurityGroup.addEgressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(53),
      'Allow DNS TCP fallback'
    );

    // Note: No internet access - all AWS API calls go through VPC endpoints
    // This prevents accidental data exfiltration while maintaining functionality
  }

  private applyVpcEndpointPolicy(vpcEndpoint: ec2.InterfaceVpcEndpoint, service: ec2.InterfaceVpcEndpointAwsService): void {
    const account = cdk.Stack.of(this).account;
    const region = cdk.Stack.of(this).region;

    // Base policy document allowing only necessary actions
    const policyDocument = new iam.PolicyDocument({
      statements: []
    });

    // ECR specific policies
    if (service === ec2.InterfaceVpcEndpointAwsService.ECR) {
      policyDocument.addStatements(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.AccountRootPrincipal()],
          actions: [
            'ecr:GetAuthorizationToken',
            'ecr:BatchCheckLayerAvailability',
            'ecr:GetDownloadUrlForLayer',
            'ecr:BatchGetImage'
          ],
          resources: [`arn:aws:ecr:${region}:${account}:repository/*`],
          conditions: {
            StringEquals: {
              'aws:sourceVpce': cdk.Token.asString(vpcEndpoint.vpcEndpointId)
            }
          }
        })
      );
    }

    // ECR Docker specific policies
    else if (service === ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER) {
      policyDocument.addStatements(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.AccountRootPrincipal()],
          actions: [
            'ecr:BatchGetImage',
            'ecr:GetDownloadUrlForLayer'
          ],
          resources: [`arn:aws:ecr:${region}:${account}:repository/*`],
          conditions: {
            StringEquals: {
              'aws:sourceVpce': cdk.Token.asString(vpcEndpoint.vpcEndpointId)
            }
          }
        })
      );
    }

    // SQS specific policies
    else if (service === ec2.InterfaceVpcEndpointAwsService.SQS) {
      policyDocument.addStatements(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.AccountRootPrincipal()],
          actions: [
            'sqs:SendMessage',
            'sqs:ReceiveMessage',
            'sqs:DeleteMessage',
            'sqs:GetQueueAttributes'
          ],
          resources: [`arn:aws:sqs:${region}:${account}:*`],
          conditions: {
            StringEquals: {
              'aws:sourceVpce': cdk.Token.asString(vpcEndpoint.vpcEndpointId)
            }
          }
        })
      );
    }

    // CloudWatch Logs specific policies
    else if (service === ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS) {
      policyDocument.addStatements(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.AccountRootPrincipal()],
          actions: [
            'logs:CreateLogGroup',
            'logs:CreateLogStream',
            'logs:PutLogEvents',
            'logs:DescribeLogStreams',
            'logs:DescribeLogGroups'
          ],
          resources: [`arn:aws:logs:${region}:${account}:*`],
          conditions: {
            StringEquals: {
              'aws:sourceVpce': cdk.Token.asString(vpcEndpoint.vpcEndpointId)
            }
          }
        })
      );
    }

    // KMS specific policies
    else if (service === ec2.InterfaceVpcEndpointAwsService.KMS) {
      policyDocument.addStatements(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.AccountRootPrincipal()],
          actions: [
            'kms:Encrypt',
            'kms:Decrypt',
            'kms:ReEncrypt*',
            'kms:GenerateDataKey*',
            'kms:DescribeKey'
          ],
          resources: [`arn:aws:kms:${region}:${account}:key/*`],
          conditions: {
            StringEquals: {
              'aws:sourceVpce': cdk.Token.asString(vpcEndpoint.vpcEndpointId)
            }
          }
        })
      );
    }

    // Secrets Manager specific policies
    else if (service === ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER) {
      policyDocument.addStatements(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.AccountRootPrincipal()],
          actions: [
            'secretsmanager:GetSecretValue',
            'secretsmanager:DescribeSecret'
          ],
          resources: [`arn:aws:secretsmanager:${region}:${account}:secret:*`],
          conditions: {
            StringEquals: {
              'aws:sourceVpce': cdk.Token.asString(vpcEndpoint.vpcEndpointId)
            }
          }
        })
      );
    }

    // SSM specific policies (for Systems Manager and SSM Messages)
    else if (service === ec2.InterfaceVpcEndpointAwsService.SSM ||
             service === ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES ||
             service === ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES) {
      policyDocument.addStatements(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.AccountRootPrincipal()],
          actions: [
            'ssm:GetParameter',
            'ssm:GetParameters',
            'ssm:GetParametersByPath'
          ],
          resources: [
            `arn:aws:ssm:${region}:${account}:parameter/amira/*`,
            `arn:aws:ssm:${region}:${account}:parameter/aws/service/*`
          ],
          conditions: {
            StringEquals: {
              'aws:sourceVpce': cdk.Token.asString(vpcEndpoint.vpcEndpointId)
            }
          }
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.AccountRootPrincipal()],
          actions: [
            'ssmmessages:CreateControlChannel',
            'ssmmessages:CreateDataChannel',
            'ssmmessages:OpenControlChannel',
            'ssmmessages:OpenDataChannel',
            'ec2messages:GetMessages',
            'ec2messages:AcknowledgeMessage',
            'ec2messages:DeleteMessage',
            'ec2messages:FailMessage',
            'ec2messages:GetEndpoint',
            'ec2messages:SendReply'
          ],
          resources: ['*'], // These services require wildcard for session management
          conditions: {
            StringEquals: {
              'aws:sourceVpce': cdk.Token.asString(vpcEndpoint.vpcEndpointId)
            }
          }
        })
      );
    }

    // STS specific policies
    else if (service === ec2.InterfaceVpcEndpointAwsService.STS) {
      policyDocument.addStatements(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.AccountRootPrincipal()],
          actions: [
            'sts:GetCallerIdentity'
          ],
          resources: ['*'], // GetCallerIdentity requires wildcard
          conditions: {
            StringEquals: {
              'aws:sourceVpce': cdk.Token.asString(vpcEndpoint.vpcEndpointId)
            }
          }
        }),
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          principals: [new iam.AccountRootPrincipal()],
          actions: [
            'sts:AssumeRole'
          ],
          resources: [
            `arn:aws:iam::${account}:role/AmiraLetterScoring*`,
            `arn:aws:iam::${account}:role/cdk-*`
          ],
          conditions: {
            StringEquals: {
              'aws:sourceVpce': cdk.Token.asString(vpcEndpoint.vpcEndpointId)
            }
          }
        })
      );
    }

    // Apply the policy to the VPC endpoint if it has statements
    if (policyDocument.statementCount > 0) {
      const cfnVpcEndpoint = vpcEndpoint.node.defaultChild as ec2.CfnVPCEndpoint;
      cfnVpcEndpoint.addPropertyOverride('PolicyDocument', policyDocument.toJSON());
    }
  }

  private setupVpcFlowLogs(encryptionKey: kms.IKey): void {
    const vpcFlowLogGroup = new logs.LogGroup(this, 'VpcFlowLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryptionKey
    });

    this.vpc.addFlowLog('FlowLogsAll', {
      destination: ec2.FlowLogDestination.toCloudWatchLogs(vpcFlowLogGroup),
      trafficType: ec2.FlowLogTrafficType.ALL
    });
  }
}
