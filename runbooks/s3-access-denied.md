# S3 Access Denied (403) Errors Troubleshooting and Remediation

## Overview
This runbook addresses Amazon S3 buckets experiencing elevated 403 AccessDenied errors. When S3 returns 403 errors, applications cannot read or write objects — causing broken file uploads, failed downloads, and data pipeline disruptions. Common causes include incorrect bucket policies, overly restrictive public access block settings, missing IAM permissions, or KMS key access issues.

## Applicable Alarms
- CloudWatch metric: AWS/S3 4xxErrors
- Threshold: Elevated 403 error rate for 5 minutes
- Alarm pattern: S3 access denied, 403 errors, 4xx error rate
- Related metrics: AllRequests, GetRequests, PutRequests, FirstByteLatency

## Diagnosis Steps

Retrieve the alarm details to confirm the error pattern and affected bucket.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME</step>

Check the bucket policy for any deny statements or restrictive conditions.
<step>aws s3api get-bucket-policy --bucket BUCKET_NAME</step>

Review the public access block configuration to identify overly restrictive settings.
<step>aws s3api get-public-access-block --bucket BUCKET_NAME</step>

## Remediation Steps

Retrieve the current bucket policy to analyze the access rules.
<step>aws s3api get-bucket-policy --bucket BUCKET_NAME</step>

Check the bucket ACL for any conflicting access control entries.
<step on_success="1">aws s3api get-bucket-acl --bucket BUCKET_NAME</step>

Ensure the public access block is properly configured to prevent unintended public access while allowing authorized access.
<step>aws s3api put-public-access-block --bucket BUCKET_NAME --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true</step>

## Verification

Confirm the alarm has returned to OK state after policy adjustments.
<step>aws cloudwatch describe-alarms --alarm-names ALARM_NAME --query "MetricAlarms[].StateValue"</step>

## Rollback

Restore the original public access block settings if the change disrupted access.
<rollback>aws s3api put-public-access-block --bucket BUCKET_NAME --public-access-block-configuration BlockPublicAcls=ORIGINAL_BLOCK_PUBLIC_ACLS,IgnorePublicAcls=ORIGINAL_IGNORE_PUBLIC_ACLS,BlockPublicPolicy=ORIGINAL_BLOCK_PUBLIC_POLICY,RestrictPublicBuckets=ORIGINAL_RESTRICT_PUBLIC_BUCKETS</rollback>
