#!/bin/bash
set -uo pipefail

# MSP Assistant AgentCore Cleanup Script
# Usage: ./destroy.sh [--region us-east-1] [--force] [--keep-bootstrap]

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

REGION="us-east-1"
FORCE=false
KEEP_BOOTSTRAP=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --region) REGION="$2"; shift 2 ;;
    --force) FORCE=true; shift ;;
    --keep-bootstrap) KEEP_BOOTSTRAP=true; shift ;;
    *) echo "Usage: ./destroy.sh [--region us-east-1] [--force] [--keep-bootstrap]"; exit 1 ;;
  esac
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
CFN_EXEC_ROLE="cdk-hnb659fds-cfn-exec-role-${ACCOUNT_ID}-${REGION}"

echo -e "${RED}╔════════════════════════════════════════════╗${NC}"
echo -e "${RED}║  MSP Assistant → Full Teardown             ║${NC}"
echo -e "${RED}╚════════════════════════════════════════════╝${NC}"
echo ""
echo "Region: $REGION | Account: $ACCOUNT_ID"
echo ""

if [ "$FORCE" != true ]; then
  echo -e "${YELLOW}⚠️  WARNING: This will delete ALL deployed resources!${NC}"
  read -p "Are you sure? (yes/no): " confirm
  [ "$confirm" != "yes" ] && echo "Cancelled." && exit 0
fi

# ─── Helpers ──────────────────────────────────────────────────────────────────

