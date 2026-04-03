# DynamoDB Throttled Requests Troubleshooting and Remediation

## Overview
This runbook addresses Amazon DynamoDB tables experiencing throttled read or write requests. When DynamoDB throttles requests, operations are rejected with ProvisionedThroughputExceededException — causing application errors, data processing delays, and failed transactions. Throttling typically occurs when provisioned capacity is insufficient for the workload or when there are hot partition keys.

## Applicable Alarms
- CloudWatch metric: AWS/DynamoDB ThrottledRequests
- Threshold: ThrottledRequests > 0 for 5 minutes
- Alarm pattern: DynamoDB throttling, provisioned throughput exceeded, throttled requests
- Related metrics: ConsumedReadCapacityUnits, ConsumedWriteCapacityUnits, ReadThrottleEvents, WriteThrottleEvents

## Diagnosis Steps

Retrieve the alarm details to confirm the throttling pattern and severity.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME</step>

Check the table configuration including current provisioned throughput and billing mode.
<step>aws dynamodb describe-table --table-name TABLE_NAME --query "Table.[TableName,TableStatus,ProvisionedThroughput,BillingModeSummary]"</step>

## Remediation Steps

Check the current billing mode to determine the best remediation approach.
<step>aws dynamodb describe-table --table-name TABLE_NAME --query "Table.BillingModeSummary.BillingMode" --output text</step>

Switch to on-demand (PAY_PER_REQUEST) billing mode to eliminate throughput-based throttling entirely.
<step on_success="1">aws dynamodb update-table --table-name TABLE_NAME --billing-mode PAY_PER_REQUEST</step>

If switching to on-demand is not desired, increase the provisioned throughput capacity.
<step on_failure="2">aws dynamodb update-table --table-name TABLE_NAME --provisioned-throughput ReadCapacityUnits=100,WriteCapacityUnits=100</step>

## Verification

Confirm the alarm has returned to OK state and throttling has stopped.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME --query "MetricAlarms[].StateValue"</step>

## Rollback

Revert to provisioned billing mode with the original capacity settings if on-demand costs are too high.
<rollback>aws dynamodb update-table --table-name TABLE_NAME --billing-mode PROVISIONED --provisioned-throughput ReadCapacityUnits=ORIGINAL_RCU,WriteCapacityUnits=ORIGINAL_WCU</rollback>
