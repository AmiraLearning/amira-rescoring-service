import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as kms from 'aws-cdk-lib/aws-kms';
import { Construct } from 'constructs';

export interface QueueingConstructProps {
  readonly kmsKey?: kms.IKey;
  readonly visibilityTimeout?: cdk.Duration;
  readonly maxReceiveCount?: number;
  readonly dlqRetentionPeriod?: cdk.Duration;
  readonly receiveMessageWaitTimeSeconds?: number; // Long polling duration (0-20 seconds)
}

export class QueueingConstruct extends Construct {
  public readonly queue: sqs.Queue;
  public readonly dlq: sqs.Queue;

  constructor(scope: Construct, id: string, props: QueueingConstructProps = {}) {
    super(scope, id);

    // Dead Letter Queue
    this.dlq = new sqs.Queue(this, 'DLQ', {
      retentionPeriod: props.dlqRetentionPeriod ?? cdk.Duration.days(14),
      encryption: props.kmsKey ? sqs.QueueEncryption.KMS : sqs.QueueEncryption.KMS_MANAGED,
      encryptionMasterKey: props.kmsKey,
      enforceSSL: true
    });

    // Main queue
    this.queue = new sqs.Queue(this, 'Queue', {
      visibilityTimeout: props.visibilityTimeout ?? cdk.Duration.minutes(15),
      deadLetterQueue: {
        queue: this.dlq,
        maxReceiveCount: props.maxReceiveCount ?? 3
      },
      encryption: props.kmsKey ? sqs.QueueEncryption.KMS : sqs.QueueEncryption.KMS_MANAGED,
      encryptionMasterKey: props.kmsKey,
      enforceSSL: true,
      receiveMessageWaitTime: cdk.Duration.seconds(props.receiveMessageWaitTimeSeconds ?? 20) // Long polling for cost optimization
    });
  }
}