log()  { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
step() { echo -e "\n${YELLOW}[$1] $2${NC}"; }

# Filter AWS CLI "None" output so for-loops iterate zero times on empty results
filter_none() { grep -v '^None$' || true; }

delete_versioned_bucket() {
  local bucket=$1
  aws s3api head-bucket --bucket "$bucket" --region "$REGION" 2>/dev/null || return 0
  echo "  Emptying $bucket..."
  
  # Delete current objects (non-versioned) in batch
  aws s3 rm "s3://$bucket" --recursive --region "$REGION" 2>/dev/null || true
  
  # Delete all versions in batch (up to 1000 per call)
  local versions=$(aws s3api list-object-versions --bucket "$bucket" --region "$REGION" \
    --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null)
  if [ -n "$versions" ] && [ "$versions" != "null" ] && echo "$versions" | jq -e '.Objects | length > 0' >/dev/null 2>&1; then
    echo "    Deleting $(echo "$versions" | jq '.Objects | length') object versions..."
    echo "$versions" | aws s3api delete-objects --bucket "$bucket" --region "$REGION" --delete file:///dev/stdin >/dev/null 2>&1 || true
  fi
  
  # Delete all delete markers in batch (up to 1000 per call)
  local markers=$(aws s3api list-object-versions --bucket "$bucket" --region "$REGION" \
    --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null)
  if [ -n "$markers" ] && [ "$markers" != "null" ] && echo "$markers" | jq -e '.Objects | length > 0' >/dev/null 2>&1; then
    echo "    Deleting $(echo "$markers" | jq '.Objects | length') delete markers..."
    echo "$markers" | aws s3api delete-objects --bucket "$bucket" --region "$REGION" --delete file:///dev/stdin >/dev/null 2>&1 || true
  fi
  
  aws s3api delete-bucket --bucket "$bucket" --region "$REGION" 2>/dev/null && log "Deleted bucket: $bucket" || warn "Bucket $bucket may already be deleted"
}

delete_iam_role() {
  local role=$1
  aws iam get-role --role-name "$role" >/dev/null 2>&1 || return 0
  for arn in $(aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null | filter_none); do
    aws iam detach-role-policy --role-name "$role" --policy-arn "$arn" 2>/dev/null
  done
  for p in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames[]' --output text 2>/dev/null | filter_none); do
    aws iam delete-role-policy --role-name "$role" --policy-name "$p" 2>/dev/null
  done
  for ip in $(aws iam list-instance-profiles-for-role --role-name "$role" --query 'InstanceProfiles[].InstanceProfileName' --output text 2>/dev/null | filter_none); do
    aws iam remove-role-from-instance-profile --instance-profile-name "$ip" --role-name "$role" 2>/dev/null
  done
  aws iam delete-role --role-name "$role" 2>/dev/null && log "Deleted role: $role"
}

delete_vpc() {
  local vpc_id=$1
  aws ec2 describe-vpcs --vpc-ids "$vpc_id" --region "$REGION" >/dev/null 2>&1 || return 0
  echo "  Tearing down VPC $vpc_id..."
  local nat_ids=$(aws ec2 describe-nat-gateways --region "$REGION" \
    --filter "Name=vpc-id,Values=$vpc_id" "Name=state,Values=available,pending" \
    --query 'NatGateways[].NatGatewayId' --output text | filter_none)
  for nat in $nat_ids; do
    aws ec2 delete-nat-gateway --nat-gateway-id "$nat" --region "$REGION" >/dev/null 2>&1
    echo "    Deleting NAT $nat..."
  done
  if [ -n "$nat_ids" ]; then
    echo "    Waiting for NAT gateways (up to 90s)..."
    local elapsed=0
    while [ $elapsed -lt 90 ]; do
      local remaining=$(aws ec2 describe-nat-gateways --region "$REGION" \
        --filter "Name=vpc-id,Values=$vpc_id" "Name=state,Values=available,pending,deleting" \
        --query 'NatGateways[].NatGatewayId' --output text | filter_none)
      [ -z "$remaining" ] && break
      sleep 10; elapsed=$((elapsed + 10))
    done
  fi
  for ep in $(aws ec2 describe-vpc-endpoints --region "$REGION" --filters "Name=vpc-id,Values=$vpc_id" \
    --query 'VpcEndpoints[].VpcEndpointId' --output text | filter_none); do
    aws ec2 delete-vpc-endpoints --vpc-endpoint-ids "$ep" --region "$REGION" >/dev/null 2>&1
  done
  for fl in $(aws ec2 describe-flow-logs --region "$REGION" --filter "Name=resource-id,Values=$vpc_id" \
    --query 'FlowLogs[].FlowLogId' --output text | filter_none); do
    aws ec2 delete-flow-logs --flow-log-ids "$fl" --region "$REGION" >/dev/null 2>&1
  done
  for sg in $(aws ec2 describe-security-groups --region "$REGION" --filters "Name=vpc-id,Values=$vpc_id" \
    --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text | filter_none); do
    for rule in $(aws ec2 describe-security-group-rules --region "$REGION" --filters "Name=group-id,Values=$sg" \
      --query 'SecurityGroupRules[?!IsEgress].SecurityGroupRuleId' --output text | filter_none); do
      aws ec2 revoke-security-group-ingress --group-id "$sg" --security-group-rule-ids "$rule" --region "$REGION" >/dev/null 2>&1
    done
    for rule in $(aws ec2 describe-security-group-rules --region "$REGION" --filters "Name=group-id,Values=$sg" \
      --query 'SecurityGroupRules[?IsEgress].SecurityGroupRuleId' --output text | filter_none); do
      aws ec2 revoke-security-group-egress --group-id "$sg" --security-group-rule-ids "$rule" --region "$REGION" >/dev/null 2>&1
    done
  done
  for sg in $(aws ec2 describe-security-groups --region "$REGION" --filters "Name=vpc-id,Values=$vpc_id" \
    --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text | filter_none); do
    aws ec2 delete-security-group --group-id "$sg" --region "$REGION" 2>/dev/null
  done
  for eni in $(aws ec2 describe-network-interfaces --region "$REGION" --filters "Name=vpc-id,Values=$vpc_id" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output text | filter_none); do
    aws ec2 delete-network-interface --network-interface-id "$eni" --region "$REGION" 2>/dev/null
  done
  for s in $(aws ec2 describe-subnets --region "$REGION" --filters "Name=vpc-id,Values=$vpc_id" \
    --query 'Subnets[].SubnetId' --output text | filter_none); do
    aws ec2 delete-subnet --subnet-id "$s" --region "$REGION" 2>/dev/null
  done
  for rt in $(aws ec2 describe-route-tables --region "$REGION" --filters "Name=vpc-id,Values=$vpc_id" \
    --query 'RouteTables[?Associations[0].Main!=`true`].RouteTableId' --output text | filter_none); do
    for assoc in $(aws ec2 describe-route-tables --region "$REGION" --route-table-ids "$rt" \
      --query 'RouteTables[0].Associations[?!Main].RouteTableAssociationId' --output text | filter_none); do
      aws ec2 disassociate-route-table --association-id "$assoc" --region "$REGION" 2>/dev/null
    done
    aws ec2 delete-route-table --route-table-id "$rt" --region "$REGION" 2>/dev/null
  done
  for igw in $(aws ec2 describe-internet-gateways --region "$REGION" \
    --filters "Name=attachment.vpc-id,Values=$vpc_id" --query 'InternetGateways[].InternetGatewayId' --output text | filter_none); do
    aws ec2 detach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$vpc_id" --region "$REGION" 2>/dev/null
    aws ec2 delete-internet-gateway --internet-gateway-id "$igw" --region "$REGION" 2>/dev/null
  done
  for eip in $(aws ec2 describe-addresses --region "$REGION" \
    --filters "Name=tag:aws:cloudformation:stack-name,Values=MSPAssistant*" \
    --query 'Addresses[].AllocationId' --output text | filter_none); do
    aws ec2 release-address --allocation-id "$eip" --region "$REGION" 2>/dev/null
  done
  aws ec2 delete-vpc --vpc-id "$vpc_id" --region "$REGION" 2>/dev/null && log "Deleted VPC $vpc_id" || warn "VPC $vpc_id may need manual cleanup"
}

delete_cfn_stack() {
  local stack=$1
  local status=$(aws cloudformation describe-stacks --stack-name "$stack" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>&1) || return 0
  echo "  $stack ($status)"
  if ! aws cloudformation delete-stack --stack-name "$stack" --region "$REGION" 2>/dev/null; then
    warn "$stack delete failed (likely missing exec role)"
    return 1
  fi
  local elapsed=0
  while [ $elapsed -lt 120 ]; do
    sleep 10; elapsed=$((elapsed + 10))
    status=$(aws cloudformation describe-stacks --stack-name "$stack" --region "$REGION" \
      --query 'Stacks[0].StackStatus' --output text 2>&1) || { log "$stack deleted"; return 0; }
    [[ "$status" == *"does not exist"* ]] && { log "$stack deleted"; return 0; }
    [ "$status" = "DELETE_FAILED" ] && break
  done
  if [ "$status" = "DELETE_FAILED" ]; then
    local retain=$(aws cloudformation list-stack-resources --stack-name "$stack" --region "$REGION" \
      --query 'StackResourceSummaries[?ResourceStatus!=`DELETE_COMPLETE`].LogicalResourceId' --output text 2>/dev/null | filter_none)
    if [ -n "$retain" ]; then
      aws cloudformation delete-stack --stack-name "$stack" --region "$REGION" --retain-resources $retain 2>/dev/null
      sleep 15
      aws cloudformation describe-stacks --stack-name "$stack" --region "$REGION" >/dev/null 2>&1 || { log "$stack deleted (with retain)"; return 0; }
    fi
    warn "$stack may still exist in DELETE_FAILED state"
  fi
}

delete_secrets_by_prefix() {
  local prefix=$1
  echo "  Deleting secrets with prefix: $prefix"
  local secrets=$(aws secretsmanager list-secrets --region "$REGION" \
    --filters Key=name,Values="${prefix}" \
    --query 'SecretList[].Name' --output text 2>/dev/null | filter_none)
  [ -z "$secrets" ] && echo "    No secrets found" && return 0
  for secret in $secrets; do
    aws secretsmanager delete-secret --secret-id "$secret" --force-delete-without-recovery \
      --region "$REGION" >/dev/null 2>&1 && log "Deleted secret: $secret" || warn "Failed: $secret"
  done
}

delete_cloudwatch_resource_policy() {
  local policy_name=$1
  echo "  Deleting CloudWatch resource policy: $policy_name"
  aws logs delete-resource-policy --policy-name "$policy_name" --region "$REGION" >/dev/null 2>&1 && \
    log "Deleted policy: $policy_name" || echo "    Policy may not exist"
}

delete_dynamodb_table() {
  local table_name=$1
  echo "  Deleting DynamoDB table: $table_name"
  aws dynamodb describe-table --table-name "$table_name" --region "$REGION" >/dev/null 2>&1 || return 0
  aws dynamodb delete-table --table-name "$table_name" --region "$REGION" >/dev/null 2>&1 && \
    log "Deleted table: $table_name" || warn "Failed: $table_name"
}

cleanup_local_files() {
  echo "  Cleaning up local files..."
  [ -f "frontend/.env" ] && rm -f "frontend/.env" && log "Deleted frontend/.env"
  [ -f "infrastructure/cdk/outputs.json" ] && rm -f "infrastructure/cdk/outputs.json" && log "Deleted outputs.json"
  [ -f "/tmp/jira-openapi.json" ] && rm -f "/tmp/jira-openapi.json" && log "Deleted /tmp/jira-openapi.json"
  for runtime_dir in agents/runtime agents/runtime_*; do
    [ -f "$runtime_dir/env_config.txt" ] && rm -f "$runtime_dir/env_config.txt" && log "Deleted $runtime_dir/env_config.txt"
  done
}

# ─── Step 1: Delete AgentCore resources ───────────────────────────────────────

step "1/9" "Deleting AgentCore resources..."

for gw_id in $(aws bedrock-agentcore-control list-gateways --region "$REGION" \
  --query 'items[].gatewayId' --output text 2>/dev/null | filter_none); do
  echo "  Deleting gateway $gw_id targets..."
  for target_id in $(aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier "$gw_id" --region "$REGION" --query 'items[].targetId' --output text 2>/dev/null | filter_none); do
    if ! aws bedrock-agentcore-control delete-gateway-target \
      --gateway-identifier "$gw_id" --target-id "$target_id" --region "$REGION" 2>&1; then
      warn "Failed to delete gateway target: $target_id"
    fi
  done
  
  # Wait for ALL targets to fully delete (critical for runtime deletion)
  echo "  Waiting for targets to delete (up to 120s)..."
  elapsed=0
  while [ $elapsed -lt 120 ]; do
    remaining=$(aws bedrock-agentcore-control list-gateway-targets \
      --gateway-identifier "$gw_id" --region "$REGION" \
      --query 'items[].targetId' --output text 2>/dev/null | filter_none | wc -w | tr -d ' ')
    [ "$remaining" -eq 0 ] && break
    sleep 10; elapsed=$((elapsed + 10))
  done
  
  if [ "$remaining" -ne 0 ]; then
    warn "Gateway targets still exist after 120s, proceeding anyway"
  else
    log "All gateway targets deleted"
  fi
  
  # Delete gateway
  if ! aws bedrock-agentcore-control delete-gateway --gateway-identifier "$gw_id" --region "$REGION" 2>&1; then
    warn "Failed to delete gateway: $gw_id"
  else
    log "Gateway deletion initiated: $gw_id"
  fi
  
  # Wait for gateway to fully delete (critical for runtime deletion)
  echo "  Waiting for gateway to delete (up to 120s)..."
  elapsed=0
  while [ $elapsed -lt 120 ]; do
    gw_status=$(aws bedrock-agentcore-control get-gateway --gateway-identifier "$gw_id" --region "$REGION" \
      --query 'status' --output text 2>&1)
    if [[ "$gw_status" == *"ResourceNotFoundException"* ]] || [[ "$gw_status" == *"does not exist"* ]]; then
      log "Gateway fully deleted: $gw_id"
      break
    fi
    sleep 10; elapsed=$((elapsed + 10))
  done
  
  if [ $elapsed -ge 120 ]; then
    warn "Gateway may still exist after 120s: $gw_id"
  fi
done

for cred_name in $(aws bedrock-agentcore-control list-api-key-credential-providers --region "$REGION" \
  --query 'credentialProviders[].name' --output text 2>/dev/null | filter_none); do
  if ! aws bedrock-agentcore-control delete-api-key-credential-provider --name "$cred_name" --region "$REGION" 2>&1; then
    warn "Failed to delete API key: $cred_name"
  else
    log "Deleted API key: $cred_name"
  fi
done

# Delete Cedar Policy Engine (policies must be deleted before engine)
for engine_id in $(aws bedrock-agentcore-control list-policy-engines --region "$REGION" \
  --query "policyEngines[?name=='msp-policy-engine'].policyEngineId" --output text 2>/dev/null | filter_none); do
  for policy_id in $(aws bedrock-agentcore-control list-policies \
    --policy-engine-id "$engine_id" --region "$REGION" \
    --query "policies[].policyId" --output text 2>/dev/null | filter_none); do
    aws bedrock-agentcore-control delete-policy \
      --policy-engine-id "$engine_id" --policy-id "$policy_id" --region "$REGION" 2>/dev/null && \
      log "Deleted Cedar policy: $policy_id" || warn "Failed to delete Cedar policy: $policy_id"
  done
  aws bedrock-agentcore-control delete-policy-engine \
    --policy-engine-id "$engine_id" --region "$REGION" 2>/dev/null && \
    log "Deleted Cedar policy engine: $engine_id" || warn "Failed to delete Cedar policy engine: $engine_id"
done

# Delete Bedrock Guardrail
for guardrail_id in $(aws bedrock list-guardrails --region "$REGION" \
  --query "guardrails[?name=='msp-ops-topic-guardrail'].id" --output text 2>/dev/null | filter_none); do
  aws bedrock delete-guardrail --guardrail-identifier "$guardrail_id" --region "$REGION" 2>/dev/null && \
    log "Deleted guardrail: $guardrail_id" || warn "Failed to delete guardrail: $guardrail_id"
done

for cred_name in $(aws bedrock-agentcore-control list-oauth2-credential-providers --region "$REGION" \
  --query 'credentialProviders[].name' --output text 2>/dev/null | filter_none); do
  if ! aws bedrock-agentcore-control delete-oauth2-credential-provider --name "$cred_name" --region "$REGION" 2>&1; then
    warn "Failed to delete OAuth2: $cred_name"
  else
    log "Deleted OAuth2: $cred_name"
  fi
done

for mem_id in $(aws bedrock-agentcore-control list-memories --region "$REGION" \
  --query 'memories[].id' --output text 2>/dev/null | filter_none); do
  if ! aws bedrock-agentcore-control delete-memory --memory-id "$mem_id" --region "$REGION" 2>&1; then
    warn "Failed to delete memory: $mem_id"
  fi
done
log "Deleted all memories"

# First attempt: Delete all runtimes
echo "  Deleting runtimes (first attempt)..."
FAILED_RUNTIMES=""
for rt_id in $(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query 'agentRuntimes[].agentRuntimeId' --output text 2>/dev/null | filter_none); do
  DELETE_OUTPUT=$(aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$rt_id" --region "$REGION" 2>&1)
  if [ $? -ne 0 ]; then
    warn "Failed to delete runtime: $rt_id"
    echo "    Error: $DELETE_OUTPUT"
    FAILED_RUNTIMES="$FAILED_RUNTIMES $rt_id"
  else
    log "Deleted runtime $rt_id"
  fi
done

# Wait for successful deletions to propagate
echo "  Waiting for runtime deletions (up to 120s)..."
RT_WAIT=0
while [ $RT_WAIT -lt 120 ]; do
  RT_REMAINING=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query 'agentRuntimes[].agentRuntimeId' --output text 2>/dev/null | filter_none | wc -w | tr -d ' ')
  [ "$RT_REMAINING" -eq 0 ] && break
  sleep 10; RT_WAIT=$((RT_WAIT + 10))
done

# Retry failed runtimes if any remain
if [ "$RT_REMAINING" -gt 0 ]; then
  echo ""
  echo "  $RT_REMAINING runtimes still exist, retrying with extended wait..."
  echo "  (Gateway dependencies may need more time to fully clear)"
  
  # Additional wait for gateway dependencies to clear
  sleep 30
  
  # Retry deletion for remaining runtimes
  for rt_id in $(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query 'agentRuntimes[].agentRuntimeId' --output text 2>/dev/null | filter_none); do
    echo "  Retrying runtime: $rt_id"
    DELETE_OUTPUT=$(aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$rt_id" --region "$REGION" 2>&1)
    if [ $? -ne 0 ]; then
      warn "Retry failed for runtime: $rt_id"
      echo "    Error: $DELETE_OUTPUT"
    else
      log "Successfully deleted runtime on retry: $rt_id"
    fi
  done
  
  # Final wait after retry
  echo "  Waiting for retry deletions (up to 120s)..."
  RT_WAIT=0
  while [ $RT_WAIT -lt 120 ]; do
    RT_REMAINING=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
      --query 'agentRuntimes[].agentRuntimeId' --output text 2>/dev/null | filter_none | wc -w | tr -d ' ')
    [ "$RT_REMAINING" -eq 0 ] && break
    sleep 10; RT_WAIT=$((RT_WAIT + 10))
  done
  
  if [ "$RT_REMAINING" -gt 0 ]; then
    warn "$RT_REMAINING runtimes still exist after retry"
    echo "  Remaining runtime IDs:"
    aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
      --query 'agentRuntimes[].agentRuntimeId' --output text 2>/dev/null | filter_none | tr '\t' '\n' | sed 's/^/    /'
  else
    log "All runtimes successfully deleted after retry"
  fi
fi

# ─── Step 2: Delete ECS ───────────────────────────────────────────────────────

step "2/9" "Deleting ECS resources..."

for cluster_arn in $(aws ecs list-clusters --region "$REGION" \
  --query 'clusterArns[?contains(@, `MSP`) || contains(@, `Backend`)]' --output text | filter_none); do
  for svc_arn in $(aws ecs list-services --cluster "$cluster_arn" --region "$REGION" \
    --query 'serviceArns[]' --output text 2>/dev/null | filter_none); do
    aws ecs update-service --cluster "$cluster_arn" --service "$svc_arn" --desired-count 0 --region "$REGION" >/dev/null 2>&1 || true
    aws ecs delete-service --cluster "$cluster_arn" --service "$svc_arn" --force --region "$REGION" >/dev/null 2>&1 || true
  done
  aws ecs delete-cluster --cluster "$cluster_arn" --region "$REGION" >/dev/null 2>&1 || true
  log "Deleted ECS cluster"
done

echo "  Waiting for ECS tasks to drain (30s)..."
sleep 30

# ─── Step 3: Delete ALB, Target Groups, API Gateway ──────────────────────────

step "3/9" "Deleting ALB, Target Groups, API Gateway..."

for alb_arn in $(aws elbv2 describe-load-balancers --region "$REGION" \
  --query 'LoadBalancers[?contains(LoadBalancerName, `MSP`) || contains(LoadBalancerName, `Backe`)].LoadBalancerArn' --output text | filter_none); do
  for listener in $(aws elbv2 describe-listeners --load-balancer-arn "$alb_arn" --region "$REGION" \
    --query 'Listeners[].ListenerArn' --output text 2>/dev/null | filter_none); do
    aws elbv2 delete-listener --listener-arn "$listener" --region "$REGION" 2>/dev/null
  done
  aws elbv2 delete-load-balancer --load-balancer-arn "$alb_arn" --region "$REGION" 2>/dev/null
  log "Deleted ALB"
done

for tg in $(aws elbv2 describe-target-groups --region "$REGION" \
  --query 'TargetGroups[?contains(TargetGroupName, `MSP`) || contains(TargetGroupName, `Backe`)].TargetGroupArn' --output text | filter_none); do
  aws elbv2 delete-target-group --target-group-arn "$tg" --region "$REGION" 2>/dev/null
done

for api_id in $(aws apigateway get-rest-apis --region "$REGION" \
  --query 'items[?contains(name, `msp`) || contains(name, `MSP`) || contains(name, `Backend`)].id' --output text | filter_none); do
  aws apigateway delete-rest-api --rest-api-id "$api_id" --region "$REGION" 2>/dev/null
  log "Deleted API Gateway $api_id"
done

echo "  Waiting for ALB to drain (45s)..."
sleep 45

# ─── Step 4: Delete S3 buckets ───────────────────────────────────────────────

step "4/9" "Deleting S3 buckets..."

for bucket in $(aws s3 ls | awk '{print $3}' | grep -iE 'mspassistant|msp-assistant|bedrock-agentcore-codebuild-sources-'"$ACCOUNT_ID"'-'"$REGION"''); do
  delete_versioned_bucket "$bucket"
done

# ─── Step 5: Delete ECR, Lambda, CloudWatch, Cognito, DynamoDB ───────────────

step "5/9" "Deleting ECR, Lambda, CloudWatch, Cognito, DynamoDB..."

for repo in $(aws ecr describe-repositories --region "$REGION" \
  --query 'repositories[?contains(repositoryName, `msp`)].repositoryName' --output text 2>/dev/null | filter_none); do
  aws ecr delete-repository --repository-name "$repo" --force --region "$REGION" >/dev/null 2>&1 && log "Deleted ECR: $repo"
done

for fn in $(aws lambda list-functions --region "$REGION" \
  --query 'Functions[?contains(FunctionName, `MSPAssistant`)].FunctionName' --output text | filter_none); do
  aws lambda delete-function --function-name "$fn" --region "$REGION" 2>/dev/null
  log "Deleted Lambda $fn"
done

for lg in $(aws logs describe-log-groups --region "$REGION" \
  --query 'logGroups[?contains(logGroupName, `MSP`) || contains(logGroupName, `msp`) || contains(logGroupName, `bedrock-agentcore`)].logGroupName' --output text | filter_none); do
  aws logs delete-log-group --log-group-name "$lg" --region "$REGION" 2>/dev/null
done
log "Deleted CloudWatch Log Groups"

for policy_name in $(aws logs describe-resource-policies --region "$REGION" \
  --query 'resourcePolicies[?contains(policyName, `TransactionSearch`)].policyName' --output text 2>/dev/null | filter_none); do
  delete_cloudwatch_resource_policy "$policy_name"
done

for pool_id in $(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
  --query 'UserPools[?contains(Name, `msp`)].Id' --output text | filter_none); do
  domain=$(aws cognito-idp describe-user-pool --user-pool-id "$pool_id" --region "$REGION" \
    --query 'UserPool.Domain' --output text 2>/dev/null | filter_none)
  [ -n "$domain" ] && aws cognito-idp delete-user-pool-domain --user-pool-id "$pool_id" --domain "$domain" --region "$REGION" 2>/dev/null
  aws cognito-idp delete-user-pool --user-pool-id "$pool_id" --region "$REGION" 2>/dev/null
  log "Deleted Cognito pool $pool_id"
done

for table in $(aws dynamodb list-tables --region "$REGION" \
  --query 'TableNames[?contains(@, `msp-assistant`)]' --output text 2>/dev/null | filter_none); do
  delete_dynamodb_table "$table"
done

# ─── Step 5.5: Delete Secrets ─────────────────────────────────────────────────

step "5.5/9" "Deleting Secrets Manager secrets..."
delete_secrets_by_prefix "msp-credentials/"

# ─── Step 6: Delete VPC ──────────────────────────────────────────────────────

step "6/9" "Deleting VPC..."

for vpc_id in $(aws ec2 describe-vpcs --region "$REGION" \
  --filters "Name=tag:aws:cloudformation:stack-name,Values=MSPAssistant*" \
  --query 'Vpcs[].VpcId' --output text | filter_none); do
  delete_vpc "$vpc_id"
done

# ─── Step 7: Delete CloudFormation stacks ─────────────────────────────────────

step "7/9" "Deleting CloudFormation stacks..."

if ! aws iam get-role --role-name "$CFN_EXEC_ROLE" >/dev/null 2>&1; then
  warn "CFN exec role missing — recreating..."
  aws iam create-role --role-name "$CFN_EXEC_ROLE" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"cloudformation.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null 2>&1
  aws iam attach-role-policy --role-name "$CFN_EXEC_ROLE" \
    --policy-arn arn:aws:iam::aws:policy/AdministratorAccess 2>/dev/null
  echo "  Waiting 15s for IAM propagation..."
  sleep 15
  log "CFN exec role recreated"
fi

# Discover and delete stacks in dependency order (Frontend → Backend → AgentCore → CDKToolkit)
ALL_STACKS=$(aws cloudformation list-stacks --region "$REGION" \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
  --query 'StackSummaries[?contains(StackName, `MSPAssistant`)].StackName' --output text | filter_none)

# Delete Frontend first (imports from Backend)
for stack in $ALL_STACKS; do
  case $stack in
    *Frontend*) 
      delete_cfn_stack "$stack"
      # Wait for Frontend to fully delete before proceeding
      echo "  Waiting for Frontend stack deletion to complete..."
      WAIT_COUNT=0
      while [ $WAIT_COUNT -lt 60 ]; do
        STACK_STATUS=$(aws cloudformation describe-stacks --stack-name "$stack" --region "$REGION" \
          --query 'Stacks[0].StackStatus' --output text 2>&1)
        [[ "$STACK_STATUS" == *"does not exist"* ]] && break
        sleep 10; WAIT_COUNT=$((WAIT_COUNT + 10))
      done
      ;;
  esac
