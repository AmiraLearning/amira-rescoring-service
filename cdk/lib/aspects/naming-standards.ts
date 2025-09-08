import * as cdk from 'aws-cdk-lib';
import { IConstruct } from 'constructs';
import { IAspect } from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ecr from 'aws-cdk-lib/aws-ecr';

/**
 * Unified naming conventions and mandatory tagging aspect
 * Enforces consistent naming patterns and applies mandatory tags across all resources
 */
export class NamingStandardsAspect implements IAspect {
  private readonly stage: string;
  private readonly mandatoryTags: Record<string, string>;
  private readonly namingConventions: Record<string, (baseName: string) => string>;

  constructor(stage: string, mandatoryTags: Record<string, string> = {}) {
    this.stage = stage;
    this.mandatoryTags = {
      Environment: stage,
      Project: 'amira-letter-scoring',
      ManagedBy: 'CDK',
      Owner: 'ML-Platform-Team',
      CostCenter: 'ml-infrastructure',
      ...mandatoryTags
    };

    // Define naming conventions by resource type
    this.namingConventions = {
      's3-bucket': (baseName: string) => `amira-${stage}-${baseName}`,
      'lambda-function': (baseName: string) => `amira-${stage}-${baseName}`,
      'sqs-queue': (baseName: string) => `amira-${stage}-${baseName}`,
      'ecs-service': (baseName: string) => `amira-${stage}-${baseName}`,
      'ecs-cluster': (baseName: string) => `amira-${stage}-${baseName}`,
      'alb': (baseName: string) => `amira-${stage}-${baseName}`,
      'security-group': (baseName: string) => `amira-${stage}-${baseName}-sg`,
      'vpc': (baseName: string) => `amira-${stage}-${baseName}-vpc`,
      'kms-key': (baseName: string) => `alias/amira-${stage}-${baseName}`,
      'log-group': (baseName: string) => `/aws/amira/${stage}/${baseName}`,
      'ecr-repository': (baseName: string) => `amira/${stage}/${baseName}`
    };
  }

  visit(node: IConstruct): void {
    this.applyNamingConventions(node);
    this.applyMandatoryTags(node);
    this.validateResourceNaming(node);
  }

  private applyNamingConventions(node: IConstruct): void {
    const resourceType = this.getResourceType(node);
    if (!resourceType) return;

    const namingFunction = this.namingConventions[resourceType];
    if (!namingFunction) return;

    // Extract base name from construct ID and apply naming convention
    const baseName = this.extractBaseName(node.node.id);
    const standardizedName = namingFunction(baseName);

    // Apply naming convention based on resource type
    this.setResourceName(node, standardizedName, resourceType);
  }

  private applyMandatoryTags(node: IConstruct): void {
    // Apply tags to taggable resources
    if (cdk.Tags.of(node)) {
      Object.entries(this.mandatoryTags).forEach(([key, value]) => {
        cdk.Tags.of(node).add(key, value);
      });

      // Add resource-specific tags
      const resourceType = this.getResourceType(node);
      if (resourceType) {
        cdk.Tags.of(node).add('ResourceType', resourceType);
        cdk.Tags.of(node).add('LastModified', new Date().toISOString());
        cdk.Tags.of(node).add('CreatedBy', 'CDK-NamingStandardsAspect');
      }
    }

    // Apply tags at the CloudFormation level for resources that don't support CDK tags
    this.applyCfnLevelTags(node);
  }

  private validateResourceNaming(node: IConstruct): void {
    const violations: string[] = [];

    // Check for naming convention violations
    if (node instanceof s3.Bucket) {
      const bucketName = node.bucketName;
      if (!bucketName.startsWith(`amira-${this.stage}-`)) {
        violations.push(`S3 bucket '${bucketName}' does not follow naming convention`);
      }
    }

    if (node instanceof lambda.Function) {
      const functionName = node.functionName;
      if (functionName && !functionName.startsWith(`amira-${this.stage}-`)) {
        violations.push(`Lambda function '${functionName}' does not follow naming convention`);
      }
    }

    // Report violations as metadata
    if (violations.length > 0) {
      node.node.addMetadata('naming-violations', violations);
    }
  }

