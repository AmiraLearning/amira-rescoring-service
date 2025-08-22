import os
from typing import Any

import boto3


def _find_container_instance_arn(
    *, ecs_client: Any, cluster_arn: str, instance_id: str
) -> str | None:
    """Locate the ECS container instance ARN for a given EC2 instance ID.

    Args:
        ecs_client: Boto3 ECS client.
        cluster_arn: ARN of the ECS cluster.
        instance_id: EC2 instance ID to match.

    Returns:
        str | None: Matching container instance ARN, or None if not found.
    """
    next_token: str | None = None
    while True:
        request_kwargs: dict[str, Any] = {"cluster": cluster_arn}
        if next_token is not None:
            request_kwargs["nextToken"] = next_token

        list_response: dict[str, Any] = ecs_client.list_container_instances(
            **request_kwargs
        )
        container_instance_arns: list[str] = list_response.get(
            "containerInstanceArns", []
        )
        if not container_instance_arns:
            return None

        describe_response: dict[str, Any] = ecs_client.describe_container_instances(
            cluster=cluster_arn, containerInstances=container_instance_arns
        )
        container_instances: list[dict[str, Any]] = describe_response.get(
            "containerInstances", []
        )
        for container_instance in container_instances:
            ec2_instance_id: str | None = container_instance.get("ec2InstanceId")
            if ec2_instance_id == instance_id:
                container_instance_arn: str | None = container_instance.get(
                    "containerInstanceArn"
                )
                return container_instance_arn

        next_token = list_response.get("nextToken")
        if next_token is None:
            return None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle Spot ITN/Rebalance events and set ECS container instance to DRAINING.

    Args:
        event: EventBridge event for Spot interruption or rebalance recommendation.
        context: Lambda context (unused).

    Returns:
        dict[str, Any]: Status payload indicating action taken.
    """
    ecs_client: Any = boto3.client("ecs")
    cluster_arn: str | None = os.environ.get("CLUSTER_ARN")

    event_detail: dict[str, Any] = (
        event.get("detail", {}) if isinstance(event, dict) else {}
    )
    instance_id: str | None = event_detail.get("instance-id") or event_detail.get(
        "EC2InstanceId"
    )
    if instance_id is None or cluster_arn is None:
        return {"status": "missing-params"}

    container_instance_arn: str | None = _find_container_instance_arn(
        ecs_client=ecs_client, cluster_arn=cluster_arn, instance_id=instance_id
    )
    if container_instance_arn is None:
        return {"status": "no-container-instance"}

    _ = ecs_client.update_container_instances_state(
        cluster=cluster_arn,
        containerInstances=[container_instance_arn],
        status="DRAINING",
    )
    return {"status": "draining", "containerInstanceArn": container_instance_arn}