done

# Delete Backend second
for stack in $ALL_STACKS; do
  case $stack in
    *Backend*) delete_cfn_stack "$stack" ;;
  esac
done

# Delete AgentCore third
for stack in $ALL_STACKS; do
  case $stack in
    *AgentCore*) delete_cfn_stack "$stack" ;;
  esac
done

# Delete CDKToolkit last (after MSP stacks, before IAM cleanup)
if [ "$KEEP_BOOTSTRAP" = false ]; then
  delete_cfn_stack "CDKToolkit"
  # Wait for CDKToolkit to delete its resources (S3, ECR, IAM roles)
  echo "  Waiting for CDKToolkit deletion to complete..."
  WAIT_COUNT=0
  while [ $WAIT_COUNT -lt 60 ]; do
    STACK_STATUS=$(aws cloudformation describe-stacks --stack-name "CDKToolkit" --region "$REGION" \
      --query 'Stacks[0].StackStatus' --output text 2>&1)
    [[ "$STACK_STATUS" == *"does not exist"* ]] && break
    sleep 10; WAIT_COUNT=$((WAIT_COUNT + 10))
  done
else
  echo "  Skipping CDKToolkit (--keep-bootstrap)"
fi

# ─── Step 8: Delete IAM roles (cleanup any orphaned roles) ───────────────────

