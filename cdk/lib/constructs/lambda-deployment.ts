// import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as codedeploy from 'aws-cdk-lib/aws-codedeploy';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';

export interface LambdaDeploymentConstructProps {
  readonly lambdaFunction: lambda.Function;
  readonly enableCanaryDeployment?: boolean;
  readonly canaryConfig?: {
    readonly deploymentConfig: codedeploy.ILambdaDeploymentConfig;
    readonly alarms?: cw.IAlarm[];
    readonly preHook?: lambda.IFunction;
    readonly postHook?: lambda.IFunction;
  };
}

export class LambdaDeploymentConstruct extends Construct {
  public readonly alias: lambda.Alias;
  public readonly deploymentGroup?: codedeploy.LambdaDeploymentGroup;

  constructor(scope: Construct, id: string, props: LambdaDeploymentConstructProps) {
    super(scope, id);

    // Create a version for the Lambda function
    const version = props.lambdaFunction.currentVersion;

    // Create an alias pointing to the version
    this.alias = new lambda.Alias(this, 'ProdAlias', {
      aliasName: 'prod',
      version,
      description: 'Production alias with canary deployment support'
    });

    // Set up canary deployment if enabled
    if (props.enableCanaryDeployment) {
      this.setupCanaryDeployment(props);
    }
  }

  private setupCanaryDeployment(props: LambdaDeploymentConstructProps): void {
    const deploymentConfig = props.canaryConfig?.deploymentConfig ||
                           codedeploy.LambdaDeploymentConfig.CANARY_10PERCENT_5MINUTES;

    const deploymentGroup = new codedeploy.LambdaDeploymentGroup(this, 'DeploymentGroup', {
      alias: this.alias,
      deploymentConfig,
      alarms: props.canaryConfig?.alarms || [],
      preHook: props.canaryConfig?.preHook,
      postHook: props.canaryConfig?.postHook,
      // Auto-rollback on alarm or failure
      autoRollback: {
        failedDeployment: true,
        stoppedDeployment: true,
        deploymentInAlarm: props.canaryConfig?.alarms ? props.canaryConfig.alarms.length > 0 : false
      },
      // Deploy in batches to minimize blast radius
      deploymentGroupName: `${props.lambdaFunction.functionName}-deployment-group`
    });

    // Cast to mutable for assignment
    (this as any).deploymentGroup = deploymentGroup;

    // Add basic error rate alarm if no custom alarms provided
    if (!props.canaryConfig?.alarms || props.canaryConfig.alarms.length === 0) {
      const errorAlarm = new cw.Alarm(this, 'ErrorRateAlarm', {
        metric: this.alias.metricErrors(),
        threshold: 5,
        evaluationPeriods: 2,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarmDescription: 'Lambda error rate too high during deployment'
      });

      const durationAlarm = new cw.Alarm(this, 'DurationAlarm', {
        metric: this.alias.metricDuration(),
        threshold: 30000, // 30 seconds
        evaluationPeriods: 3,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarmDescription: 'Lambda duration too high during deployment'
      });

      // Update deployment group with default alarms
      const cfnDeploymentGroup = deploymentGroup.node.defaultChild as codedeploy.CfnDeploymentGroup;
      cfnDeploymentGroup.addPropertyOverride('AlarmConfiguration.Alarms', [
        { Name: errorAlarm.alarmName },
        { Name: durationAlarm.alarmName }
      ]);
      cfnDeploymentGroup.addPropertyOverride('AlarmConfiguration.Enabled', true);
    }
  }
}
