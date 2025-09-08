import * as cdk from 'aws-cdk-lib';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

export interface CrossStackLinkingConstructProps {
  readonly vpc?: ec2.IVpc;
  readonly albSecurityGroup?: ec2.ISecurityGroup;
  readonly tritonAlb?: elbv2.IApplicationLoadBalancer;
  readonly enableTritonUrlValidation?: boolean;
}

export class CrossStackLinkingConstruct extends Construct {
  public readonly tritonUrlParameter?: ssm.StringParameter;
  public readonly vpcIdParameter?: ssm.StringParameter;
  public readonly privateSubnetIdsParameter?: ssm.StringParameter;
  public readonly albSecurityGroupParameter?: ssm.StringParameter;

  constructor(scope: Construct, id: string, props: CrossStackLinkingConstructProps = {}) {
    super(scope, id);

    // Create SSM parameters for cross-stack discovery
    if (props.tritonAlb) {
      this.tritonUrlParameter = new ssm.StringParameter(this, 'TritonAlbUrlParam', {
        parameterName: '/amira/triton_alb_url',
        stringValue: `https://${props.tritonAlb.loadBalancerDnsName}`,
        description: 'Triton GPU inference cluster ALB URL'
      });
    }

    if (props.vpc) {
      this.vpcIdParameter = new ssm.StringParameter(this, 'VpcIdParam', {
        parameterName: '/amira/vpc_id',
        stringValue: props.vpc.vpcId,
        description: 'VPC ID for cross-stack Lambda attachment'
      });

      this.privateSubnetIdsParameter = new ssm.StringParameter(this, 'VpcPrivateSubnetIdsParam', {
        parameterName: '/amira/vpc_private_subnet_ids',
        stringValue: props.vpc.privateSubnets.map(s => s.subnetId).join(','),
        description: 'Private subnet IDs for Lambda VPC configuration'
      });
    }

    if (props.albSecurityGroup) {
      this.albSecurityGroupParameter = new ssm.StringParameter(this, 'AlbSecurityGroupIdParam', {
        parameterName: '/amira/alb_sg_id',
        stringValue: props.albSecurityGroup.securityGroupId,
        description: 'ALB Security Group ID for Lambda access'
      });
    }

    // Optional validation that Triton URL parameter exists (for fail-fast deployment)
    if (props.enableTritonUrlValidation && !props.tritonAlb) {
      this.createTritonUrlValidation();
    }
  }

  private createTritonUrlValidation(): void {
    const ssmParamName = '/amira/triton_alb_url';
    const ssmParamArn = cdk.Arn.format({
      service: 'ssm',
      resource: 'parameter',
      resourceName: 'amira/triton_alb_url'
    }, cdk.Stack.of(this));

    new cr.AwsCustomResource(this, 'TritonUrlValidation', {
      onCreate: {
        service: 'SSM',
        action: 'getParameter',
        parameters: { Name: ssmParamName },
        physicalResourceId: cr.PhysicalResourceId.of('TritonUrlValidation')
      },
      onUpdate: {
        service: 'SSM',
        action: 'getParameter',
        parameters: { Name: ssmParamName },
        physicalResourceId: cr.PhysicalResourceId.of('TritonUrlValidation')
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: [ssmParamArn] }),
      timeout: cdk.Duration.minutes(2)
    });
  }

  /**
   * Static method to resolve Triton URL from SSM parameter
   */
  public static getTritonUrlFromSsm(scope: Construct): string {
    return ssm.StringParameter.valueForStringParameter(scope, '/amira/triton_alb_url');
  }

  /**
   * Static method to resolve VPC configuration from SSM parameters
   */
  public static getVpcConfigFromSsm(scope: Construct): {
    vpcId: string;
    privateSubnetIds: string[];
    albSecurityGroupId: string;
  } {
    const vpcId = ssm.StringParameter.valueForStringParameter(scope, '/amira/vpc_id');
    const subnetIdsCsv = ssm.StringParameter.valueForStringParameter(scope, '/amira/vpc_private_subnet_ids');
    const albSecurityGroupId = ssm.StringParameter.valueForStringParameter(scope, '/amira/alb_sg_id');

    return {
      vpcId,
      privateSubnetIds: cdk.Fn.split(',', subnetIdsCsv),
      albSecurityGroupId
    };
  }
}