step "8/9" "Deleting IAM roles..."

for role in $(aws iam list-roles --query 'Roles[?contains(RoleName, `MSPAssistant`)].RoleName' --output text | filter_none); do
  delete_iam_role "$role"
done

for role in $(aws iam list-roles --query 'Roles[?contains(RoleName, `msp-gateway`)].RoleName' --output text | filter_none); do
  delete_iam_role "$role"
done

# Clean up any remaining CDK roles (should be none if CDKToolkit deleted successfully)
for role in $(aws iam list-roles --query 'Roles[?contains(RoleName, `cdk-hnb659fds`)].RoleName' --output text | filter_none); do
  delete_iam_role "$role"
done

# ─── Step 9: Final cleanup ───────────────────────────────────────────────────

step "9/9" "Final cleanup..."

# Clean up CDK assets bucket and ECR repo if they still exist (fallback)
for bucket in $(aws s3 ls | awk '{print $3}' | grep -E "cdk-hnb659fds-assets-${ACCOUNT_ID}-${REGION}"); do
  delete_versioned_bucket "$bucket"
done

for repo in $(aws ecr describe-repositories --region "$REGION" \
  --query 'repositories[?contains(repositoryName, `cdk-hnb659fds-container-assets`)].repositoryName' --output text 2>/dev/null | filter_none); do
  aws ecr delete-repository --repository-name "$repo" --force --region "$REGION" >/dev/null 2>&1 && log "Deleted CDK ECR: $repo"
