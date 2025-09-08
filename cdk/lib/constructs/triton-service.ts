import * as cdk from 'aws-cdk-lib';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as appscaling from 'aws-cdk-lib/aws-applicationautoscaling';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

export interface TritonServiceConstructProps {
  readonly cluster: ecs.Cluster;
  readonly capacityProviders: ecs.AsgCapacityProvider[];
  readonly logGroup: logs.ILogGroup;
  readonly vpc: ec2.IVpc;
  readonly albSecurityGroup: ec2.ISecurityGroup;
  readonly ecsSecurityGroup: ec2.ISecurityGroup;
  readonly accessLogsBucket: s3.IBucket;
  readonly certArn: string;
  readonly tls: {
    enableHttp2: boolean;
    ciphers?: string;
    secretArn?: string;
    requireSecret: boolean;
  };
  readonly repositories: {
    triton: ecr.IRepository;
    cwAgent: ecr.IRepository;
    dcgmExporter: ecr.IRepository;
  };
  readonly imageTags: {
    triton: string;
    cwAgent: string;
    dcgmExporter: string;
  };
}

export class TritonServiceConstruct extends Construct {
  public readonly service: ecs.Ec2Service;
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly targetGroup: elbv2.ApplicationTargetGroup;
  public readonly taskDefinition: ecs.Ec2TaskDefinition;

  constructor(scope: Construct, id: string, props: TritonServiceConstructProps) {
    super(scope, id);

    // Create task roles
    const { taskRole, taskExecutionRole } = this.createTaskRoles();

    // Create task definition
    this.taskDefinition = this.createTaskDefinition(props, taskRole, taskExecutionRole);

    // Create ALB and target group
    const { alb, targetGroup } = this.createLoadBalancer(props);
    this.alb = alb;
    this.targetGroup = targetGroup;

    // Create ECS service
    this.service = this.createEcsService(props, targetGroup);

    // Setup auto scaling
    this.setupAutoScaling();
  }

