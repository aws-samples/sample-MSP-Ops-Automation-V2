# Lambda Function Throttling Troubleshooting and Remediation

## Overview
This runbook addresses AWS Lambda functions experiencing throttling events. When Lambda throttles invocations, function executions are rejected — causing event processing delays, dropped messages, and downstream service failures. Throttling occurs when concurrent executions exceed the function's reserved concurrency limit or the account-level concurrency limit.

## Applicable Alarms
- CloudWatch metric: AWS/Lambda Throttles
- Threshold: Throttles > 0 for 5 minutes
- Alarm pattern: Lambda throttling, function throttled, invocations rejected
- Related metrics: ConcurrentExecutions, Invocations, Duration, Errors

## Diagnosis Steps

Retrieve the alarm details to confirm the throttling event and duration.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME</step>

Check the function configuration including memory, timeout, and runtime settings.
<step>aws lambda get-function-configuration --function-name FUNCTION_NAME</step>

Review account-level concurrency limits to understand available capacity.
<step>aws lambda get-account-settings</step>

## Remediation Steps

Check if the function has reserved concurrency configured that might be limiting throughput.
<step>aws lambda get-function-concurrency --function-name FUNCTION_NAME</step>

Increase the reserved concurrency to allow more parallel executions.
<step on_success="1">aws lambda put-function-concurrency --function-name FUNCTION_NAME --reserved-concurrent-executions 100</step>

If reserved concurrency is insufficient, configure provisioned concurrency to pre-warm execution environments.
<step on_failure="2">aws lambda put-provisioned-concurrency-config --function-name FUNCTION_NAME --qualifier $LATEST --provisioned-concurrent-executions 10</step>

## Verification

Confirm the alarm has returned to OK state and throttling has stopped.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME --query "MetricAlarms[].StateValue"</step>

## Rollback

Remove the reserved concurrency setting to return to account-level defaults.
<rollback>aws lambda delete-function-concurrency --function-name FUNCTION_NAME</rollback>

Remove provisioned concurrency if it was configured during remediation.
<rollback>aws lambda delete-provisioned-concurrency-config --function-name FUNCTION_NAME --qualifier $LATEST</rollback>