done

cleanup_local_files

# ─── Verification ────────────────────────────────────────────────────────────

echo ""
echo -e "${YELLOW}=== Verification ===${NC}"

CLEAN=true
check() {
  local label=$1 result=$2
  if [ -z "$result" ]; then
    echo -e "  ${GREEN}✓${NC} $label: None"
  else
    echo -e "  ${RED}✗${NC} $label: $result"
    CLEAN=false
  fi
}

check "CloudFormation" "$(aws cloudformation list-stacks --region "$REGION" \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_FAILED UPDATE_ROLLBACK_COMPLETE \
  --query 'StackSummaries[?contains(StackName, `MSP`) || contains(StackName, `CDKToolkit`)].StackName' --output text 2>/dev/null)"

check "S3 Buckets" "$(aws s3 ls 2>/dev/null | grep -iE 'msp|cdk-hnb659fds|bedrock-agentcore' || true)"

check "IAM Roles" "$(aws iam list-roles \
  --query 'Roles[?contains(RoleName, `MSP`) || contains(RoleName, `cdk-hnb659fds`) || contains(RoleName, `msp-gateway`)].RoleName' --output text 2>/dev/null)"

check "Log Groups" "$(aws logs describe-log-groups --region "$REGION" \
  --query 'logGroups[?contains(logGroupName, `MSP`) || contains(logGroupName, `msp`)].logGroupName' --output text 2>/dev/null)"

