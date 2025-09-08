import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import { Construct } from 'constructs';

export interface EcsClusterConstructProps {
  readonly vpc: ec2.IVpc;
  readonly instanceTypes: ec2.InstanceType[];
  readonly securityGroup: ec2.ISecurityGroup;
  readonly clusterName: string;
  readonly spotMaxPrice?: string; // Maximum spot price per hour, omit for on-demand
}

export class EcsClusterConstruct extends Construct {
  public readonly cluster: ecs.Cluster;
  public readonly capacityProviders: ecs.AsgCapacityProvider[];
  public readonly asgs: autoscaling.AutoScalingGroup[];

  constructor(scope: Construct, id: string, props: EcsClusterConstructProps) {
    super(scope, id);

    // ECS Cluster
    this.cluster = new ecs.Cluster(this, 'Cluster', {
      vpc: props.vpc,
      clusterName: props.clusterName,
      enableFargateCapacityProviders: true
    });

    // Configure cluster settings
    this.configureCluster();

    // Create instance role
    const instanceRole = this.createInstanceRole();

    // Create ASGs and capacity providers for each instance type
    const { asgs, capacityProviders } = this.createCapacityProviders(
      props.vpc,
      props.instanceTypes,
      props.securityGroup,
      instanceRole,
      props.spotMaxPrice
    );

    this.asgs = asgs;
    this.capacityProviders = capacityProviders;

    // Add capacity providers to cluster
    capacityProviders.forEach(cp => this.cluster.addAsgCapacityProvider(cp));
  }

  private configureCluster(): void {
    const cfnCluster = this.cluster.node.defaultChild as ecs.CfnCluster;

    // Enable Container Insights V2
    cfnCluster.addPropertyOverride('ClusterSettings', [{
      Name: 'containerInsights',
      Value: 'enabled'
    }]);

    // Configure execute command
    cfnCluster.addPropertyOverride('Configuration', {
      ExecuteCommandConfiguration: {
        LogConfiguration: {
          CloudWatchLogGroupName: `/ecs/${this.cluster.clusterName}`,
          CloudWatchEncryptionEnabled: true
        }
      }
    });
  }

  private createInstanceRole(): iam.Role {
    const role = new iam.Role(this, 'InstanceRole', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com')
    });

    role.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonEC2ContainerServiceforEC2Role')
    );
    role.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore')
    );

    return role;
  }

  private createCapacityProviders(
    vpc: ec2.IVpc,
    instanceTypes: ec2.InstanceType[],
    securityGroup: ec2.ISecurityGroup,
    role: iam.IRole,
    spotMaxPrice?: string
  ): { asgs: autoscaling.AutoScalingGroup[]; capacityProviders: ecs.AsgCapacityProvider[] } {
    const asgs: autoscaling.AutoScalingGroup[] = [];
    const capacityProviders: ecs.AsgCapacityProvider[] = [];

    instanceTypes.forEach((instanceType, index) => {
      const { asg, capacityProvider } = this.createAsgAndCapacityProvider(
        `Instance${index}`,
        vpc,
        instanceType,
        securityGroup,
        role,
        spotMaxPrice
      );

      asgs.push(asg);
      capacityProviders.push(capacityProvider);
    });

    return { asgs, capacityProviders };
  }

  private createAsgAndCapacityProvider(
    id: string,
    vpc: ec2.IVpc,
    instanceType: ec2.InstanceType,
    securityGroup: ec2.ISecurityGroup,
    role: iam.IRole,
    spotMaxPrice?: string
  ): { asg: autoscaling.AutoScalingGroup; capacityProvider: ecs.AsgCapacityProvider } {
    const ltOptions: any = {
      instanceType,
      machineImage: ecs.EcsOptimizedImage.amazonLinux2(ecs.AmiHardwareType.GPU),
      userData: ec2.UserData.forLinux(),
      securityGroup,
      role,
      requireImdsv2: true
    };

    // Only add spot options if spotMaxPrice is provided
    if (spotMaxPrice) {
      ltOptions.spotOptions = {
        requestType: ec2.SpotRequestType.PERSISTENT,
        interruptionBehavior: ec2.SpotInstanceInterruption.TERMINATE,
        maxPrice: spotMaxPrice
      };
    }

    const lt = new ec2.LaunchTemplate(this, `${id}LaunchTemplate`, ltOptions);

    const asg = new autoscaling.AutoScalingGroup(this, `${id}Asg`, {
      vpc,
      launchTemplate: lt,
      minCapacity: 0,
      maxCapacity: 10,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      capacityRebalance: true,
      healthChecks: autoscaling.HealthChecks.ec2({ gracePeriod: cdk.Duration.seconds(300) }), // Use EC2 health check since ALB health is handled at ECS service level
      newInstancesProtectedFromScaleIn: false
    });

    const capacityProvider = new ecs.AsgCapacityProvider(this, `${id}CapacityProvider`, {
      autoScalingGroup: asg,
      enableManagedScaling: true,
      enableManagedTerminationProtection: true,
      targetCapacityPercent: 100,
      machineImageType: ecs.MachineImageType.AMAZON_LINUX_2
    });

    return { asg, capacityProvider };
  }
}
