import * as cdk from 'aws-cdk-lib';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sns_subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as cwactions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import { Construct } from 'constructs';

export interface ObservabilityConstructProps {
  readonly alarmEmail?: string;
  readonly dashboardName: string;
  readonly metricSources: {
    // Lambda sources
    processingLambda?: lambda.IFunction;
    enqueueLambda?: lambda.IFunction;
    manualTriggerLambda?: lambda.IFunction;
    slackNotifierLambda?: lambda.IFunction;

    // ECS sources
    ecsService?: ecs.IService;
    ecsCluster?: ecs.ICluster;

    // SQS sources
    queue?: sqs.IQueue;
    dlq?: sqs.IQueue;

    // ALB sources
    alb?: elbv2.IApplicationLoadBalancer;
    targetGroup?: elbv2.IApplicationTargetGroup;

    // ASG sources
    asgs?: autoscaling.IAutoScalingGroup[];
  };
}

export class ObservabilityConstruct extends Construct {
  public readonly alarmTopic: sns.Topic;
  public readonly dashboard: cw.Dashboard;
  public readonly alarms: cw.Alarm[];
  public readonly compositeAlarms: cw.CompositeAlarm[];

  constructor(scope: Construct, id: string, props: ObservabilityConstructProps) {
    super(scope, id);

    // Create SNS topic for alerts
    this.alarmTopic = new sns.Topic(this, 'AlertsTopic', {
      displayName: 'Amira Processing Alerts'
    });

    // Optional email subscription
    if (props.alarmEmail) {
      this.alarmTopic.addSubscription(
        new sns_subscriptions.EmailSubscription(props.alarmEmail)
      );
    }

    // Create alarms
    this.alarms = this.createAlarms(props);

    // Create composite alarms for reduced noise
    this.compositeAlarms = this.createCompositeAlarms(props);

    // Create dashboard
    this.dashboard = this.createDashboard(props);
  }