check "ECR" "$(aws ecr describe-repositories --region "$REGION" \
  --query 'repositories[?contains(repositoryName, `msp`)].repositoryName' --output text 2>/dev/null)"

check "ECS" "$(aws ecs list-clusters --region "$REGION" \
  --query 'clusterArns[?contains(@, `MSP`) || contains(@, `Backend`)]' --output text 2>/dev/null)"

check "AgentCore Gateways" "$(aws bedrock-agentcore-control list-gateways --region "$REGION" \
  --query 'items[].gatewayId' --output text 2>/dev/null)"

check "AgentCore Memories" "$(aws bedrock-agentcore-control list-memories --region "$REGION" \
  --query 'memories[].id' --output text 2>/dev/null)"

check "AgentCore Runtimes" "$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query 'agentRuntimes[].agentRuntimeId' --output text 2>/dev/null)"

check "AgentCore API Key Creds" "$(aws bedrock-agentcore-control list-api-key-credential-providers --region "$REGION" \
  --query 'credentialProviders[].name' --output text 2>/dev/null)"

check "Cedar Policy Engines" "$(aws bedrock-agentcore-control list-policy-engines --region "$REGION" \
  --query "policyEngines[?name=='msp-policy-engine'].policyEngineId" --output text 2>/dev/null)"

