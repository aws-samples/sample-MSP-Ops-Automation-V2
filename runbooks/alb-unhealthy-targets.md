# ALB Unhealthy Target Count Troubleshooting and Remediation

## Overview
This runbook addresses Application Load Balancers (ALB) with unhealthy targets in their target groups. When targets fail health checks, the ALB stops routing traffic to them — reducing available capacity, causing uneven load distribution, and potentially leading to service degradation or outages if too many targets become unhealthy.

## Applicable Alarms
- CloudWatch metric: AWS/ApplicationELB UnHealthyHostCount
- Threshold: UnHealthyHostCount > 0 for 5 minutes
- Alarm pattern: ALB unhealthy targets, unhealthy host count, target health check failure
- Related metrics: HealthyHostCount, RequestCount, TargetResponseTime, HTTPCode_Target_5XX_Count

## Diagnosis Steps

Retrieve the alarm details to confirm the unhealthy target pattern.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME</step>

Check the health status of all targets in the target group to identify which are unhealthy and why.
<step>aws elbv2 describe-target-health --target-group-arn TARGET_GROUP_ARN</step>

Review the target group health check configuration to understand the check path, port, and thresholds.
<step>aws elbv2 describe-target-groups --target-group-arns TARGET_GROUP_ARN --query "TargetGroups[].[TargetGroupName,HealthCheckPath,HealthCheckPort]"</step>

## Remediation Steps

Identify the specific unhealthy targets and the reasons for their health check failures.
<step>aws elbv2 describe-target-health --target-group-arn TARGET_GROUP_ARN --query "TargetHealthDescriptions[?TargetHealth.State=='unhealthy'].[Target.Id,TargetHealth.Reason]"</step>

Adjust health check parameters to be more tolerant while targets recover — reduce check interval and adjust thresholds.
<step on_success="1">aws elbv2 modify-target-group --target-group-arn TARGET_GROUP_ARN --health-check-interval-seconds 15 --healthy-threshold-count 2 --unhealthy-threshold-count 3</step>

Deregister persistently unhealthy targets to prevent them from affecting the target group status.
<step on_success="1">aws elbv2 deregister-targets --target-group-arn TARGET_GROUP_ARN --targets Id=UNHEALTHY_TARGET_ID</step>

## Verification

Confirm the alarm has returned to OK state and all remaining targets are healthy.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME --query "MetricAlarms[].StateValue"</step>

## Rollback

Restore the original health check configuration if the changes were too lenient.
<rollback>aws elbv2 modify-target-group --target-group-arn TARGET_GROUP_ARN --health-check-interval-seconds ORIGINAL_INTERVAL --healthy-threshold-count ORIGINAL_HEALTHY_THRESHOLD --unhealthy-threshold-count ORIGINAL_UNHEALTHY_THRESHOLD</rollback>

Re-register the deregistered target if it was removed prematurely.
<rollback>aws elbv2 register-targets --target-group-arn TARGET_GROUP_ARN --targets Id=UNHEALTHY_TARGET_ID</rollback>