  private createAlarms(props: ObservabilityConstructProps): cw.Alarm[] {
    const alarms: cw.Alarm[] = [];
    const alarmAction = new cwactions.SnsAction(this.alarmTopic);

    // SQS-related alarms
    if (props.metricSources.dlq) {
      const dlqDepthAlarm = new cw.Alarm(this, 'DlqDepthAlarm', {
        alarmName: `${props.dashboardName}-DLQ-Depth`,
        metric: props.metricSources.dlq.metricApproximateNumberOfMessagesVisible(),
        threshold: 1,
        evaluationPeriods: 1,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        alarmDescription: 'Messages in Dead Letter Queue'
      });
      dlqDepthAlarm.addAlarmAction(alarmAction);
      alarms.push(dlqDepthAlarm);
    }

    if (props.metricSources.queue) {
      const queueDepthAlarm = new cw.Alarm(this, 'QueueDepthAlarm', {
        alarmName: `${props.dashboardName}-Queue-Depth`,
        metric: props.metricSources.queue.metricApproximateNumberOfMessagesVisible(),
        threshold: 1000,
        evaluationPeriods: 3,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarmDescription: 'High queue depth detected'
      });
      queueDepthAlarm.addAlarmAction(alarmAction);
      alarms.push(queueDepthAlarm);

      const queueAgeAlarm = new cw.Alarm(this, 'QueueAgeAlarm', {
        alarmName: `${props.dashboardName}-Queue-Age`,
        metric: props.metricSources.queue.metricApproximateAgeOfOldestMessage(),
        threshold: 300,
        evaluationPeriods: 3,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarmDescription: 'Old messages in queue'
      });
      queueAgeAlarm.addAlarmAction(alarmAction);
      alarms.push(queueAgeAlarm);
    }

    // Lambda-related alarms
    if (props.metricSources.processingLambda) {
      const lambdaErrorsAlarm = new cw.Alarm(this, 'ProcessingErrorsAlarm', {
        alarmName: `${props.dashboardName}-Lambda-Errors`,
        metric: props.metricSources.processingLambda.metricErrors(),
        threshold: 10,
        evaluationPeriods: 2,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        alarmDescription: 'High processing Lambda error rate'
      });
      lambdaErrorsAlarm.addAlarmAction(alarmAction);
      alarms.push(lambdaErrorsAlarm);

      // Job completion detection
      if (props.metricSources.queue) {
        const concurrentExecutionsMetric = new cw.Metric({
          namespace: 'AWS/Lambda',
          metricName: 'ConcurrentExecutions',
          dimensionsMap: { FunctionName: props.metricSources.processingLambda.functionName },
          statistic: 'Average',
          period: cdk.Duration.minutes(2)
        });

        const jobCompletionExpr = new cw.MathExpression({
          expression: 'IF(queue < 1 AND concurrent < 1, 1, 0)',
          usingMetrics: {
            queue: props.metricSources.queue.metricApproximateNumberOfMessagesVisible({
              statistic: 'Average',
              period: cdk.Duration.minutes(2)
            }),
            concurrent: concurrentExecutionsMetric
          }
        });

        const jobCompletionAlarm = new cw.Alarm(this, 'JobCompletionAlarm', {
          alarmName: 'JobCompletionDetected',
          alarmDescription: 'All jobs processed (queue empty and no active executions)',
          metric: jobCompletionExpr,
          threshold: 1,
          evaluationPeriods: 1,
          comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
          treatMissingData: cw.TreatMissingData.NOT_BREACHING
        });
        jobCompletionAlarm.addAlarmAction(alarmAction);
        alarms.push(jobCompletionAlarm);
      }
    }

    // GPU/Triton-related alarms
    const gpuUtilMetric = new cw.Metric({
      namespace: 'CWAgent',
      metricName: 'DCGM_FI_DEV_GPU_UTIL',
      statistic: 'Average',
      period: cdk.Duration.minutes(1)
    });

    const gpuUtilLowAlarm = new cw.Alarm(this, 'GpuUtilLowAlarm', {
      metric: gpuUtilMetric,
      threshold: 20,
      evaluationPeriods: 5,
      comparisonOperator: cw.ComparisonOperator.LESS_THAN_THRESHOLD,
      alarmDescription: 'GPU utilization consistently low'
    });
    gpuUtilLowAlarm.addAlarmAction(alarmAction);
    alarms.push(gpuUtilLowAlarm);

    const gpuUtilHighAlarm = new cw.Alarm(this, 'GpuUtilHighAlarm', {
      metric: gpuUtilMetric,
      threshold: 95,
      evaluationPeriods: 3,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
      alarmDescription: 'GPU utilization critically high'
    });
    gpuUtilHighAlarm.addAlarmAction(alarmAction);
    alarms.push(gpuUtilHighAlarm);

    // Triton latency alarms
    const tritonLatencyMetric = new cw.Metric({
      namespace: 'CWAgent',
      metricName: 'nv_inference_request_duration_us',
      statistic: 'p95',
      period: cdk.Duration.minutes(1)
    });

    const tritonLatencyHighAlarm = new cw.Alarm(this, 'TritonLatencyHighAlarm', {
      metric: tritonLatencyMetric,
      threshold: 500000, // 500ms
      evaluationPeriods: 5,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
      alarmDescription: 'Triton inference latency too high'
    });
    tritonLatencyHighAlarm.addAlarmAction(alarmAction);
    alarms.push(tritonLatencyHighAlarm);

    const tritonFailuresMetric = new cw.Metric({
      namespace: 'CWAgent',
      metricName: 'nv_inference_fail',
      statistic: 'Sum',
      period: cdk.Duration.minutes(1)
    });

    const tritonFailuresAlarm = new cw.Alarm(this, 'TritonFailuresAlarm', {
      metric: tritonFailuresMetric,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      alarmDescription: 'Triton inference failures detected'
    });
    tritonFailuresAlarm.addAlarmAction(alarmAction);
    alarms.push(tritonFailuresAlarm);

    return alarms;
  }