check "Bedrock Guardrails" "$(aws bedrock list-guardrails --region "$REGION" \
  --query "guardrails[?name=='msp-ops-topic-guardrail'].id" --output text 2>/dev/null)"

check "AgentCore OAuth2 Creds" "$(aws bedrock-agentcore-control list-oauth2-credential-providers --region "$REGION" \
  --query 'credentialProviders[].name' --output text 2>/dev/null)"

check "Secrets Manager" "$(aws secretsmanager list-secrets --region "$REGION" \
  --filters Key=name,Values=msp-credentials/ \
  --query 'SecretList[].Name' --output text 2>/dev/null)"

check "DynamoDB Tables" "$(aws dynamodb list-tables --region "$REGION" \
  --query 'TableNames[?contains(@, `msp-assistant`)]' --output text 2>/dev/null)"

check "CloudWatch Policies" "$(aws logs describe-resource-policies --region "$REGION" \
  --query 'resourcePolicies[?contains(policyName, `TransactionSearch`)].policyName' --output text 2>/dev/null)"

check "Local Files" "$(ls -1 frontend/.env infrastructure/cdk/outputs.json /tmp/jira-openapi.json agents/runtime*/env_config.txt 2>/dev/null || true)"

echo ""
if [ "$CLEAN" = true ]; then
  echo -e "${GREEN}╔════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}║         Cleanup Complete! ✓                ║${NC}"
  echo -e "${GREEN}╚════════════════════════════════════════════╝${NC}"
  echo ""
  echo "All MSP Assistant resources removed from $REGION."
else
  echo -e "${YELLOW}╔════════════════════════════════════════════╗${NC}"
  echo -e "${YELLOW}║  Cleanup mostly complete — check above     ║${NC}"
  echo -e "${YELLOW}╚════════════════════════════════════════════╝${NC}"
fi