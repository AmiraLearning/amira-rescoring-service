import * as cdk from 'aws-cdk-lib';
import { IConstruct } from 'constructs';
import { IAspect } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as rds from 'aws-cdk-lib/aws-rds';

/**
 * Aspect to standardize removal policies across all resources based on environment and resource type
 */
export class RemovalPolicyStandardsAspect implements IAspect {
  private readonly stage: string;
  private readonly isProd: boolean;

  constructor(stage: string) {
    this.stage = stage.toLowerCase();
    this.isProd = this.stage === 'prod' || this.stage === 'production';
  }

  visit(node: IConstruct): void {
    this.applyRemovalPolicyStandards(node);
  }

  private applyRemovalPolicyStandards(node: IConstruct): void {
    // Stateful resources (data stores) - RETAIN in production, DESTROY in non-prod
    if (this.isStatefulResource(node)) {
      const policy = this.isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY;
      this.applyRemovalPolicy(node, policy, 'stateful-resource');
    }

    // Ephemeral resources (compute, networking) - always DESTROY
    else if (this.isEphemeralResource(node)) {
      this.applyRemovalPolicy(node, cdk.RemovalPolicy.DESTROY, 'ephemeral-resource');
    }

    // Security resources (KMS keys, secrets) - RETAIN in production, DESTROY with delay in non-prod
    else if (this.isSecurityResource(node)) {
      if (this.isProd) {
        this.applyRemovalPolicy(node, cdk.RemovalPolicy.RETAIN, 'security-resource');
      } else {
        // Non-prod: allow destruction but with delay for safety
        if (node instanceof kms.Key) {
          const cfnKey = node.node.defaultChild as kms.CfnKey;
          cfnKey.pendingWindowInDays = 7; // Minimum 7 days for non-prod
        }
        this.applyRemovalPolicy(node, cdk.RemovalPolicy.DESTROY, 'security-resource-nonprod');
      }
    }
  }

  private isStatefulResource(node: IConstruct): boolean {
    return (
      node instanceof s3.Bucket ||
      node instanceof dynamodb.Table ||
      node instanceof rds.DatabaseInstance ||
      node instanceof rds.DatabaseCluster
    );
  }

  private isEphemeralResource(node: IConstruct): boolean {
    return (
      node instanceof logs.LogGroup ||
      // Add other ephemeral resources as needed
      node.node.id.includes('LogGroup') ||
      node.node.id.includes('VpcFlowLogs')
    );
  }

  private isSecurityResource(node: IConstruct): boolean {
    return (
      node instanceof kms.Key ||
      node.node.id.includes('Key') ||
      node.node.id.includes('Secret')
    );
  }

  private applyRemovalPolicy(node: IConstruct, policy: cdk.RemovalPolicy, category: string): void {
    try {
      // Apply to CDK resources that support removal policy
      if ('applyRemovalPolicy' in node && typeof (node as any).applyRemovalPolicy === 'function') {
        (node as any).applyRemovalPolicy(policy);

        // Add metadata for tracking
        node.node.addMetadata('removal-policy-applied', {
          policy: policy.toString(),
          category,
          stage: this.stage,
          appliedBy: 'RemovalPolicyStandardsAspect'
        });
      }

      // For CloudFormation resources, set deletion policy
      const cfnResource = node.node.defaultChild;
      if (cfnResource && cdk.CfnResource.isCfnResource(cfnResource)) {
        if (policy === cdk.RemovalPolicy.RETAIN) {
          cfnResource.addPropertyOverride('DeletionPolicy', 'Retain');
          if (this.isStatefulResource(node)) {
            cfnResource.addPropertyOverride('UpdateReplacePolicy', 'Retain');
          }
        } else if (policy === cdk.RemovalPolicy.SNAPSHOT && this.supportsSnapshots(node)) {
          cfnResource.addPropertyOverride('DeletionPolicy', 'Snapshot');
        }
      }

    } catch (error) {
      // Some resources might not support removal policy changes
      node.node.addMetadata('removal-policy-error', {
        error: String(error),
        attemptedPolicy: policy.toString(),
        category
      });
    }
  }

  private supportsSnapshots(node: IConstruct): boolean {
    return (
      node instanceof rds.DatabaseInstance ||
      node instanceof rds.DatabaseCluster ||
      node instanceof dynamodb.Table
    );
  }
}

/**
 * Helper function to apply removal policy standards to a stack or app
 */
export function applyRemovalPolicyStandards(scope: IConstruct, stage: string): void {
  cdk.Aspects.of(scope).add(new RemovalPolicyStandardsAspect(stage));
}
