import * as ec2 from 'aws-cdk-lib/aws-ec2';

export interface StageConfig {
  readonly stageName: string;
  readonly features: FeatureFlags;
  readonly networking: NetworkingConfig;
  readonly compute: ComputeConfig;
  readonly queueing: QueueingConfig;
  readonly observability: ObservabilityConfig;
  readonly security: SecurityConfig;
  readonly storage?: StorageConfig;
  readonly athena?: AthenaConfig;
  readonly tls?: TlsConfig;
}

export interface FeatureFlags {
  readonly enableTriton: boolean;
  readonly enableWarming: boolean;
  readonly enableAthenaCleanup: boolean;
  readonly enableInterfaceEndpoints: boolean;
}

export interface NetworkingConfig {
  readonly natGatewayCount: number;
  readonly albClientSecurityGroupId?: string;
  readonly useNatInstances?: boolean;
}

export interface ComputeConfig {
  readonly instanceTypes: ec2.InstanceType[];
  readonly maxCapacity: number;
  readonly spotMaxPrice?: string; // Maximum spot price per hour, omit for on-demand pricing
  readonly lambda: {
    memorySize: number;
    timeout: number;
    maxConcurrency: number;
    maxEventSourceConcurrency: number;
  };
}

export interface ObservabilityConfig {
  readonly alarmEmail?: string;
  readonly warmRateMinutes?: number;
}

export interface SecurityConfig {
  readonly tritonCertArn?: string;
  readonly enableVersioning: boolean;
}

export interface AthenaConfig {
  readonly database: string;
  readonly output: string;
  readonly query: string;
  readonly cleanup?: {
    bucket: string;
    prefix: string;
    ageDays: number;
  };
}

export interface AudioBucketConfig {
  readonly name: string;
  readonly prefix?: string;
}

export interface QueueingConfig {
  readonly maxBatchingWindowSeconds: number;
}

export interface StorageConfig {
  readonly audioBucket?: AudioBucketConfig;
}

export interface TlsConfig {
  readonly enableHttp2: boolean;
  readonly ciphers?: string;
  readonly secretArn?: string;
  readonly requireSecret: boolean;
}

export class StageConfigs {
  static readonly DEV: StageConfig = {
    stageName: 'dev',
    features: {
      enableTriton: true,
      enableWarming: false,
      enableAthenaCleanup: false,
      enableInterfaceEndpoints: true
    },
    networking: {
      natGatewayCount: 1, // Cost optimization for dev
      useNatInstances: true // Further cost reduction with NAT instances
    },
    compute: {
      instanceTypes: [
        ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE)
      ],
      maxCapacity: 3,
      spotMaxPrice: '0.30', // Lower spot price for dev to prevent cost overruns
      lambda: {
        memorySize: 10240,
        timeout: 15, // minutes
        maxConcurrency: 5,
        maxEventSourceConcurrency: 5
      }
    },
    queueing: {
      maxBatchingWindowSeconds: 0 // Cost optimization: immediate processing in dev
    },
    observability: {
      warmRateMinutes: 30
    },
    security: {
      enableVersioning: false // Cost optimization for dev
    },
    tls: {
      enableHttp2: true,
      requireSecret: false // Allow self-signed certs in dev
    }
  };

  static readonly STAGING: StageConfig = {
    stageName: 'staging',
    features: {
      enableTriton: true,
      enableWarming: true,
      enableAthenaCleanup: true,
      enableInterfaceEndpoints: true
    },
    networking: {
      natGatewayCount: 2
    },
    compute: {
      instanceTypes: [
        ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE),
        ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE2)
      ],
      maxCapacity: 5,
      spotMaxPrice: '0.50', // Moderate spot price for staging
      lambda: {
        memorySize: 10240,
        timeout: 15,
        maxConcurrency: 8,
        maxEventSourceConcurrency: 8
      }
    },
    queueing: {
      maxBatchingWindowSeconds: 20 // Long polling for cost optimization in staging
    },
    observability: {
      // TODO: Replace with actual staging alert email address
      // alarmEmail: 'staging-alerts@example.com',
      warmRateMinutes: 20
    },
    security: {
      enableVersioning: true
    },
    tls: {
      enableHttp2: true,
      requireSecret: true
    }
  };

  static readonly PROD: StageConfig = {
    stageName: 'prod',
    features: {
      enableTriton: true,
      enableWarming: true,
      enableAthenaCleanup: true,
      enableInterfaceEndpoints: true
    },
    networking: {
      natGatewayCount: 2
    },
    compute: {
      instanceTypes: [
        ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE4),
        ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE),
        ec2.InstanceType.of(ec2.InstanceClass.G5, ec2.InstanceSize.XLARGE2)
      ],
      maxCapacity: 10,
      // spotMaxPrice omitted for production - use on-demand for reliability
      lambda: {
        memorySize: 10240,
        timeout: 15,
        maxConcurrency: 10,
        maxEventSourceConcurrency: 10
      }
    },
    queueing: {
      maxBatchingWindowSeconds: 20 // Long polling for maximum cost efficiency in production
    },
    observability: {
      // TODO: Replace with actual production alert email address - CRITICAL FOR PROD!
      // alarmEmail: 'prod-alerts@example.com',
      warmRateMinutes: 15
    },
    security: {
      enableVersioning: true,
      // TODO: Add actual production certificate ARN
      // tritonCertArn: 'arn:aws:acm:us-east-1:123456789012:certificate/12345678-1234-1234-1234-123456789012'
    },
    tls: {
      enableHttp2: true,
      requireSecret: true,
      // TODO: Add actual production TLS secret ARN if required
      // secretArn: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:triton-tls-AbCdEf'
    },
    athena: {
      database: 'amira_prod',
      output: 's3://amira-athena-results-prod/',
      query: 'SELECT activity_id FROM activities WHERE process_flag = 1',
      cleanup: {
        bucket: 'amira-athena-results-prod',
        prefix: 'staging/',
        ageDays: 7
      }
    }
  };

  static getConfig(stage: string): StageConfig {
    switch (stage.toLowerCase()) {
      case 'dev':
      case 'development':
        return this.DEV;
      case 'staging':
      case 'stage':
        return this.STAGING;
      case 'prod':
      case 'production':
        return this.PROD;
      default:
        throw new Error(`Unknown stage: ${stage}. Supported stages: dev, staging, prod`);
    }
  }
}
