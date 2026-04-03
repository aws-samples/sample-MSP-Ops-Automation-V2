# ECS Service Task Failures Troubleshooting and Remediation

## Overview
This runbook addresses Amazon ECS services where running task count has dropped below the desired count. When ECS tasks fail to start or crash repeatedly, the service operates at reduced capacity — causing degraded performance, increased latency, and potential outages. Common causes include container image issues, insufficient resources, health check failures, or application crashes.

## Applicable Alarms
- CloudWatch metric: AWS/ECS RunningTaskCount, CPUUtilization
- Threshold: Running tasks < desired tasks for 10 minutes
- Alarm pattern: ECS task failure, service degraded, tasks not running
- Related metrics: MemoryUtilization, RunningTaskCount, DesiredTaskCount

## Diagnosis Steps

Retrieve the alarm details to understand the task failure pattern.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME</step>

Check the service status including running count vs desired count to quantify the gap.
<step>aws ecs describe-services --cluster CLUSTER_NAME --services SERVICE_NAME --query "services[].[serviceName,runningCount,desiredCount,status]"</step>

List recently stopped tasks to identify failure patterns and error messages.
<step>aws ecs list-tasks --cluster CLUSTER_NAME --service-name SERVICE_NAME --desired-status STOPPED</step>

## Remediation Steps

Check the current desired count to understand the expected capacity.
<step>aws ecs describe-services --cluster CLUSTER_NAME --services SERVICE_NAME --query "services[].desiredCount" --output text</step>

Force a new deployment to replace failing tasks with fresh container instances.
<step on_success="1">aws ecs update-service --cluster CLUSTER_NAME --service SERVICE_NAME --force-new-deployment</step>

If force deployment fails, explicitly set the desired count to ensure minimum capacity.
<step on_failure="2">aws ecs update-service --cluster CLUSTER_NAME --service SERVICE_NAME --desired-count 2</step>

## Verification

Confirm the alarm has returned to OK state and tasks are running at desired count.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME --query "MetricAlarms[].StateValue"</step>

## Rollback

Revert the desired count to the original value if the change caused issues.
<rollback>aws ecs update-service --cluster CLUSTER_NAME --service SERVICE_NAME --desired-count ORIGINAL_DESIRED_COUNT</rollback>
