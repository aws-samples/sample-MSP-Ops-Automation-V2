# API Gateway 5XX Error Rate Troubleshooting and Remediation

## Overview
This runbook addresses Amazon API Gateway REST APIs experiencing elevated 5XX server error rates. A spike in 5XX errors indicates backend failures — such as Lambda timeouts, integration misconfigurations, or downstream service outages — causing client-facing API errors, degraded user experience, and potential revenue loss.

## Applicable Alarms
- CloudWatch metric: AWS/ApiGateway 5XXError
- Threshold: 5XXError rate > 5% for 5 minutes
- Alarm pattern: API Gateway 5xx errors, server error rate elevated
- Related metrics: 4XXError, Count, Latency, IntegrationLatency

## Diagnosis Steps

Retrieve the alarm configuration and current state to understand the error threshold.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME</step>

Get the API Gateway details including name, description, and endpoint configuration.
<step>aws apigateway get-rest-api --rest-api-id API_ID</step>

List all deployment stages to identify which stage is affected.
<step>aws apigateway get-stages --rest-api-id API_ID</step>

## Remediation Steps

Review the current resource policy to check for misconfigurations blocking valid requests.
<step>aws apigateway get-rest-api --rest-api-id API_ID --query "policy"</step>

Update the API policy to remove any restrictive configurations causing 5XX errors.
<step on_success="1">aws apigateway update-rest-api --rest-api-id API_ID --patch-operations op=replace,path=/policy,value=""</step>

Deploy the updated configuration to the production stage so changes take effect.
<step on_success="2">aws apigateway create-deployment --rest-api-id API_ID --stage-name prod --description "Automated remediation deployment"</step>

## Verification

Confirm the alarm has returned to OK state after the API configuration update.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME --query "MetricAlarms[].StateValue"</step>

## Rollback

Restore the original API policy if the change caused unintended access issues.
<rollback>aws apigateway update-rest-api --rest-api-id API_ID --patch-operations op=replace,path=/policy,value="ORIGINAL_POLICY"</rollback>

Redeploy to apply the rollback policy to production.
<rollback>aws apigateway create-deployment --rest-api-id API_ID --stage-name prod --description "Rollback deployment"</rollback>