  private createCompositeAlarms(props: ObservabilityConstructProps): cw.CompositeAlarm[] {
    const compositeAlarms: cw.CompositeAlarm[] = [];
    const alarmAction = new cwactions.SnsAction(this.alarmTopic);

    // Find related alarms for composite grouping
    const queueRelatedAlarms = this.alarms.filter(alarm =>
      alarm.alarmName.includes('Queue') || alarm.alarmName.includes('Dlq')
    );

    const lambdaRelatedAlarms = this.alarms.filter(alarm =>
      alarm.alarmName.includes('Processing') || alarm.alarmName.includes('Lambda')
    );

    const gpuRelatedAlarms = this.alarms.filter(alarm =>
      alarm.alarmName.includes('Gpu') || alarm.alarmName.includes('Triton')
    );

    // Enhanced queue health composite with math expression
    if (props.metricSources.queue) {
      const queueDepth = props.metricSources.queue.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(1),
        statistic: 'Average'
      });

      const queueAge = props.metricSources.queue.metricApproximateAgeOfOldestMessage({
        period: cdk.Duration.minutes(1),
        statistic: 'Average'
      });

      // Composite metric: healthy if queue depth < 10 AND age < 120 seconds
      const queueHealthExpression = new cw.MathExpression({
        expression: 'IF(depth < 10 AND age < 120, 1, 0)',
        usingMetrics: {
          depth: queueDepth,
          age: queueAge
        },
        label: 'Queue Health Status'
      });

      const queueHealthAlarm = new cw.Alarm(this, 'QueueHealthComposite', {
        alarmName: `${props.dashboardName}-Queue-Health-Composite`,
        metric: queueHealthExpression,
        threshold: 1,
        evaluationPeriods: 3,
        comparisonOperator: cw.ComparisonOperator.LESS_THAN_THRESHOLD,
        alarmDescription: 'Queue unhealthy - high depth (>10) or message age (>2min) detected',
        treatMissingData: cw.TreatMissingData.BREACHING
      });

