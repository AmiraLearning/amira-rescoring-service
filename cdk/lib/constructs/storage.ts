import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

export interface StorageConstructProps {
  readonly enableVersioning?: boolean;
  readonly lifecycleRules?: s3.LifecycleRule[];
  readonly kmsKeyAlias: string;
}

export class StorageConstruct extends Construct {
  public readonly kmsKey: kms.Key;
  public readonly resultsBucket: s3.Bucket;
  public readonly accessLogsBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: StorageConstructProps) {
    super(scope, id);

    // KMS key for encryption
    this.kmsKey = new kms.Key(this, 'Key', {
      enableKeyRotation: true,
      alias: props.kmsKeyAlias
    });

    // Access logs bucket
    this.accessLogsBucket = new s3.Bucket(this, 'AccessLogsBucket', {
      versioned: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN
    });

    // Results bucket with KMS encryption
    this.resultsBucket = new s3.Bucket(this, 'ResultsBucket', {
      versioned: props.enableVersioning ?? true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: this.kmsKey,
      bucketKeyEnabled: true,
      serverAccessLogsBucket: this.accessLogsBucket,
      serverAccessLogsPrefix: 's3-access-logs/',
      enforceSSL: true,
      lifecycleRules: this.validateAndOptimizeLifecycleRules(props.lifecycleRules),
      removalPolicy: cdk.RemovalPolicy.RETAIN
    });

    // Add security policies
    this.addSecurityPolicies();
  }

  private addSecurityPolicies(): void {
    // Deny insecure transport
    this.resultsBucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'DenyInsecureTransport',
      effect: iam.Effect.DENY,
      principals: [new iam.AnyPrincipal()], // DENY policies apply to all principals
      actions: ['s3:*'],
      resources: [this.resultsBucket.bucketArn, `${this.resultsBucket.bucketArn}/*`],
      conditions: { Bool: { 'aws:SecureTransport': 'false' } }
    }));

    // Deny unencrypted object uploads
    this.resultsBucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'DenyUnEncryptedObjectUploads',
      effect: iam.Effect.DENY,
      principals: [new iam.AnyPrincipal()], // DENY policies apply to all principals
      actions: ['s3:PutObject'],
      resources: [`${this.resultsBucket.bucketArn}/*`],
      conditions: { StringNotEquals: { 's3:x-amz-server-side-encryption': 'aws:kms' } }
    }));
  }

  private validateAndOptimizeLifecycleRules(providedRules?: s3.LifecycleRule[]): s3.LifecycleRule[] {
    if (providedRules && providedRules.length > 0) {
      // Validate provided rules for cost optimization
      const validatedRules = providedRules.map(rule => {
        // Check for inefficient transitions
        if (rule.transitions) {
          const hasIntelligentTiering = rule.transitions.some(t =>
            t.storageClass === s3.StorageClass.INTELLIGENT_TIERING
          );
          const hasImmediateIA = rule.transitions.some(t =>
            t.storageClass === s3.StorageClass.INFREQUENT_ACCESS &&
            t.transitionAfter && t.transitionAfter.toDays() < 30
          );

          if (hasImmediateIA && !hasIntelligentTiering) {
            console.warn(`Lifecycle rule '${rule.id}' transitions to IA before 30 days without Intelligent Tiering. Consider using Intelligent Tiering for cost optimization.`);
          }
        }
        return rule;
      });
      return validatedRules;
    }

    // Provide cost-optimized defaults
    return [
      {
        id: 'CostOptimizedIntelligentTiering',
        enabled: true,
        transitions: [
          {
            storageClass: s3.StorageClass.INTELLIGENT_TIERING,
            transitionAfter: cdk.Duration.days(0)
          }
        ]
      }
    ];
  }
}
