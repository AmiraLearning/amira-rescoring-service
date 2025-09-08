import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { IConstruct } from 'constructs';
import { IAspect } from 'aws-cdk-lib';

export interface SecurityStandardsAspectProps {
  readonly enforceS3Ssl?: boolean;
  readonly enforceS3Encryption?: boolean;
  readonly enforceSqsSsl?: boolean;
  readonly requireS3BlockPublicAccess?: boolean;
  readonly enforceLogRetention?: boolean;
  readonly enforceTagging?: boolean;
  readonly organizationTags?: Record<string, string>;
  readonly defaultLogRetention?: logs.RetentionDays;
}

export class SecurityStandardsAspect implements IAspect {
  private readonly props: SecurityStandardsAspectProps;

  constructor(props: SecurityStandardsAspectProps = {}) {
    this.props = {
      enforceS3Ssl: props.enforceS3Ssl ?? true,
      enforceS3Encryption: props.enforceS3Encryption ?? true,
      enforceSqsSsl: props.enforceSqsSsl ?? true,
      requireS3BlockPublicAccess: props.requireS3BlockPublicAccess ?? true,
      enforceLogRetention: props.enforceLogRetention ?? true,
      enforceTagging: props.enforceTagging ?? true,
      defaultLogRetention: props.defaultLogRetention ?? logs.RetentionDays.ONE_MONTH,
      organizationTags: props.organizationTags ?? {
        'cost-center': 'platform',
        'owner': 'amira-team',
        'data-classification': 'internal'
      },
      ...props
    };
  }

  visit(node: IConstruct): void {
    // Apply resource-specific standards (only idempotent property overrides)
    if (node instanceof s3.Bucket) {
      this.applyS3Standards(node);
    }

    if (node instanceof logs.LogGroup) {
      this.applyLogGroupStandards(node);
    }

    if (node instanceof ecr.Repository) {
      this.applyEcrStandards(node);
    }

    if (node instanceof lambda.Function) {
      this.applyLambdaStandards(node);
    }
  }

  private applyS3Standards(bucket: s3.Bucket): void {
    // Ensure public access is blocked (idempotent property override)
    if (this.props.requireS3BlockPublicAccess) {
      const cfnBucket = bucket.node.defaultChild as s3.CfnBucket;
      if (!cfnBucket.publicAccessBlockConfiguration) {
        cfnBucket.addPropertyOverride('PublicAccessBlockConfiguration', {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true
        });
      }
    }
  }


  private applyLogGroupStandards(logGroup: logs.LogGroup): void {
    if (this.props.enforceLogRetention) {
      // Ensure consistent log retention and cleanup (only if not explicitly set)
      const cfnLogGroup = logGroup.node.defaultChild as logs.CfnLogGroup;

      // Check if retention is already explicitly set to avoid overriding
      const currentRetention = cfnLogGroup.retentionInDays;
      if (currentRetention === undefined || currentRetention === null) {
        cfnLogGroup.addPropertyOverride('RetentionInDays', this.props.defaultLogRetention);
      }

      // Only apply removal policy if not already set
      try {
        const currentPolicy = (logGroup as any).node.metadata.find((m: any) => m.type === 'aws:cdk:removal-policy');
        if (!currentPolicy) {
          logGroup.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
        }
      } catch (error) {
        // Fallback: try to apply, but don't fail if it's already set
        try {
          logGroup.applyRemovalPolicy(cdk.RemovalPolicy.DESTROY);
        } catch {
          // Policy already set, ignore
        }
      }
    }
  }

  private applyEcrStandards(repository: ecr.Repository): void {
    // Enforce image immutability to prevent tag mutations (only if not explicitly set)
    const cfnRepository = repository.node.defaultChild as ecr.CfnRepository;

    // Check if immutability is already set to avoid overriding explicit choices
    const currentMutability = cfnRepository.imageTagMutability;
    if (currentMutability === undefined || currentMutability === null) {
      cfnRepository.addPropertyOverride('ImageTagMutability', 'IMMUTABLE');
    }

    // Check if scanning is already configured
    const currentScanConfig = (cfnRepository as any).imageScanningConfiguration;
    if (!currentScanConfig || currentScanConfig.scanOnPush === undefined) {
      cfnRepository.addPropertyOverride('ImageScanningConfiguration.ScanOnPush', true);
    }

    // Add lifecycle policy if not already present
    if (!cfnRepository.lifecyclePolicy) {
      cfnRepository.addPropertyOverride('LifecyclePolicy', {
        LifecyclePolicyText: JSON.stringify({
          rules: [{
            rulePriority: 1,
            description: 'Keep only 10 most recent images',
            selection: {
              tagStatus: 'any',
              countType: 'imageCountMoreThan',
              countNumber: 10
            },
            action: { type: 'expire' }
          }]
        })
      });
    }
  }

  private applyLambdaStandards(lambdaFunction: lambda.Function): void {
    // Ensure X-Ray tracing is enabled for observability (idempotent property override)
    const cfnFunction = lambdaFunction.node.defaultChild as lambda.CfnFunction;
    if (!cfnFunction.tracingConfig) {
      cfnFunction.addPropertyOverride('TracingConfig.Mode', 'Active');
    }
  }

}
