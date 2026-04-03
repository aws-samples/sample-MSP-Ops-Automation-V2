# RDS High Database Connections Troubleshooting and Remediation

## Overview
This runbook addresses Amazon RDS database instances experiencing a high number of active connections approaching or exceeding the configured maximum. When connection count exceeds the threshold, new connections are rejected, causing application errors, failed queries, and potential database deadlocks. Common causes include connection pool exhaustion, connection leaks in application code, or sudden traffic spikes.

## Applicable Alarms
- CloudWatch metric: AWS/RDS DatabaseConnections
- Threshold: DatabaseConnections > 80% of max_connections
- Alarm pattern: RDS high connections, database connection limit
- Related metrics: CPUUtilization, FreeableMemory, ReadIOPS, WriteIOPS

## Diagnosis Steps

Retrieve the alarm details to confirm current state and threshold configuration.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME</step>

Check the RDS instance class, status, and endpoint to understand current capacity.
<step>aws rds describe-db-instances --db-instance-identifier DB_INSTANCE_ID --query "DBInstances[].[DBInstanceIdentifier,DBInstanceClass,DBInstanceStatus,Endpoint]"</step>

## Remediation Steps

Identify the parameter group to modify the max_connections setting.
<step>aws rds describe-db-instances --db-instance-identifier DB_INSTANCE_ID --query "DBInstances[].DBParameterGroups[].DBParameterGroupName" --output text</step>

Increase the max_connections parameter. Note: this change requires a reboot to take effect.
<step on_success="1">aws rds modify-db-parameter-group --db-parameter-group-name PARAM_GROUP --parameters "ParameterName=max_connections,ParameterValue=200,ApplyMethod=pending-reboot"</step>

Scale up the RDS instance to a larger class with more memory, which supports more connections.
<step on_success="1">aws rds modify-db-instance --db-instance-identifier DB_INSTANCE_ID --db-instance-class db.r5.large --apply-immediately</step>

## Verification

Confirm the alarm has returned to OK state after remediation.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME --query "MetricAlarms[].StateValue"</step>

## Rollback

Revert the instance class to the original size if the change was not effective.
<rollback>aws rds modify-db-instance --db-instance-identifier DB_INSTANCE_ID --db-instance-class ORIGINAL_INSTANCE_CLASS --apply-immediately</rollback>
