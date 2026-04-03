# EC2 High CPU Utilization Troubleshooting and Remediation

## Overview
This runbook addresses Amazon EC2 instances experiencing sustained high CPU utilization. When an EC2 instance CPU exceeds the configured threshold, application performance degrades — resulting in slow response times, request timeouts, and potential service outages. Common root causes include undersized instance types, runaway processes, traffic spikes, or inefficient application code.

## Applicable Alarms
- CloudWatch metric: AWS/EC2 CPUUtilization
- Threshold: CPUUtilization > 80% for 5 minutes
- Alarm pattern: EC2 high CPU, CPUUtilization exceeded
- Related metrics: NetworkIn, NetworkOut, StatusCheckFailed

## Diagnosis Steps

Retrieve the alarm details to confirm the alarm state and threshold configuration.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME</step>

Check the instance type, state, and CPU configuration to determine if vertical scaling is possible.
<step>aws ec2 describe-instances --instance-ids INSTANCE_ID --query "Reservations[].Instances[].[InstanceId,InstanceType,State.Name,CpuOptions]"</step>

Pull the last hour of CPU metrics to understand the utilization trend and severity.
<step>aws cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name CPUUtilization --dimensions Name=InstanceId,Value=INSTANCE_ID --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) --end-time $(date -u +%Y-%m-%dT%H:%M:%S) --period 300 --statistics Average</step>

## Remediation Steps

Identify the current instance type to determine an appropriate upgrade target.
<step>aws ec2 describe-instances --instance-ids INSTANCE_ID --query "Reservations[].Instances[].InstanceType" --output text</step>

Scale up the instance to a larger type with more CPU capacity. This requires the instance to be stopped first.
<step on_success="1">aws ec2 modify-instance-attribute --instance-id INSTANCE_ID --instance-type "{\"Value\": \"t3.large\"}"</step>

## Verification

Confirm the alarm has returned to OK state after remediation.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME --query "MetricAlarms[].StateValue"</step>

## Rollback

Revert the instance type to its original value if the change caused issues.
<rollback>aws ec2 modify-instance-attribute --instance-id INSTANCE_ID --instance-type "{\"Value\": \"ORIGINAL_INSTANCE_TYPE\"}"</rollback>
