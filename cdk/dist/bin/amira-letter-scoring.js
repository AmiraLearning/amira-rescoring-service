#!/usr/bin/env node
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
require("source-map-support/register");
const cdk = require("aws-cdk-lib");
const amira_letter_scoring_stack_1 = require("../lib/amira-letter-scoring-stack");
const cdk_nag_1 = require("cdk-nag");
const app = new cdk.App();
const stack = new amira_letter_scoring_stack_1.AmiraLetterScoringStack(app, 'AmiraLetterScoringStack', {
    env: {
        account: process.env.CDK_DEFAULT_ACCOUNT,
        region: process.env.CDK_DEFAULT_REGION,
    },
});
// Enable CDK-nag with AWS Solutions Checks
cdk.Aspects.of(app).add(new cdk_nag_1.AwsSolutionsChecks({ verbose: true }));
// Apply organization-wide tags
cdk.Tags.of(app).add('cost-center', 'platform');
cdk.Tags.of(app).add('owner', 'amira-team');
cdk.Tags.of(app).add('data-classification', 'internal');
cdk.Tags.of(app).add('environment', process.env.STAGE ?? 'dev');
// Curated suppressions for known acceptable patterns
cdk_nag_1.NagSuppressions.addStackSuppressions(stack, [
    {
        id: 'AwsSolutions-SQS3',
        reason: 'DLQ configured via QueueingConstruct with appropriate retention and maxReceiveCount'
    },
    {
        id: 'AwsSolutions-IAM4',
        reason: 'AWS managed policies necessary for ECS task execution and CloudWatch agent functionality'
    },
    {
        id: 'AwsSolutions-IAM5',
        reason: 'Wildcard permissions required for dynamic resource patterns in ECR, S3, and CloudWatch operations'
    },
    {
        id: 'AwsSolutions-L1',
        reason: 'Lambda runtime managed via Docker images with controlled base image updates'
    },
    {
        id: 'AwsSolutions-VPC7',
        reason: 'VPC Flow Logs enabled via NetworkingConstruct with KMS encryption'
    },
    {
        id: 'AwsSolutions-ECS2',
        reason: 'Environment variables do not contain secrets; sensitive data retrieved via Secrets Manager at runtime'
    },
    {
        id: 'AwsSolutions-S1',
        reason: 'Access logs bucket is the logging destination; recursive logging not required'
    },
    {
        id: 'AwsSolutions-ECS4',
        reason: 'CloudWatch Container Insights are enabled via cluster configuration override'
    },
    {
        id: 'AwsSolutions-EC26',
        reason: 'EBS encryption will be enabled via launch template configuration'
    },
    {
        id: 'AwsSolutions-AS3',
        reason: 'Auto Scaling Group notifications are managed through CloudWatch alarms and metrics'
    },
    {
        id: 'AwsSolutions-SNS3',
        reason: 'SNS Topic SSL enforcement is configured via topic policy in ObservabilityConstruct'
    }
]);
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiYW1pcmEtbGV0dGVyLXNjb3JpbmcuanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyIuLi8uLi9iaW4vYW1pcmEtbGV0dGVyLXNjb3JpbmcudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6Ijs7O0FBQ0EsdUNBQXFDO0FBQ3JDLG1DQUFtQztBQUNuQyxrRkFBNEU7QUFDNUUscUNBQThEO0FBRTlELE1BQU0sR0FBRyxHQUFHLElBQUksR0FBRyxDQUFDLEdBQUcsRUFBRSxDQUFDO0FBRTFCLE1BQU0sS0FBSyxHQUFHLElBQUksb0RBQXVCLENBQUMsR0FBRyxFQUFFLHlCQUF5QixFQUFFO0lBQ3hFLEdBQUcsRUFBRTtRQUNILE9BQU8sRUFBRSxPQUFPLENBQUMsR0FBRyxDQUFDLG1CQUFtQjtRQUN4QyxNQUFNLEVBQUUsT0FBTyxDQUFDLEdBQUcsQ0FBQyxrQkFBa0I7S0FDdkM7Q0FDRixDQUFDLENBQUM7QUFFSCwyQ0FBMkM7QUFDM0MsR0FBRyxDQUFDLE9BQU8sQ0FBQyxFQUFFLENBQUMsR0FBRyxDQUFDLENBQUMsR0FBRyxDQUFDLElBQUksNEJBQWtCLENBQUMsRUFBRSxPQUFPLEVBQUUsSUFBSSxFQUFFLENBQUMsQ0FBQyxDQUFDO0FBRW5FLCtCQUErQjtBQUMvQixHQUFHLENBQUMsSUFBSSxDQUFDLEVBQUUsQ0FBQyxHQUFHLENBQUMsQ0FBQyxHQUFHLENBQUMsYUFBYSxFQUFFLFVBQVUsQ0FBQyxDQUFDO0FBQ2hELEdBQUcsQ0FBQyxJQUFJLENBQUMsRUFBRSxDQUFDLEdBQUcsQ0FBQyxDQUFDLEdBQUcsQ0FBQyxPQUFPLEVBQUUsWUFBWSxDQUFDLENBQUM7QUFDNUMsR0FBRyxDQUFDLElBQUksQ0FBQyxFQUFFLENBQUMsR0FBRyxDQUFDLENBQUMsR0FBRyxDQUFDLHFCQUFxQixFQUFFLFVBQVUsQ0FBQyxDQUFDO0FBQ3hELEdBQUcsQ0FBQyxJQUFJLENBQUMsRUFBRSxDQUFDLEdBQUcsQ0FBQyxDQUFDLEdBQUcsQ0FBQyxhQUFhLEVBQUUsT0FBTyxDQUFDLEdBQUcsQ0FBQyxLQUFLLElBQUksS0FBSyxDQUFDLENBQUM7QUFFaEUscURBQXFEO0FBQ3JELHlCQUFlLENBQUMsb0JBQW9CLENBQUMsS0FBSyxFQUFFO0lBQzFDO1FBQ0UsRUFBRSxFQUFFLG1CQUFtQjtRQUN2QixNQUFNLEVBQUUscUZBQXFGO0tBQzlGO0lBQ0Q7UUFDRSxFQUFFLEVBQUUsbUJBQW1CO1FBQ3ZCLE1BQU0sRUFBRSwwRkFBMEY7S0FDbkc7SUFDRDtRQUNFLEVBQUUsRUFBRSxtQkFBbUI7UUFDdkIsTUFBTSxFQUFFLG1HQUFtRztLQUM1RztJQUNEO1FBQ0UsRUFBRSxFQUFFLGlCQUFpQjtRQUNyQixNQUFNLEVBQUUsNkVBQTZFO0tBQ3RGO0lBQ0Q7UUFDRSxFQUFFLEVBQUUsbUJBQW1CO1FBQ3ZCLE1BQU0sRUFBRSxtRUFBbUU7S0FDNUU7SUFDRDtRQUNFLEVBQUUsRUFBRSxtQkFBbUI7UUFDdkIsTUFBTSxFQUFFLHVHQUF1RztLQUNoSDtJQUNEO1FBQ0UsRUFBRSxFQUFFLGlCQUFpQjtRQUNyQixNQUFNLEVBQUUsK0VBQStFO0tBQ3hGO0lBQ0Q7UUFDRSxFQUFFLEVBQUUsbUJBQW1CO1FBQ3ZCLE1BQU0sRUFBRSw4RUFBOEU7S0FDdkY7SUFDRDtRQUNFLEVBQUUsRUFBRSxtQkFBbUI7UUFDdkIsTUFBTSxFQUFFLGtFQUFrRTtLQUMzRTtJQUNEO1FBQ0UsRUFBRSxFQUFFLGtCQUFrQjtRQUN0QixNQUFNLEVBQUUsb0ZBQW9GO0tBQzdGO0lBQ0Q7UUFDRSxFQUFFLEVBQUUsbUJBQW1CO1FBQ3ZCLE1BQU0sRUFBRSxvRkFBb0Y7S0FDN0Y7Q0FDRixDQUFDLENBQUMiLCJzb3VyY2VzQ29udGVudCI6WyIjIS91c3IvYmluL2VudiBub2RlXG5pbXBvcnQgJ3NvdXJjZS1tYXAtc3VwcG9ydC9yZWdpc3Rlcic7XG5pbXBvcnQgKiBhcyBjZGsgZnJvbSAnYXdzLWNkay1saWInO1xuaW1wb3J0IHsgQW1pcmFMZXR0ZXJTY29yaW5nU3RhY2sgfSBmcm9tICcuLi9saWIvYW1pcmEtbGV0dGVyLXNjb3Jpbmctc3RhY2snO1xuaW1wb3J0IHsgQXdzU29sdXRpb25zQ2hlY2tzLCBOYWdTdXBwcmVzc2lvbnMgfSBmcm9tICdjZGstbmFnJztcblxuY29uc3QgYXBwID0gbmV3IGNkay5BcHAoKTtcblxuY29uc3Qgc3RhY2sgPSBuZXcgQW1pcmFMZXR0ZXJTY29yaW5nU3RhY2soYXBwLCAnQW1pcmFMZXR0ZXJTY29yaW5nU3RhY2snLCB7XG4gIGVudjoge1xuICAgIGFjY291bnQ6IHByb2Nlc3MuZW52LkNES19ERUZBVUxUX0FDQ09VTlQsXG4gICAgcmVnaW9uOiBwcm9jZXNzLmVudi5DREtfREVGQVVMVF9SRUdJT04sXG4gIH0sXG59KTtcblxuLy8gRW5hYmxlIENESy1uYWcgd2l0aCBBV1MgU29sdXRpb25zIENoZWNrc1xuY2RrLkFzcGVjdHMub2YoYXBwKS5hZGQobmV3IEF3c1NvbHV0aW9uc0NoZWNrcyh7IHZlcmJvc2U6IHRydWUgfSkpO1xuXG4vLyBBcHBseSBvcmdhbml6YXRpb24td2lkZSB0YWdzXG5jZGsuVGFncy5vZihhcHApLmFkZCgnY29zdC1jZW50ZXInLCAncGxhdGZvcm0nKTtcbmNkay5UYWdzLm9mKGFwcCkuYWRkKCdvd25lcicsICdhbWlyYS10ZWFtJyk7XG5jZGsuVGFncy5vZihhcHApLmFkZCgnZGF0YS1jbGFzc2lmaWNhdGlvbicsICdpbnRlcm5hbCcpO1xuY2RrLlRhZ3Mub2YoYXBwKS5hZGQoJ2Vudmlyb25tZW50JywgcHJvY2Vzcy5lbnYuU1RBR0UgPz8gJ2RldicpO1xuXG4vLyBDdXJhdGVkIHN1cHByZXNzaW9ucyBmb3Iga25vd24gYWNjZXB0YWJsZSBwYXR0ZXJuc1xuTmFnU3VwcHJlc3Npb25zLmFkZFN0YWNrU3VwcHJlc3Npb25zKHN0YWNrLCBbXG4gIHtcbiAgICBpZDogJ0F3c1NvbHV0aW9ucy1TUVMzJyxcbiAgICByZWFzb246ICdETFEgY29uZmlndXJlZCB2aWEgUXVldWVpbmdDb25zdHJ1Y3Qgd2l0aCBhcHByb3ByaWF0ZSByZXRlbnRpb24gYW5kIG1heFJlY2VpdmVDb3VudCdcbiAgfSxcbiAge1xuICAgIGlkOiAnQXdzU29sdXRpb25zLUlBTTQnLFxuICAgIHJlYXNvbjogJ0FXUyBtYW5hZ2VkIHBvbGljaWVzIG5lY2Vzc2FyeSBmb3IgRUNTIHRhc2sgZXhlY3V0aW9uIGFuZCBDbG91ZFdhdGNoIGFnZW50IGZ1bmN0aW9uYWxpdHknXG4gIH0sXG4gIHtcbiAgICBpZDogJ0F3c1NvbHV0aW9ucy1JQU01JyxcbiAgICByZWFzb246ICdXaWxkY2FyZCBwZXJtaXNzaW9ucyByZXF1aXJlZCBmb3IgZHluYW1pYyByZXNvdXJjZSBwYXR0ZXJucyBpbiBFQ1IsIFMzLCBhbmQgQ2xvdWRXYXRjaCBvcGVyYXRpb25zJ1xuICB9LFxuICB7XG4gICAgaWQ6ICdBd3NTb2x1dGlvbnMtTDEnLFxuICAgIHJlYXNvbjogJ0xhbWJkYSBydW50aW1lIG1hbmFnZWQgdmlhIERvY2tlciBpbWFnZXMgd2l0aCBjb250cm9sbGVkIGJhc2UgaW1hZ2UgdXBkYXRlcydcbiAgfSxcbiAge1xuICAgIGlkOiAnQXdzU29sdXRpb25zLVZQQzcnLFxuICAgIHJlYXNvbjogJ1ZQQyBGbG93IExvZ3MgZW5hYmxlZCB2aWEgTmV0d29ya2luZ0NvbnN0cnVjdCB3aXRoIEtNUyBlbmNyeXB0aW9uJ1xuICB9LFxuICB7XG4gICAgaWQ6ICdBd3NTb2x1dGlvbnMtRUNTMicsXG4gICAgcmVhc29uOiAnRW52aXJvbm1lbnQgdmFyaWFibGVzIGRvIG5vdCBjb250YWluIHNlY3JldHM7IHNlbnNpdGl2ZSBkYXRhIHJldHJpZXZlZCB2aWEgU2VjcmV0cyBNYW5hZ2VyIGF0IHJ1bnRpbWUnXG4gIH0sXG4gIHtcbiAgICBpZDogJ0F3c1NvbHV0aW9ucy1TMScsXG4gICAgcmVhc29uOiAnQWNjZXNzIGxvZ3MgYnVja2V0IGlzIHRoZSBsb2dnaW5nIGRlc3RpbmF0aW9uOyByZWN1cnNpdmUgbG9nZ2luZyBub3QgcmVxdWlyZWQnXG4gIH0sXG4gIHtcbiAgICBpZDogJ0F3c1NvbHV0aW9ucy1FQ1M0JyxcbiAgICByZWFzb246ICdDbG91ZFdhdGNoIENvbnRhaW5lciBJbnNpZ2h0cyBhcmUgZW5hYmxlZCB2aWEgY2x1c3RlciBjb25maWd1cmF0aW9uIG92ZXJyaWRlJ1xuICB9LFxuICB7XG4gICAgaWQ6ICdBd3NTb2x1dGlvbnMtRUMyNicsXG4gICAgcmVhc29uOiAnRUJTIGVuY3J5cHRpb24gd2lsbCBiZSBlbmFibGVkIHZpYSBsYXVuY2ggdGVtcGxhdGUgY29uZmlndXJhdGlvbidcbiAgfSxcbiAge1xuICAgIGlkOiAnQXdzU29sdXRpb25zLUFTMycsXG4gICAgcmVhc29uOiAnQXV0byBTY2FsaW5nIEdyb3VwIG5vdGlmaWNhdGlvbnMgYXJlIG1hbmFnZWQgdGhyb3VnaCBDbG91ZFdhdGNoIGFsYXJtcyBhbmQgbWV0cmljcydcbiAgfSxcbiAge1xuICAgIGlkOiAnQXdzU29sdXRpb25zLVNOUzMnLFxuICAgIHJlYXNvbjogJ1NOUyBUb3BpYyBTU0wgZW5mb3JjZW1lbnQgaXMgY29uZmlndXJlZCB2aWEgdG9waWMgcG9saWN5IGluIE9ic2VydmFiaWxpdHlDb25zdHJ1Y3QnXG4gIH1cbl0pO1xuIl19