  private getResourceType(node: IConstruct): string | null {
    if (node instanceof s3.Bucket) return 's3-bucket';
    if (node instanceof lambda.Function) return 'lambda-function';
    if (node instanceof sqs.Queue) return 'sqs-queue';
    if (node instanceof ecs.Ec2Service || node instanceof ecs.FargateService) return 'ecs-service';
    if (node instanceof ecs.Cluster) return 'ecs-cluster';
    if (node instanceof elbv2.ApplicationLoadBalancer) return 'alb';
    if (node instanceof ec2.SecurityGroup) return 'security-group';
    if (node instanceof ec2.Vpc) return 'vpc';
    if (node instanceof kms.Key) return 'kms-key';
    if (node instanceof logs.LogGroup) return 'log-group';
    if (node instanceof ecr.Repository) return 'ecr-repository';

    return null;
  }

  private extractBaseName(constructId: string): string {
    // Remove common prefixes/suffixes and convert to kebab-case
    let baseName = constructId
      .replace(/^(Amira|AmiraLetter)/i, '')
      .replace(/(Stack|Construct|Service|Function|Bucket|Queue)$/i, '')
      .replace(/([A-Z])/g, '-$1')
      .toLowerCase()
      .replace(/^-+|-+$/g, '')
      .replace(/-+/g, '-');

    // Handle empty or invalid base names
    if (!baseName || baseName.length === 0) {
      baseName = 'resource';
    }

    return baseName;
  }

  private setResourceName(node: IConstruct, standardizedName: string, resourceType: string): void {
    const cfnResource = node.node.defaultChild;
    if (!cfnResource) return;

    try {
      switch (resourceType) {
        case 's3-bucket':
          if (cfnResource instanceof s3.CfnBucket) {
            cfnResource.bucketName = standardizedName;
          }
          break;

        case 'lambda-function':
          if (cfnResource instanceof lambda.CfnFunction) {
            cfnResource.functionName = standardizedName;
          }
          break;

        case 'sqs-queue':
          if (cfnResource instanceof sqs.CfnQueue) {
            cfnResource.queueName = standardizedName;
          }
          break;

        case 'ecs-service':
          if (cfnResource instanceof ecs.CfnService) {
            cfnResource.serviceName = standardizedName;
          }
          break;

        case 'ecs-cluster':
          if (cfnResource instanceof ecs.CfnCluster) {
            cfnResource.clusterName = standardizedName;
          }
          break;

        case 'alb':
          if (cfnResource instanceof elbv2.CfnLoadBalancer) {
            cfnResource.name = standardizedName;
          }
          break;

        case 'security-group':
          if (cfnResource instanceof ec2.CfnSecurityGroup) {
            cfnResource.groupName = standardizedName;
          }
          break;

        case 'log-group':
          if (cfnResource instanceof logs.CfnLogGroup) {
            cfnResource.logGroupName = standardizedName;
          }
          break;

        case 'ecr-repository':
          if (cfnResource instanceof ecr.CfnRepository) {
            cfnResource.repositoryName = standardizedName;
          }
          break;

        case 'kms-key':
          // KMS key aliases are handled separately via KMS API
          node.node.addMetadata('standard-alias', standardizedName);
          break;
      }
    } catch (error) {
      // Some resources might be immutable, log as metadata instead of failing
      node.node.addMetadata('naming-standard-attempted', {
        targetName: standardizedName,
        error: String(error)
      });
    }
  }

  private applyCfnLevelTags(node: IConstruct): void {
    const cfnResource = node.node.defaultChild;
    if (!cfnResource || !cdk.CfnResource.isCfnResource(cfnResource)) {
      return;
    }

    // Apply tags at CloudFormation level
    const tagsArray = Object.entries(this.mandatoryTags).map(([key, value]) => ({
      key,
      value
    }));

    try {
      cfnResource.addPropertyOverride('Tags', tagsArray);
    } catch (error) {
      // Some resources don't support tags, add as metadata for tracking
      node.node.addMetadata('tag-application-failed', {
        resource: cfnResource.cfnResourceType,
        error: String(error)
      });
    }
  }

  /**
   * Add custom naming convention for specific resource types
   */
  public addNamingConvention(resourceType: string, namingFunction: (baseName: string) => string): void {
    this.namingConventions[resourceType] = namingFunction;
  }

  /**
   * Add additional mandatory tags
   */
  public addMandatoryTags(tags: Record<string, string>): void {
    Object.assign(this.mandatoryTags, tags);
  }
}

/**
 * Helper function to apply naming standards to an app or stack
 */
export function applyNamingStandards(
  scope: IConstruct,
  stage: string,
  additionalTags: Record<string, string> = {}
): void {
  cdk.Aspects.of(scope).add(new NamingStandardsAspect(stage, additionalTags));
}