      queueHealthAlarm.addAlarmAction(alarmAction);
      compositeAlarms.push(queueHealthAlarm as any); // Cast for type compatibility
    }

    // Traditional composite for other queue-related alarms
    if (queueRelatedAlarms.length >= 1) {
      const queueIssuesComposite = new cw.CompositeAlarm(this, 'QueueIssuesComposite', {
        alarmDescription: 'Multiple queue-related issues detected',
        alarmRule: cw.AlarmRule.anyOf(...queueRelatedAlarms.map(alarm =>
          cw.AlarmRule.fromAlarm(alarm, cw.AlarmState.ALARM)
        )),
        actionsEnabled: true
      });
      queueIssuesComposite.addAlarmAction(alarmAction);
      compositeAlarms.push(queueIssuesComposite);
    }

    // Processing health composite alarm (Lambda errors + GPU issues)
    if (lambdaRelatedAlarms.length >= 1 && gpuRelatedAlarms.length >= 1) {
      const processingHealthComposite = new cw.CompositeAlarm(this, 'ProcessingHealthComposite', {
        alarmDescription: 'Processing pipeline health issues - Lambda or GPU problems',
        alarmRule: cw.AlarmRule.anyOf(
          ...lambdaRelatedAlarms.map(alarm => cw.AlarmRule.fromAlarm(alarm, cw.AlarmState.ALARM)),
          ...gpuRelatedAlarms.map(alarm => cw.AlarmRule.fromAlarm(alarm, cw.AlarmState.ALARM))
        ),
        actionsEnabled: true
      });
      processingHealthComposite.addAlarmAction(alarmAction);
      compositeAlarms.push(processingHealthComposite);
    }

    // System degradation composite (multiple failures)
    if (this.alarms.length >= 3) {
      const systemDegradationComposite = new cw.CompositeAlarm(this, 'SystemDegradationComposite', {
        alarmDescription: 'System degradation - multiple components failing simultaneously',
        alarmRule: cw.AlarmRule.anyOf(
          // Any 2 or more alarms in ALARM state indicates system degradation
          ...this.getCombinations(this.alarms, 2).map(alarmPair =>
            cw.AlarmRule.allOf(...alarmPair.map(alarm =>
              cw.AlarmRule.fromAlarm(alarm, cw.AlarmState.ALARM)
            ))
          )
        ),
        actionsEnabled: true
      });
      systemDegradationComposite.addAlarmAction(alarmAction);
      compositeAlarms.push(systemDegradationComposite);
    }

    return compositeAlarms;
  }

  // Helper method to get combinations of alarms for system degradation detection
  private getCombinations<T>(items: T[], size: number): T[][] {
    if (size > items.length || size <= 0) return [];
    if (size === items.length) return [items];
    if (size === 1) return items.map(item => [item]);

    const combinations: T[][] = [];
    for (let i = 0; i < items.length - size + 1; i++) {
      const head = items[i];
      const tailCombinations = this.getCombinations(items.slice(i + 1), size - 1);
      tailCombinations.forEach(tail => combinations.push([head, ...tail]));
    }
    return combinations.slice(0, 10); // Limit to 10 combinations to avoid CloudFormation limits
  }

  private createDashboard(props: ObservabilityConstructProps): cw.Dashboard {
    const dashboard = new cw.Dashboard(this, 'Dashboard', {
      dashboardName: props.dashboardName
    });

    const widgets: cw.IWidget[] = [];

    // SQS metrics
    if (props.metricSources.queue || props.metricSources.dlq) {
      const queueMetrics: cw.IMetric[] = [];
      const queueRightMetrics: cw.IMetric[] = [];

      if (props.metricSources.queue) {
        queueMetrics.push(props.metricSources.queue.metricApproximateNumberOfMessagesVisible());
        queueRightMetrics.push(props.metricSources.queue.metricApproximateAgeOfOldestMessage());
      }

      if (props.metricSources.dlq) {
        queueMetrics.push(props.metricSources.dlq.metricApproximateNumberOfMessagesVisible());
      }

      widgets.push(new cw.GraphWidget({
        title: 'SQS Queue Metrics',
        left: queueMetrics,
        right: queueRightMetrics,
        width: 12
      }));
    }

    // Lambda metrics
    if (props.metricSources.processingLambda) {
      const lambdaMetrics = [
        props.metricSources.processingLambda.metricInvocations(),
        props.metricSources.processingLambda.metricDuration()
      ];
      const lambdaRightMetrics = [
        props.metricSources.processingLambda.metricErrors(),
        props.metricSources.processingLambda.metricThrottles()
      ];

      widgets.push(new cw.GraphWidget({
        title: 'Lambda Processing Metrics',
        left: lambdaMetrics,
        right: lambdaRightMetrics,
        width: 12
      }));

      // Concurrency
      widgets.push(new cw.GraphWidget({
        title: 'Lambda Concurrency',
        left: [new cw.Metric({
          namespace: 'AWS/Lambda',
          metricName: 'ConcurrentExecutions',
          dimensionsMap: { FunctionName: props.metricSources.processingLambda.functionName },
          statistic: 'Average',
          period: cdk.Duration.minutes(2)
        })],
        width: 24
      }));
    }

    // GPU metrics
    widgets.push(new cw.GraphWidget({
      title: 'GPU Utilization',
      left: [new cw.Metric({
        namespace: 'CWAgent',
        metricName: 'DCGM_FI_DEV_GPU_UTIL',
        statistic: 'Average',
        period: cdk.Duration.minutes(1)
      })],
      width: 12
    }));

    widgets.push(new cw.GraphWidget({
      title: 'GPU Memory',
      left: [
        new cw.Metric({
          namespace: 'CWAgent',
          metricName: 'DCGM_FI_DEV_FB_USED',
          statistic: 'Average',
          period: cdk.Duration.minutes(1)
        }),
        new cw.Metric({
          namespace: 'CWAgent',
          metricName: 'DCGM_FI_DEV_FB_TOTAL',
          statistic: 'Average',
          period: cdk.Duration.minutes(1)
        })
      ],
      width: 12
    }));

    // Triton metrics
    widgets.push(new cw.GraphWidget({
      title: 'Triton Inference Latency (p95 µs)',
      left: [new cw.Metric({
        namespace: 'CWAgent',
        metricName: 'nv_inference_request_duration_us',
        statistic: 'p95',
        period: cdk.Duration.minutes(1)
      })],
      width: 12
    }));

    widgets.push(new cw.GraphWidget({
      title: 'Triton Queue Latency (p95 µs)',
      left: [new cw.Metric({
        namespace: 'CWAgent',
        metricName: 'nv_inference_queue_duration_us',
        statistic: 'p95',
        period: cdk.Duration.minutes(1)
      })],
      width: 12
    }));

    widgets.push(new cw.GraphWidget({
      title: 'Triton Throughput & Failures',
      left: [new cw.Metric({
        namespace: 'CWAgent',
        metricName: 'nv_inference_count',
        statistic: 'Sum',
        period: cdk.Duration.minutes(1)
      })],
      right: [new cw.Metric({
        namespace: 'CWAgent',
        metricName: 'nv_inference_fail',
        statistic: 'Sum',
        period: cdk.Duration.minutes(1)
      })],
      width: 24
    }));

    // Business metrics
    widgets.push(new cw.GraphWidget({
      title: 'Job Completion Tracking',
      left: [
        new cw.Metric({
          namespace: 'Amira/Jobs',
          metricName: 'JobsCompleted',
          statistic: 'Sum'
        }),
        new cw.Metric({
          namespace: 'Amira/Jobs',
          metricName: 'JobsFailed',
          statistic: 'Sum'
        })
      ],
      width: 12
    }));

    widgets.push(new cw.GraphWidget({
      title: 'Processing Time (ms)',
      left: [new cw.Metric({
        namespace: 'Amira/Jobs',
        metricName: 'ProcessingTime',
        statistic: 'Average'
      })],
      right: [new cw.Metric({
        namespace: 'Amira/Jobs',
        metricName: 'ProcessingTime',
        statistic: 'p95'
      })],
      width: 12
    }));

    // ECS metrics
    if (props.metricSources.ecsService && props.metricSources.ecsCluster) {
      widgets.push(new cw.GraphWidget({
        title: 'ECS Service Desired vs Running',
        left: [
          new cw.Metric({
            namespace: 'ECS/ContainerInsights',
            metricName: 'ServiceDesiredCount',
            dimensionsMap: {
              ClusterName: props.metricSources.ecsCluster.clusterName,
              ServiceName: props.metricSources.ecsService.serviceName
            },
            statistic: 'Average'
          }),
          new cw.Metric({
            namespace: 'ECS/ContainerInsights',
            metricName: 'ServiceRunningCount',
            dimensionsMap: {
              ClusterName: props.metricSources.ecsCluster.clusterName,
              ServiceName: props.metricSources.ecsService.serviceName
            },
            statistic: 'Average'
          })
        ],
        width: 24
      }));
    }

    // ASG metrics
    if (props.metricSources.asgs && props.metricSources.asgs.length > 0) {
      const asgLeftMetrics: cw.IMetric[] = [];
      const asgRightMetrics: cw.IMetric[] = [];

      props.metricSources.asgs.forEach((asg, index) => {
        const desiredMetric = new cw.Metric({
          namespace: 'AWS/AutoScaling',
          metricName: 'GroupDesiredCapacity',
          dimensionsMap: { AutoScalingGroupName: asg.autoScalingGroupName },
          statistic: 'Average'
        });
        const inServiceMetric = new cw.Metric({
          namespace: 'AWS/AutoScaling',
          metricName: 'GroupInServiceInstances',
          dimensionsMap: { AutoScalingGroupName: asg.autoScalingGroupName },
          statistic: 'Average'
        });

        if (index === 0) {
          asgLeftMetrics.push(desiredMetric, inServiceMetric);
        } else {
          asgRightMetrics.push(desiredMetric, inServiceMetric);
        }
      });

      widgets.push(new cw.GraphWidget({
        title: 'Auto Scaling Groups',
        left: asgLeftMetrics,
        right: asgRightMetrics,
        width: 24
      }));
    }

    dashboard.addWidgets(...widgets);
    return dashboard;
  }
}