  private createTaskRoles(): { taskRole: iam.Role; taskExecutionRole: iam.Role } {
    const taskExecutionRole = new iam.Role(this, 'TaskExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy')
      ]
    });

    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com')
    });

    return { taskRole, taskExecutionRole };
  }

  private createTaskDefinition(
    props: TritonServiceConstructProps,
    taskRole: iam.Role,
    taskExecutionRole: iam.Role
  ): ecs.Ec2TaskDefinition {
    const taskDefinition = new ecs.Ec2TaskDefinition(this, 'TaskDefinition', {
      family: `triton-inference-${cdk.Stack.of(this).stackName}`,
      executionRole: taskExecutionRole,
      taskRole,
      networkMode: ecs.NetworkMode.AWS_VPC
    });

    // Triton container
    const tritonContainer = taskDefinition.addContainer('TritonServer', {
      image: ecs.ContainerImage.fromEcrRepository(props.repositories.triton, props.imageTags.triton),
      memoryReservationMiB: 4096,
      cpu: 1024,
      gpuCount: 1,
      logging: ecs.LogDriver.awsLogs({ logGroup: props.logGroup, streamPrefix: 'triton-server' }),
      portMappings: [{ containerPort: 8000 }, { containerPort: 8001 }, { containerPort: 8002 }],
      healthCheck: {
        command: ['CMD-SHELL', 'curl -sf http://127.0.0.1:8000/v2/health/ready || exit 1'],
        interval: cdk.Duration.seconds(15),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(30)
      }
    });

    // TLS proxy sidecar
    this.addTlsProxyContainer(taskDefinition, props);

    // DCGM exporter
    taskDefinition.addContainer('DcgmExporter', {
      image: ecs.ContainerImage.fromEcrRepository(props.repositories.dcgmExporter, props.imageTags.dcgmExporter),
      memoryReservationMiB: 256,
      cpu: 128,
      logging: ecs.LogDriver.awsLogs({ logGroup: props.logGroup, streamPrefix: 'dcgm-exporter' }),
      portMappings: [{ containerPort: 9400 }]
    });

    // CloudWatch Agent
    this.addCloudWatchAgent(taskDefinition, props, taskRole);

    // Configure conditional secrets for TLS
    this.configureTlsSecrets(taskDefinition, props);

    // Set ulimits
    tritonContainer.addUlimits({ name: ecs.UlimitName.NOFILE, softLimit: 65536, hardLimit: 65536 });

    return taskDefinition;
  }

  private addTlsProxyContainer(
    taskDefinition: ecs.Ec2TaskDefinition,
    props: TritonServiceConstructProps
  ): ecs.ContainerDefinition {
    return taskDefinition.addContainer('TlsProxy', {
      image: ecs.ContainerImage.fromRegistry('nginx:1.25-alpine'),
      memoryReservationMiB: 128,
      cpu: 128,
      logging: ecs.LogDriver.awsLogs({ logGroup: props.logGroup, streamPrefix: 'tls-proxy' }),
      portMappings: [{ containerPort: 8443 }],
      environment: {
        ENABLE_HTTP2: props.tls.enableHttp2.toString(),
        SSL_CIPHERS: props.tls.ciphers || '',
        REQUIRE_TLS_SECRET: props.tls.requireSecret.toString(),
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
  }

  private addCloudWatchAgent(
    taskDefinition: ecs.Ec2TaskDefinition,
    props: TritonServiceConstructProps,
    taskRole: iam.Role
  ): void {
    // Create SSM parameter for CW Agent config
    const cwAgentConfigString = JSON.stringify({
      agent: {
        metrics_collection_interval: 60,
        run_as_user: "cwagent"
      },
      metrics: {
        namespace: "CWAgent",
        metrics_collected: {
          prometheus: {
            prometheus_config_path: "/opt/aws/amazon-cloudwatch-agent/etc/prometheus.yaml",
            emf_processor: {
              metric_declaration_dedup: true,
              metric_unit: {
                "nv_inference_request_duration_us": "Microseconds",
                "nv_inference_queue_duration_us": "Microseconds"
              }
            }
          }
        }
      }
    });

    const cwAgentConfigParam = new ssm.StringParameter(this, 'CwAgentConfig', {
      parameterName: '/amira/cwagent_config',
      stringValue: cwAgentConfigString
    });

    taskDefinition.addContainer('CloudWatchAgent', {
      image: ecs.ContainerImage.fromEcrRepository(props.repositories.cwAgent, props.imageTags.cwAgent),
      memoryReservationMiB: 256,
      cpu: 128,
      logging: ecs.LogDriver.awsLogs({ logGroup: props.logGroup, streamPrefix: 'cloudwatch-agent' }),
      command: [
        '/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent',
        '-a', 'fetch-config',
        '-m', 'ec2',
        '-c', `ssm:${cwAgentConfigParam.parameterName}`,
        '-s'
      ],
      environment: { AWS_REGION: cdk.Stack.of(this).region }
    });

    cwAgentConfigParam.grantRead(taskRole);
  }

  private configureTlsSecrets(
    taskDefinition: ecs.Ec2TaskDefinition,
    props: TritonServiceConstructProps
  ): void {
    if (!props.tls.secretArn) {
      return;
    }

    const cfnTaskDef = taskDefinition.node.defaultChild as ecs.CfnTaskDefinition;
    cfnTaskDef.addPropertyOverride('ContainerDefinitions.1.Secrets', [
      { name: 'TLS_CERT', valueFrom: `${props.tls.secretArn}:cert::` },
      { name: 'TLS_KEY', valueFrom: `${props.tls.secretArn}:key::` }
    ]);

    // Remove deprecated property
    cfnTaskDef.addPropertyDeletionOverride('InferenceAccelerators');
  }

  private createLoadBalancer(
    props: TritonServiceConstructProps
  ): { alb: elbv2.ApplicationLoadBalancer; targetGroup: elbv2.ApplicationTargetGroup } {
    const alb = new elbv2.ApplicationLoadBalancer(this, 'LoadBalancer', {
      vpc: props.vpc,
      internetFacing: false,
      securityGroup: props.albSecurityGroup,
      deletionProtection: true
    });

    // Apply ALB hardening via CloudFormation properties
    const cfnAlb = alb.node.defaultChild as elbv2.CfnLoadBalancer;
    cfnAlb.addPropertyOverride('LoadBalancerAttributes', [
      { Key: 'routing.http.drop_invalid_header_fields.enabled', Value: 'true' },
      { Key: 'routing.http.preserve_host_header.enabled', Value: 'true' },
      { Key: 'routing.http.x_amzn_tls_version_and_cipher_suite.enabled', Value: 'true' },
      { Key: 'routing.http.xff_client_port.enabled', Value: 'true' },
      { Key: 'waf.fail_open.enabled', Value: 'false' }
    ]);

    alb.logAccessLogs(props.accessLogsBucket, 'triton-alb-access-logs/');

    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'TargetGroup', {
      vpc: props.vpc,
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

    const listener = alb.addListener('HttpsListener', {
      port: 443,
      protocol: elbv2.ApplicationProtocol.HTTPS,
      sslPolicy: elbv2.SslPolicy.TLS12_EXT, // Use available TLS policy
      certificates: [elbv2.ListenerCertificate.fromArn(props.certArn)],
      defaultTargetGroups: [targetGroup]
    });

    // Add conditional validation rules for enhanced security
    this.addAlbValidationRules(listener, targetGroup, props);

    return { alb, targetGroup };
  }

  private createEcsService(
    props: TritonServiceConstructProps,
    targetGroup: elbv2.ApplicationTargetGroup
  ): ecs.Ec2Service {
    const service = new ecs.Ec2Service(this, 'Service', {
      cluster: props.cluster,
      taskDefinition: this.taskDefinition,
      serviceName: 'triton-inference-service',
      desiredCount: 1, // Start with 1 task to enable scale-from-zero with RequestCountPerTarget
      securityGroups: [props.ecsSecurityGroup],
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      capacityProviderStrategies: props.capacityProviders.map((cp) => ({
        capacityProvider: cp.capacityProviderName,
        weight: 1
      })),
      placementStrategies: [ecs.PlacementStrategy.spreadAcrossInstances()],
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      enableExecuteCommand: true,
      // Circuit breaker for safe deployments
      circuitBreaker: { rollback: true }
    });

    service.attachToApplicationTargetGroup(targetGroup);
    return service;
  }

  private addAlbValidationRules(listener: elbv2.ApplicationListener, targetGroup: elbv2.ApplicationTargetGroup, props: TritonServiceConstructProps): void {
    // Health check exemption - highest priority, always allow health checks
    new elbv2.ApplicationListenerRule(this, 'HealthCheckRule', {
      listener,
      priority: 10,
      conditions: [
        elbv2.ListenerCondition.pathPatterns(['/v2/health/*', '/health'])
      ],
      action: elbv2.ListenerAction.forward([targetGroup])
    });

    // Optional: Basic authorization check for protected paths (if secrets required)
    if (props.tls.requireSecret) {
      // Only protect specific Triton management endpoints
      new elbv2.ApplicationListenerRule(this, 'ProtectedEndpointsRule', {
        listener,
        priority: 20,
        conditions: [
          elbv2.ListenerCondition.pathPatterns(['/v2/repository/*'])
        ],
        action: elbv2.ListenerAction.fixedResponse(401, {
          contentType: 'application/json',
          messageBody: JSON.stringify({
            error: 'Repository management requires authentication',
            hint: 'Use Triton model management APIs with proper credentials'
          })
        })
      });
    }

    // Default action: Forward all other requests to target group
    // Listener default action handles this, so no catch-all rule needed
    // This prevents the 403 blocking issue while maintaining security

    // Note: For production security, implement proper authentication via:
    // - AWS WAF with rate limiting and IP restrictions
    // - Application-level authentication in Triton inference server
    // - VPC security groups for network-level access control
  }

  private setupAutoScaling(): void {
    const scalableTarget = new appscaling.ScalableTarget(this, 'ScalableTarget', {
      serviceNamespace: appscaling.ServiceNamespace.ECS,
      maxCapacity: 10,
      minCapacity: 1, // Minimum 1 task to ensure RequestCountPerTarget scaling works
      resourceId: `service/${this.service.cluster.clusterName}/${this.service.serviceName}`,
      scalableDimension: 'ecs:service:DesiredCount'
    });

    // Scale based on ALB request count per target
    const albRequestMetric = new cw.Metric({
      namespace: 'AWS/ApplicationELB',
      metricName: 'RequestCountPerTarget',
      dimensionsMap: {
        LoadBalancer: this.alb.loadBalancerFullName,
        TargetGroup: this.targetGroup.targetGroupFullName
      },
      statistic: 'Sum',
      period: cdk.Duration.minutes(1)
    });

    scalableTarget.scaleToTrackMetric('RequestScaling', {
      customMetric: albRequestMetric,
      targetValue: 50,
      scaleInCooldown: cdk.Duration.minutes(2),
      scaleOutCooldown: cdk.Duration.seconds(30)
    });

    // GPU utilization scaling
    const gpuUtilMetric = new cw.Metric({
      namespace: 'CWAgent',
      metricName: 'DCGM_FI_DEV_GPU_UTIL',
      statistic: 'Average',
      period: cdk.Duration.minutes(1)
    });

    scalableTarget.scaleOnMetric('GpuUtilScaling', {
      metric: gpuUtilMetric,
      scalingSteps: [
        { lower: 70, change: +1 },
        { lower: 90, change: +2 },
        { upper: 30, change: -1 }
      ],
      adjustmentType: appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
      cooldown: cdk.Duration.minutes(2),
      minAdjustmentMagnitude: 1
    });

    // Latency-based scaling
    const tritonLatencyMetric = new cw.Metric({
      namespace: 'CWAgent',
      metricName: 'nv_inference_request_duration_us',
      statistic: 'p95',
      period: cdk.Duration.minutes(1)
    });

    scalableTarget.scaleOnMetric('LatencyScaling', {
      metric: tritonLatencyMetric,
      scalingSteps: [
        { lower: 500000, change: +1 },
        { lower: 800000, change: +2 }
      ],
      adjustmentType: appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
      cooldown: cdk.Duration.minutes(2),
      minAdjustmentMagnitude: 1
    });
  }
}
