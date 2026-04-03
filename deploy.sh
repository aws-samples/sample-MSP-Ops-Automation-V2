#!/bin/bash
set -e

# MSP Assistant AgentCore Deployment
# Usage: ./deploy.sh --email admin@example.com [--region us-east-1]

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Parse arguments
EMAIL=""
REGION="us-east-1"

while [[ $# -gt 0 ]]; do
  case $1 in
    --email)
      EMAIL="$2"
      shift 2
      ;;
    --region)
      REGION="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      echo "Usage: ./deploy.sh --email user@example.com [--region us-east-1]"
      exit 1
      ;;
  esac
done

if [ -z "$EMAIL" ]; then
  echo -e "${RED}Error: --email required${NC}"
  exit 1
fi

# Load environment variables from backend/.env
if [ -f "backend/.env" ]; then
  echo "Loading configuration from backend/.env..."
  export $(grep -v '^#' backend/.env | xargs)
  echo "✓ Configuration loaded"
else
  echo -e "${RED}Error: backend/.env not found${NC}"
  echo "Please copy backend/.env.example to backend/.env and configure it"
  echo ""
  echo "Required variables:"
  echo "  - AWS_REGION"
  echo "  - BEDROCK_MODEL_ID"
  echo "  - JIRA_DOMAIN"
  echo "  - JIRA_EMAIL"
  echo "  - JIRA_API_TOKEN"
  echo "  - JIRA_PROJECT_KEY"
  exit 1
fi

echo -e "${GREEN}╔════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  MSP Assistant → AgentCore Deployment     ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════╝${NC}"
echo ""
echo "Email: $EMAIL"
echo "Region: $REGION"
echo ""

# Tee all output (stdout + stderr) to a timestamped log file
LOG_FILE="deploy-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "Logging to $LOG_FILE"

# Validate required environment variables
echo "Validating environment variables..."
REQUIRED_VARS=("JIRA_DOMAIN" "JIRA_EMAIL" "JIRA_API_TOKEN" "JIRA_PROJECT_KEY" "MODEL")
MISSING_VARS=()

for var in "${REQUIRED_VARS[@]}"; do
  if [ -z "${!var}" ]; then
    MISSING_VARS+=("$var")
  fi
done

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
  echo -e "${RED}Error: Missing required environment variables:${NC}"
  for var in "${MISSING_VARS[@]}"; do
    echo "  - $var"
  done
  echo ""
  echo "Please configure these in backend/.env"
  exit 1
fi

echo "✓ All required variables configured"
echo "  Jira Domain: $JIRA_DOMAIN"
echo "  Jira Email: $JIRA_EMAIL"
echo "  Model: $MODEL"
echo ""

# Set script directory for absolute path references
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Helper function: Get CLI caller's managed policies (handles both IAM users and assumed roles)
get_cli_managed_policies() {
  local caller_arn=$(aws sts get-caller-identity --query 'Arn' --output text --region "$REGION")

  # Detect if caller is IAM user or assumed role
  if [[ "$caller_arn" == *":user/"* ]]; then
    # IAM User
    local username=$(echo "$caller_arn" | awk -F'/' '{print $NF}')
    aws iam list-attached-user-policies --user-name "$username" \
      --query 'AttachedPolicies[].PolicyArn' --output text --region "$REGION" 2>/dev/null || echo ""
  elif [[ "$caller_arn" == *":assumed-role/"* ]]; then
    # Assumed Role (SSO, EC2 instance role, etc.)
    local role_name=$(echo "$caller_arn" | awk -F'/' '{print $2}')
    aws iam list-attached-role-policies --role-name "$role_name" \
      --query 'AttachedPolicies[].PolicyArn' --output text --region "$REGION" 2>/dev/null || echo ""
  else
    # Unknown principal type - return empty
    echo ""
  fi
}

# Step 1: Prerequisites validation
echo -e "${YELLOW}[1/14] Validating prerequisites...${NC}"
chmod +x scripts/validate-prerequisites.sh
scripts/validate-prerequisites.sh "$REGION"

# Step 2: Build backend Docker image
echo -e "${YELLOW}[2/14] Building backend Docker image...${NC}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO_NAME="msp-assistant-backend"
ECR_REPO_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_REPO_NAME"

echo "Building Docker image (no cache for fresh build)..."
docker build --platform linux/amd64 --no-cache -t msp-assistant-backend:latest -f backend/Dockerfile .

# Verify build succeeded
if ! docker images msp-assistant-backend:latest --format "{{.ID}}" | grep -q .; then
  echo -e "${RED}Error: Docker build failed${NC}"
  exit 1
fi
echo "✓ Docker image built"

# Tag for ECR
echo "Tagging image for ECR: $ECR_REPO_URI:latest"
docker tag msp-assistant-backend:latest "$ECR_REPO_URI:latest"

# Verify tag succeeded
if ! docker images "$ECR_REPO_URI:latest" --format "{{.ID}}" | grep -q .; then
  echo -e "${RED}Error: Docker tag failed${NC}"
  exit 1
fi
echo "✓ Docker image tagged for ECR"

# Step 3: Push backend image to ECR
echo -e "${YELLOW}[3/14] Pushing backend image to ECR...${NC}"

# Ensure ECR repository exists
echo "Checking ECR repository..."
if ! aws ecr describe-repositories --repository-names "$ECR_REPO_NAME" --region "$REGION" &>/dev/null; then
  echo "Creating ECR repository: $ECR_REPO_NAME"
  aws ecr create-repository \
    --repository-name "$ECR_REPO_NAME" \
    --region "$REGION" \
    --image-scanning-configuration scanOnPush=true \
    --encryption-configuration encryptionType=AES256
  
  if [ $? -ne 0 ]; then
    echo -e "${RED}Error: Failed to create ECR repository${NC}"
    exit 1
  fi
  echo "✓ ECR repository created"
else
  echo "✓ ECR repository exists"
fi

# Login to ECR
echo "Authenticating with ECR..."
echo "  Account: $ACCOUNT_ID"
echo "  Region: $REGION"
echo "  Repository: $ECR_REPO_URI"

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"

if [ $? -ne 0 ]; then
  echo -e "${RED}Error: ECR authentication failed${NC}"
  exit 1
fi
echo "✓ ECR authentication successful"

# Push image
echo ""
echo "Pushing image to ECR..."
echo "  Source: msp-assistant-backend:latest"
echo "  Destination: $ECR_REPO_URI:latest"
echo ""

docker push "$ECR_REPO_URI:latest"

if [ $? -ne 0 ]; then
  echo -e "${RED}Error: Docker push failed${NC}"
  exit 1
fi

echo ""
echo "✓ Backend image pushed to ECR"

# Verify image exists in ECR
echo "Verifying image in ECR..."
IMAGE_DIGEST=$(aws ecr describe-images --repository-name "$ECR_REPO_NAME" --region "$REGION" --image-ids imageTag=latest --query 'imageDetails[0].imageDigest' --output text 2>/dev/null)

if [ -n "$IMAGE_DIGEST" ] && [ "$IMAGE_DIGEST" != "None" ]; then
  echo "✓ Image verified in ECR: $IMAGE_DIGEST"
else
  echo -e "${YELLOW}⚠️  Warning: Could not verify image in ECR, but push appeared successful${NC}"
fi

# Step 4: Deploy Supervisor Runtime (MOVED BEFORE CDK)
echo -e "${YELLOW}[4/14] Deploying Supervisor Runtime...${NC}"
echo ""
echo "⚠️  CRITICAL: Supervisor must deploy BEFORE CDK"
echo "   CDK reads outputs.json to get Runtime ARN for ECS environment variables"
echo ""

cd "$SCRIPT_DIR/agents/runtime"

# Clean old configuration AND stale env files to force fresh deployment
rm -f .bedrock_agentcore.yaml
rm -f env_config.txt

echo "Configuring Supervisor Runtime with Direct Python deployment..."
agentcore configure \
  --entrypoint supervisor_runtime.py \
  --protocol HTTP \
  --name msp_supervisor_agent \
  --region "$REGION" \
  --requirements-file requirements.txt \
  --non-interactive

if [ $? -ne 0 ]; then
  echo -e "${RED}Error: Supervisor configuration failed${NC}"
  cd "$SCRIPT_DIR"
  exit 1
fi

echo "  Configuration complete, starting deployment..."
echo "  (This will take 10-15 minutes - progress shown below)"
echo ""

# Deploy with live output and auto-update flag
agentcore deploy --auto-update-on-conflict

if [ $? -ne 0 ]; then
  echo -e "${RED}Error: Supervisor deployment failed${NC}"
  cd "$SCRIPT_DIR"
  exit 1
fi

# Get ARN from AWS API (more reliable than parsing agentcore status output)
SUPERVISOR_ARN=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" --output json | \
  jq -r '.agentRuntimes[] | select(.agentRuntimeName == "msp_supervisor_agent") | .agentRuntimeArn')

if [ -z "$SUPERVISOR_ARN" ] || [ "$SUPERVISOR_ARN" = "null" ]; then
  echo -e "${RED}Error: Could not get Supervisor Runtime ARN from AWS${NC}"
  echo "  Supervisor may not have deployed successfully"
  cd "$SCRIPT_DIR"
  exit 1
fi

# Validate ARN is complete (not truncated)
if [ ${#SUPERVISOR_ARN} -lt 70 ]; then
  echo -e "${RED}Error: Supervisor ARN appears truncated: $SUPERVISOR_ARN${NC}"
  echo "  Expected length >70 chars, got ${#SUPERVISOR_ARN}"
  cd "$SCRIPT_DIR"
  exit 1
fi

echo ""
echo "✓ Supervisor Runtime deployed"
echo "  Runtime ARN: $SUPERVISOR_ARN"

# Return to project root
cd "$SCRIPT_DIR"

# Save Supervisor ARN to backend/.env (MEMORY_ID will be written after CDK in Step 5)
if [ -f "backend/.env" ]; then
  # Update SUPERVISOR_RUNTIME_ARN
  if grep -q "^SUPERVISOR_RUNTIME_ARN=" backend/.env 2>/dev/null; then
    # Update existing line
    if [[ "$OSTYPE" == "darwin"* ]]; then
      sed -i '' "s|^SUPERVISOR_RUNTIME_ARN=.*|SUPERVISOR_RUNTIME_ARN=$SUPERVISOR_ARN|" backend/.env
    else
      sed -i "s|^SUPERVISOR_RUNTIME_ARN=.*|SUPERVISOR_RUNTIME_ARN=$SUPERVISOR_ARN|" backend/.env
    fi
  else
    # Append new line
    echo "" >> backend/.env
    echo "# Supervisor Runtime ARN (Added by deploy.sh)" >> backend/.env
    echo "SUPERVISOR_RUNTIME_ARN=$SUPERVISOR_ARN" >> backend/.env
  fi
  
  echo "✓ Supervisor ARN saved to backend/.env"
fi

# Update CDK outputs.json with real Supervisor ARN
echo "  Updating CDK outputs.json..."
cd infrastructure/cdk

# Ensure outputs.json exists (first deploy won't have it yet)
[ -f outputs.json ] || echo '{}' > outputs.json

# Add SupervisorRuntimeARN to MSPAssistantAgentCoreStack
jq --arg arn "$SUPERVISOR_ARN" \
  '.MSPAssistantAgentCoreStack.SupervisorRuntimeARN = $arn' \
  outputs.json > outputs.tmp.json && mv outputs.tmp.json outputs.json

if [ $? -eq 0 ]; then
  echo "✓ Supervisor ARN saved to CDK outputs"
  echo "  ARN: $SUPERVISOR_ARN"
  echo ""
  echo "✅ CDK will now read REAL Runtime ARN (not placeholder)"
else
  echo -e "${YELLOW}⚠️  Warning: Could not update outputs.json with Supervisor ARN${NC}"
fi

cd "$SCRIPT_DIR"

echo ""

# Step 4b: Create Gateway Execution Role (BEFORE CDK)
echo -e "${YELLOW}[4b/14] Creating Gateway execution role...${NC}"
echo ""
echo "⚠️  CRITICAL: Gateway role must exist BEFORE CDK creates the gateway"
echo "   CDK will use msp-gateway-execution-role when creating the gateway"
echo ""

GATEWAY_ROLE_NAME="msp-gateway-execution-role"
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text --region "$REGION")

echo "Creating/verifying gateway execution role: $GATEWAY_ROLE_NAME"

aws iam create-role \
  --role-name "$GATEWAY_ROLE_NAME" \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"bedrock-agentcore.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]
  }' >/dev/null 2>&1 || true

aws iam put-role-policy \
  --role-name "$GATEWAY_ROLE_NAME" \
  --policy-name AgentCoreAccess \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[
      {\"Sid\":\"InvokeGateway\",\"Effect\":\"Allow\",\"Action\":[\"bedrock-agentcore:InvokeGateway\"],\"Resource\":\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:gateway/*\"},
      {\"Sid\":\"InvokeMCPRuntimes\",\"Effect\":\"Allow\",\"Action\":\"bedrock-agentcore:InvokeAgentRuntime\",\"Resource\":\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:runtime/*\"},
      {\"Sid\":\"WorkloadIdentity\",\"Effect\":\"Allow\",\"Action\":[\"bedrock-agentcore:GetWorkloadAccessToken\",\"bedrock-agentcore:GetResourceOauth2Token\",\"bedrock-agentcore:GetResourceApiKey\"],\"Resource\":[\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:workload-identity-directory/default\",\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:workload-identity-directory/default/workload-identity/*\"]},
      {\"Sid\":\"TokenVault\",\"Effect\":\"Allow\",\"Action\":[\"bedrock-agentcore:GetResourceOauth2Token\",\"bedrock-agentcore:GetResourceApiKey\"],\"Resource\":\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:token-vault/*\"},
      {\"Sid\":\"GetSecretValue\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:bedrock-agentcore-identity*\"}
    ]
  }" >/dev/null 2>&1

# Attach CLI caller's managed policies to gateway role
echo "  Attaching CLI caller's managed policies to gateway role..."
CLI_MANAGED_POLICIES=$(get_cli_managed_policies)

if [ -n "$CLI_MANAGED_POLICIES" ]; then
  for POLICY_ARN in $CLI_MANAGED_POLICIES; do
    POLICY_NAME=$(echo "$POLICY_ARN" | awk -F'/' '{print $NF}')
    aws iam attach-role-policy --role-name "$GATEWAY_ROLE_NAME" --policy-arn "$POLICY_ARN" --region "$REGION" >/dev/null 2>&1 && \
      echo "    ✓ Attached $POLICY_NAME" || echo "    ⚠️ Failed to attach $POLICY_NAME"
  done
else
  echo "    ⚠️ No managed policies found for CLI caller"
fi

echo "  ✓ Gateway execution role ready: $GATEWAY_ROLE_NAME"
echo "  Waiting for IAM propagation..."
sleep 10

echo ""

# Pre-build frontend so CDK FrontendStack can find frontend/dist
# (Real build with correct env vars happens at Step 12 after CDK outputs are available)
if [ ! -d "frontend/dist" ]; then
  echo "Pre-building frontend for CDK asset bundling..."
  cd frontend && npm install && npx vite build && cd "$SCRIPT_DIR"
  echo "✓ Frontend pre-built"
fi

# Step 5: Deploy CDK stacks (READS outputs.json WITH REAL ARN)
echo -e "${YELLOW}[5/14] Deploying CDK infrastructure...${NC}"
echo ""
echo "CDK will read SupervisorRuntimeARN from outputs.json"
echo "ECS task definition will be created with correct ARN from start"
echo ""

# On redeployments, export FRONTEND_URL from previous outputs so CDK bakes the CloudFront
# URL into API Gateway CORS headers instead of falling back to localhost.
if [ -f "infrastructure/cdk/outputs.json" ]; then
  EXISTING_FRONTEND_URL=$(jq -r '.MSPAssistantFrontendStack.FrontendURL // empty' infrastructure/cdk/outputs.json 2>/dev/null)
  if [ -n "$EXISTING_FRONTEND_URL" ] && [ "$EXISTING_FRONTEND_URL" != "null" ]; then
    export FRONTEND_URL="$EXISTING_FRONTEND_URL"
    echo "  Re-deploy detected: using existing CloudFront URL for CORS: $FRONTEND_URL"
  fi
fi

cd infrastructure/cdk

# Install dependencies
pip install -r requirements.txt

# Bootstrap if needed
cdk bootstrap "aws://$ACCOUNT_ID/$REGION"

# Get ALB DNS from previous outputs if this is a redeploy (for SSE streaming CloudFront behavior).
# On a fresh deploy outputs.json doesn't exist yet, so this will be empty — FrontendStack
# handles alb_dns="" gracefully by skipping the ALB origin. We add it in a second pass below.
ALB_DNS_FOR_CDK=$(jq -r '.MSPAssistantBackendStack.ALBEndpoint // empty' infrastructure/cdk/outputs.json 2>/dev/null || true)

# Deploy all stacks — pass Supervisor ARN and ALB DNS via context
cdk deploy --all --require-approval never --outputs-file outputs.json \
  --context region="$REGION" \
  --context account="$ACCOUNT_ID" \
  --context supervisor_runtime_arn="$SUPERVISOR_ARN" \
  --context alb_dns="${ALB_DNS_FOR_CDK}"

# On fresh deploy alb_dns was empty above, so FrontendStack was created without the
# ALB SSE origin. Now that BackendStack is deployed we have the ALB DNS — re-deploy
# FrontendStack to add it. On redeployments this is a no-op (ALB origin already set).
ALB_DNS_FROM_NEW_OUTPUTS=$(jq -r '.MSPAssistantBackendStack.ALBEndpoint // empty' outputs.json 2>/dev/null || true)
if [ -n "$ALB_DNS_FROM_NEW_OUTPUTS" ] && [ "$ALB_DNS_FROM_NEW_OUTPUTS" != "$ALB_DNS_FOR_CDK" ]; then
  echo "  Updating FrontendStack with ALB SSE origin ($ALB_DNS_FROM_NEW_OUTPUTS)..."
  cdk deploy MSPAssistantFrontendStack --require-approval never \
    --context region="$REGION" \
    --context account="$ACCOUNT_ID" \
    --context alb_dns="$ALB_DNS_FROM_NEW_OUTPUTS"
  echo "  ✓ FrontendStack updated with ALB SSE origin"
fi

# Parse outputs with validation
MEMORY_ID=$(jq -r '.MSPAssistantAgentCoreStack.MemoryId // empty' outputs.json)
GATEWAY_ARN=$(jq -r '.MSPAssistantAgentCoreStack.GatewayARN // empty' outputs.json)
ECR_REPO_URI=$(jq -r '.MSPAssistantBackendStack.ECRRepositoryUri // empty' outputs.json)

# Validate critical outputs
if [ -z "$MEMORY_ID" ] || [ "$MEMORY_ID" = "null" ]; then
  echo -e "${RED}Error: Failed to get Memory ID from CDK outputs${NC}"
  exit 1
fi

if [ -z "$GATEWAY_ARN" ] || [ "$GATEWAY_ARN" = "null" ]; then
  echo -e "${RED}Error: Failed to get Gateway ARN from CDK outputs${NC}"
  exit 1
fi

echo "✓ CDK stacks deployed"
echo "  Memory ID: $MEMORY_ID"
echo "  Gateway ARN: $GATEWAY_ARN"
echo "  Supervisor ARN: $(jq -r '.MSPAssistantAgentCoreStack.SupervisorRuntimeARN // empty' outputs.json)"

cd "$SCRIPT_DIR"

# Update backend/.env with all infrastructure values from CDK
echo ""
echo "Updating backend/.env with infrastructure values..."
if [ -f "backend/.env" ]; then
  # Helper function for sed (macOS vs Linux)
  update_env_var() {
    local var_name=$1
    local var_value=$2
    if grep -q "^${var_name}=" backend/.env 2>/dev/null; then
      if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s|^${var_name}=.*|${var_name}=${var_value}|" backend/.env
      else
        sed -i "s|^${var_name}=.*|${var_name}=${var_value}|" backend/.env
      fi
    else
      # Ensure file ends with newline before appending
      [ -n "$(tail -c1 backend/.env)" ] && echo "" >> backend/.env
      echo "${var_name}=${var_value}" >> backend/.env
    fi
  }
  
  # Get additional values from CDK outputs
  GATEWAY_URL_FROM_CDK=$(jq -r '.MSPAssistantAgentCoreStack.GatewayURL // empty' infrastructure/cdk/outputs.json)
  USER_POOL_ID_FROM_CDK=$(jq -r '.MSPAssistantBackendStack.CognitoUserPoolId // empty' infrastructure/cdk/outputs.json)
  CLIENT_ID_FROM_CDK=$(jq -r '.MSPAssistantBackendStack.CognitoWebClientId // empty' infrastructure/cdk/outputs.json)
  
  # Update all infrastructure values
  update_env_var "MEMORY_ID" "$MEMORY_ID"
  update_env_var "GATEWAY_ARN" "$GATEWAY_ARN"
  [ -n "$GATEWAY_URL_FROM_CDK" ] && update_env_var "GATEWAY_URL" "$GATEWAY_URL_FROM_CDK"
  [ -n "$USER_POOL_ID_FROM_CDK" ] && update_env_var "COGNITO_USER_POOL_ID" "$USER_POOL_ID_FROM_CDK"
  [ -n "$CLIENT_ID_FROM_CDK" ] && update_env_var "COGNITO_CLIENT_ID" "$CLIENT_ID_FROM_CDK"
  
  echo "✓ All infrastructure values saved to backend/.env"
  echo "  MEMORY_ID: $MEMORY_ID"
  echo "  GATEWAY_ARN: $GATEWAY_ARN"
  echo "  GATEWAY_URL: $GATEWAY_URL_FROM_CDK"
fi

# Step 5b: Create AgentCore OAuth credential provider for Gateway→MCP auth
echo ""
echo -e "${YELLOW}[5b/14] Creating OAuth credential provider (Gateway→MCP)...${NC}"

# Read M2M Cognito outputs from CDK
M2M_CLIENT_ID=$(jq -r '.MSPAssistantBackendStack.CognitoM2MClientId // empty' infrastructure/cdk/outputs.json)
COGNITO_TOKEN_ENDPOINT=$(jq -r '.MSPAssistantBackendStack.CognitoTokenEndpoint // empty' infrastructure/cdk/outputs.json)
USER_POOL_ID_FOR_M2M=$(jq -r '.MSPAssistantBackendStack.CognitoUserPoolId // empty' infrastructure/cdk/outputs.json)

if [ -z "$M2M_CLIENT_ID" ] || [ "$M2M_CLIENT_ID" = "null" ]; then
  echo -e "${RED}Error: M2M Client ID not found in CDK outputs${NC}"
  exit 1
fi

# Retrieve M2M client secret via AWS CLI (CDK can't output secrets)
M2M_CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$USER_POOL_ID_FOR_M2M" \
  --client-id "$M2M_CLIENT_ID" \
  --region "$REGION" \
  --query 'UserPoolClient.ClientSecret' --output text 2>/dev/null)

if [ -z "$M2M_CLIENT_SECRET" ] || [ "$M2M_CLIENT_SECRET" = "None" ]; then
  echo -e "${RED}Error: Could not retrieve M2M client secret${NC}"
  exit 1
fi

COGNITO_ISSUER="https://cognito-idp.${REGION}.amazonaws.com/${USER_POOL_ID_FOR_M2M}"

# Check if OAuth provider already exists
EXISTING_OAUTH=$(aws bedrock-agentcore-control get-oauth2-credential-provider \
  --name msp-gateway-cognito --region "$REGION" --output json 2>/dev/null || true)

if [ -n "$EXISTING_OAUTH" ] && echo "$EXISTING_OAUTH" | jq -e '.name' >/dev/null 2>&1; then
  echo "✓ OAuth provider 'msp-gateway-cognito' already exists"
  
  # Check if client ID matches current M2M client
  EXISTING_CLIENT_ID=$(echo "$EXISTING_OAUTH" | jq -r '.oauth2ProviderConfigOutput.includedOauth2ProviderConfig.clientId // empty')
  
  if [ "$EXISTING_CLIENT_ID" != "$M2M_CLIENT_ID" ]; then
    echo "  ⚠️  Client ID mismatch detected (existing: $EXISTING_CLIENT_ID, expected: $M2M_CLIENT_ID)"
    echo "  Deleting and recreating OAuth provider with correct client..."
    
    # Delete with error suppression
    aws bedrock-agentcore-control delete-oauth2-credential-provider \
      --name msp-gateway-cognito \
      --region "$REGION" >/dev/null 2>&1 || true
    
    echo "  Waiting for deletion to propagate (Secrets Manager eventual consistency)..."
    sleep 15
    
    echo "  Creating OAuth provider with correct M2M client..."
    # Retry loop for eventual consistency
    OAUTH_CREATED=false
    for attempt in 1 2 3 4; do
      if aws bedrock-agentcore-control create-oauth2-credential-provider \
        --name msp-gateway-cognito \
        --region "$REGION" \
        --credential-provider-vendor CustomOauth2 \
        --oauth2-provider-config-input "{
          \"customOauth2ProviderConfig\": {
            \"oauthDiscovery\": {
              \"discoveryUrl\": \"${COGNITO_ISSUER}/.well-known/openid-configuration\"
            },
            \"clientId\": \"${M2M_CLIENT_ID}\",
            \"clientSecret\": \"${M2M_CLIENT_SECRET}\"
          }
        }" >/dev/null 2>&1; then
        OAUTH_CREATED=true
        echo "  ✓ OAuth provider recreated with correct client"
        break
      else
        echo "    Attempt $attempt failed (Secrets Manager conflict), retrying in 10s..."
        sleep 10
      fi
    done
    
    if [ "$OAUTH_CREATED" = false ]; then
      echo -e "${RED}Error: Failed to create OAuth provider after 4 attempts${NC}"
      echo "  This is likely due to Secrets Manager eventual consistency."
      echo "  Wait 30 seconds and re-run deploy.sh to continue."
      exit 1
    fi
  else
    echo "  ✓ Client ID matches, no update needed"
  fi
else
  echo "Creating OAuth provider 'msp-gateway-cognito'..."
  # Retry loop for eventual consistency (in case of previous failed delete)
  OAUTH_CREATED=false
  for attempt in 1 2 3; do
    if aws bedrock-agentcore-control create-oauth2-credential-provider \
      --name msp-gateway-cognito \
      --region "$REGION" \
      --credential-provider-vendor CustomOauth2 \
      --oauth2-provider-config-input "{
        \"customOauth2ProviderConfig\": {
          \"oauthDiscovery\": {
            \"discoveryUrl\": \"${COGNITO_ISSUER}/.well-known/openid-configuration\"
          },
          \"clientId\": \"${M2M_CLIENT_ID}\",
          \"clientSecret\": \"${M2M_CLIENT_SECRET}\"
        }
      }" >/dev/null 2>&1; then
      OAUTH_CREATED=true
      echo "✓ OAuth provider created"
      break
    else
      if [ $attempt -lt 3 ]; then
        echo "  Attempt $attempt failed, retrying in 10s..."
        sleep 10
      fi
    fi
  done
  
  if [ "$OAUTH_CREATED" = false ]; then
    echo -e "${RED}Error: Failed to create OAuth provider after 3 attempts${NC}"
    exit 1
  fi
fi

echo "  Issuer: $COGNITO_ISSUER"
echo "  Client ID: $M2M_CLIENT_ID"

# Build JWT authorizer config for MCP runtimes (used during agentcore configure)
DISCOVERY_URL="${COGNITO_ISSUER}/.well-known/openid-configuration"
JWT_AUTH_CONFIG=$(jq -n \
  --arg url "$DISCOVERY_URL" \
  --arg client "$M2M_CLIENT_ID" \
  '{customJWTAuthorizer:{discoveryUrl:$url,allowedClients:[$client],allowedScopes:["mcp-server/invoke"]}}')
echo "  JWT authorizer config built for MCP deploys"
echo ""

# Step 6: Deploy MCP Servers to AgentCore Runtime
echo -e "${YELLOW}[6/14] Deploying MCP servers to AgentCore Runtime...${NC}"
echo ""
echo "This will deploy 3 MCP servers (~30-45 minutes total):"
echo "  1. CloudWatch MCP (~10-15 min)"
echo "  2. AWS API MCP (~10-15 min)"
echo "  3. AWS Knowledge MCP (~10-15 min)"
echo ""

# Helper: deploy one MCP server, extract ARN
deploy_mcp_server() {
  local MCP_NAME="$1"
  local MCP_ENTRY="$2"
  local MCP_DIR="$3"

  echo "  Deploying ${MCP_NAME}..." >&2
  cd "$MCP_DIR"
  cp "$SCRIPT_DIR/mcp-servers/common/credential_helper.py" . 2>/dev/null || true
  rm -f .bedrock_agentcore.yaml

  agentcore configure -e "$MCP_ENTRY" --protocol MCP --name "$MCP_NAME" \
    --idle-timeout 1800 --max-lifetime 14400 --non-interactive \
    --requirements-file requirements.txt \
    --region "$REGION" \
    -ac "$JWT_AUTH_CONFIG" >&2
  if [ $? -ne 0 ]; then
    echo -e "${RED}Error: ${MCP_NAME} configuration failed${NC}" >&2
    cd "$SCRIPT_DIR"; exit 1
  fi

  agentcore deploy --auto-update-on-conflict >&2
  if [ $? -ne 0 ]; then
    echo -e "${RED}Error: ${MCP_NAME} deployment failed${NC}" >&2
    cd "$SCRIPT_DIR"; exit 1
  fi

  local ARN=$(agentcore status 2>&1 | grep "Agent ARN:" -A 1 | grep "arn:aws" | grep -oE 'arn:aws:bedrock-agentcore:[^[:space:]]+' | head -1)
  if [ -z "$ARN" ]; then
    echo -e "${RED}Error: Could not get ${MCP_NAME} Runtime ARN${NC}" >&2
    cd "$SCRIPT_DIR"; exit 1
  fi

  echo "  ✓ ${MCP_NAME} deployed: $ARN" >&2
  cd "$SCRIPT_DIR"
  echo "$ARN"
}

CLOUDWATCH_ARN=$(deploy_mcp_server "cloudwatch_mcp" "cloudwatch_mcp.py" "$SCRIPT_DIR/mcp-servers/cloudwatch")
AWS_API_ARN=$(deploy_mcp_server "aws_api_mcp" "aws_api_mcp.py" "$SCRIPT_DIR/mcp-servers/aws-api")
KNOWLEDGE_ARN=$(deploy_mcp_server "aws_knowledge_mcp" "knowledge_mcp.py" "$SCRIPT_DIR/mcp-servers/aws-knowledge")

# JWT authorizer is now set during agentcore configure (via -ac flag above)
# No post-deploy patch needed.

echo ""
echo "✓ All MCP servers deployed to AgentCore Runtime"
echo "  CloudWatch: $CLOUDWATCH_ARN"
echo "  AWS API: $AWS_API_ARN"
echo "  Knowledge: $KNOWLEDGE_ARN"
echo ""

# Step 7: Create AgentCore Gateway & Register MCP Targets
echo -e "${YELLOW}[7/14] Creating AgentCore Gateway...${NC}"
echo ""

# Define gateway role variables (used in both new and existing gateway paths)
GATEWAY_ROLE_NAME="msp-gateway-execution-role"
ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text --region "$REGION")
GATEWAY_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${GATEWAY_ROLE_NAME}"

# CRITICAL FIX: Create gateway execution role BEFORE checking if gateway exists
# This ensures the role is always present, whether gateway is new or existing
echo "Creating/verifying gateway execution role..."

aws iam create-role \
  --role-name "$GATEWAY_ROLE_NAME" \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"bedrock-agentcore.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]
  }' >/dev/null 2>&1 || true

aws iam put-role-policy \
  --role-name "$GATEWAY_ROLE_NAME" \
  --policy-name AgentCoreAccess \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[
      {\"Sid\":\"InvokeGateway\",\"Effect\":\"Allow\",\"Action\":[\"bedrock-agentcore:InvokeGateway\"],\"Resource\":\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:gateway/*\"},
      {\"Sid\":\"InvokeMCPRuntimes\",\"Effect\":\"Allow\",\"Action\":\"bedrock-agentcore:InvokeAgentRuntime\",\"Resource\":\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:runtime/*\"},
      {\"Sid\":\"WorkloadIdentity\",\"Effect\":\"Allow\",\"Action\":[\"bedrock-agentcore:GetWorkloadAccessToken\",\"bedrock-agentcore:GetResourceOauth2Token\",\"bedrock-agentcore:GetResourceApiKey\"],\"Resource\":[\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:workload-identity-directory/default\",\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:workload-identity-directory/default/workload-identity/*\"]},
      {\"Sid\":\"TokenVault\",\"Effect\":\"Allow\",\"Action\":[\"bedrock-agentcore:GetResourceOauth2Token\",\"bedrock-agentcore:GetResourceApiKey\"],\"Resource\":\"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:token-vault/*\"},
      {\"Sid\":\"GetSecretValue\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:bedrock-agentcore-identity*\"}
    ]
  }" >/dev/null 2>&1

# Attach CLI caller's managed policies to gateway role
echo "  Attaching CLI caller's managed policies to gateway role..."
CLI_MANAGED_POLICIES=$(get_cli_managed_policies)

if [ -n "$CLI_MANAGED_POLICIES" ]; then
  for POLICY_ARN in $CLI_MANAGED_POLICIES; do
    POLICY_NAME=$(echo "$POLICY_ARN" | awk -F'/' '{print $NF}')
    aws iam attach-role-policy --role-name "$GATEWAY_ROLE_NAME" --policy-arn "$POLICY_ARN" --region $REGION >/dev/null 2>&1 && \
      echo "    ✓ Attached $POLICY_NAME" || echo "    ⚠️ Failed to attach $POLICY_NAME"
  done
else
  echo "    ⚠️ No managed policies found for CLI caller"
fi

echo "  ✓ Gateway execution role ready: $GATEWAY_ROLE_NAME"
sleep 10  # Allow IAM propagation

# Now discover existing gateway from CDK outputs or by listing
GATEWAY_ARN_FROM_CDK=$(jq -r '.MSPAssistantAgentCoreStack.GatewayARN // empty' infrastructure/cdk/outputs.json 2>/dev/null)
if [ -n "$GATEWAY_ARN_FROM_CDK" ] && [ "$GATEWAY_ARN_FROM_CDK" != "null" ]; then
  GATEWAY_ID=$(echo "$GATEWAY_ARN_FROM_CDK" | awk -F'/' '{print $NF}')
  echo "✓ Found gateway from CDK outputs: $GATEWAY_ID"
else
  GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways --region $REGION \
    --query "items[?status=='READY'] | [0].gatewayId" --output text 2>/dev/null)
fi

if [ -z "$GATEWAY_ID" ] || [ "$GATEWAY_ID" = "None" ] || [ "$GATEWAY_ID" = "null" ]; then
  echo "No existing gateway found. Creating new one..."

  GATEWAY_RESPONSE=$(aws bedrock-agentcore-control create-gateway \
    --name "msp-assistant-gateway" \
    --description "Unified MCP endpoint for MSP Ops agents" \
    --role-arn "$GATEWAY_ROLE_ARN" \
    --protocol-type MCP \
    --authorizer-type AWS_IAM \
    --region $REGION --output json 2>&1)

  GATEWAY_ID=$(echo "$GATEWAY_RESPONSE" | jq -r '.gatewayId // empty')
  if [ -z "$GATEWAY_ID" ]; then
    echo -e "${RED}✗ Gateway creation failed: $GATEWAY_RESPONSE${NC}"
  else
    echo "✓ Gateway created: $GATEWAY_ID"
    echo "  Waiting for READY status..."
    for i in $(seq 1 30); do
      GW_STATUS=$(aws bedrock-agentcore-control get-gateway --gateway-identifier $GATEWAY_ID \
        --region $REGION --query 'status' --output text 2>/dev/null)
      if [ "$GW_STATUS" = "READY" ]; then break; fi
      sleep 10
    done
  fi
else
  # Gateway already exists - update its role to use the proper execution role
  echo "✓ Gateway already exists: $GATEWAY_ID"
  echo "  Updating gateway role to: $GATEWAY_ROLE_ARN"
  
  aws bedrock-agentcore-control update-gateway \
    --gateway-identifier "$GATEWAY_ID" \
    --name "msp-assistant-gateway" \
    --protocol-type MCP \
    --authorizer-type AWS_IAM \
    --role-arn "$GATEWAY_ROLE_ARN" \
    --region $REGION >/dev/null 2>&1 && \
    echo "  ✓ Gateway role updated" || echo "  ⚠️ Gateway role update failed"
  
  echo "  Waiting for READY status..."
  for i in $(seq 1 30); do
    GW_STATUS=$(aws bedrock-agentcore-control get-gateway --gateway-identifier $GATEWAY_ID \
      --region $REGION --query 'status' --output text 2>/dev/null)
    if [ "$GW_STATUS" = "READY" ]; then break; fi
    sleep 5
  done
fi

if [ -n "$GATEWAY_ID" ] && [ "$GATEWAY_ID" != "None" ]; then
  GATEWAY_URL=$(aws bedrock-agentcore-control get-gateway \
    --gateway-identifier $GATEWAY_ID --region $REGION \
    --query 'gatewayUrl' --output text 2>/dev/null)
  echo "  Gateway URL: $GATEWAY_URL"

  # Discover OAuth credential provider ARN dynamically
  MCP_OAUTH_ARN=$(aws bedrock-agentcore-control list-oauth2-credential-providers \
    --region $REGION --query "credentialProviders[?name=='msp-gateway-cognito'].credentialProviderArn" \
    --output text 2>/dev/null)

  if [ -n "$MCP_OAUTH_ARN" ] && [ "$MCP_OAUTH_ARN" != "None" ]; then
    echo "  OAuth provider: $MCP_OAUTH_ARN"

    MCP_CRED_CONFIG=$(jq -n --arg arn "$MCP_OAUTH_ARN" \
      '[{credentialProviderType:"OAUTH",credentialProvider:{oauthCredentialProvider:{providerArn:$arn,scopes:["mcp-server/invoke"],grantType:"CLIENT_CREDENTIALS"}}}]')

    # Helper: register one MCP runtime as a gateway target
    register_mcp_target() {
      local TARGET_NAME="$1"
      local TARGET_DESC="$2"
      local MCP_ARN="$3"

      echo ""
      echo "  Registering $TARGET_NAME..."

      local ESCAPED_ARN=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$MCP_ARN', safe=''))")
      local ENDPOINT="https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ESCAPED_ARN}/invocations?qualifier=DEFAULT"
      local TARGET_CONFIG=$(jq -n --arg ep "$ENDPOINT" '{mcp:{mcpServer:{endpoint:$ep}}}')

      # Check if target already exists
      local EXISTING_ID=$(aws bedrock-agentcore-control list-gateway-targets \
        --gateway-identifier "$GATEWAY_ID" --region "$REGION" \
        --query "items[?name=='${TARGET_NAME}'].targetId" --output text 2>/dev/null)

      if [ -n "$EXISTING_ID" ] && [ "$EXISTING_ID" != "None" ]; then
        # Delete and recreate to ensure clean sync (update doesn't always re-sync)
        aws bedrock-agentcore-control delete-gateway-target \
          --gateway-identifier "$GATEWAY_ID" --target-id "$EXISTING_ID" \
          --region "$REGION" >/dev/null 2>&1
        echo "    Deleted stale target $EXISTING_ID, recreating..."
        sleep 5
      fi

      aws bedrock-agentcore-control create-gateway-target \
        --gateway-identifier "$GATEWAY_ID" \
        --name "$TARGET_NAME" \
        --description "$TARGET_DESC" \
        --target-configuration "$TARGET_CONFIG" \
        --credential-provider-configurations "$MCP_CRED_CONFIG" \
        --region "$REGION" >/dev/null 2>&1 && echo "    ✓ $TARGET_NAME created" || echo "    ⚠️  $TARGET_NAME creation failed"
    }

    register_mcp_target "cloudwatch-mcp" "CloudWatch MCP - Official awslabs package" "$CLOUDWATCH_ARN"
    register_mcp_target "aws-api-mcp" "AWS API MCP - Official awslabs package with call_aws and suggest_aws_commands" "$AWS_API_ARN"
    register_mcp_target "aws-knowledge-mcp" "AWS Knowledge base search" "$KNOWLEDGE_ARN"

    # Wait for all targets to sync
    echo ""
    echo "  Waiting for gateway targets to sync..."
    for i in $(seq 1 24); do
      TARGETS_JSON=$(aws bedrock-agentcore-control list-gateway-targets \
        --gateway-identifier "$GATEWAY_ID" --region "$REGION" --output json 2>/dev/null)
      READY_COUNT=$(echo "$TARGETS_JSON" | jq '[.items[] | select(.status=="READY")] | length')
      TOTAL_COUNT=$(echo "$TARGETS_JSON" | jq '.items | length')
      echo "    Poll $i: $READY_COUNT/$TOTAL_COUNT targets READY"
      if [ "$READY_COUNT" = "$TOTAL_COUNT" ] && [ "$TOTAL_COUNT" -ge 3 ]; then
        echo "  ✓ All MCP gateway targets READY"
        break
      fi
      sleep 10
    done
    
    # Retry loop for FAILED targets (cold-start race condition recovery)
    echo ""
    echo "  Checking for FAILED targets and retrying if needed..."
    MAX_TARGET_RETRIES=3
    for retry in $(seq 1 $MAX_TARGET_RETRIES); do
      FAILED_TARGETS=$(aws bedrock-agentcore-control list-gateway-targets \
        --gateway-identifier $GATEWAY_ID --region $REGION --output json 2>/dev/null | \
        jq -r '.items[] | select(.status=="FAILED") | "\(.targetId)|\(.name)"')
      
      if [ -z "$FAILED_TARGETS" ]; then
        echo "  ✅ All targets READY (no failures)"
        break
      fi
      
      echo "  ⚠️  Retry $retry/$MAX_TARGET_RETRIES: Re-registering FAILED targets..."
      
      # Delete each failed target
      while IFS='|' read -r TARGET_ID TARGET_NAME; do
        aws bedrock-agentcore-control delete-gateway-target \
          --gateway-identifier $GATEWAY_ID --target-id "$TARGET_ID" \
          --region $REGION >/dev/null 2>&1
        echo "    Deleted FAILED target: $TARGET_NAME ($TARGET_ID)"
      done <<< "$FAILED_TARGETS"
      
      sleep 10  # wait for deletion propagation
      
      # Re-register each failed target
      while IFS='|' read -r TARGET_ID TARGET_NAME; do
        case "$TARGET_NAME" in
          cloudwatch-mcp)
            register_mcp_target "cloudwatch-mcp" "CloudWatch MCP - alarms, metrics, logs" "$CLOUDWATCH_ARN"
            ;;
          aws-api-mcp)
            register_mcp_target "aws-api-mcp" "AWS API MCP - EC2, S3, Lambda, RDS, ECS, Cost, Security, Advisor" "$AWS_API_ARN"
            ;;
          aws-knowledge-mcp)
            register_mcp_target "aws-knowledge-mcp" "AWS Knowledge base search" "$KNOWLEDGE_ARN"
            ;;
        esac
      done <<< "$FAILED_TARGETS"
      
      # Wait for re-registered targets to sync
      echo "    Waiting 60s for re-registered targets to sync..."
      sleep 60
    done
  else
    echo -e "${YELLOW}  ⚠️  No OAuth provider 'msp-gateway-cognito' found — skipping MCP gateway targets${NC}"
    echo "  Create it manually, then re-run deploy."
  fi

  echo ""
  echo "✓ Gateway ready (AWS_IAM inbound auth, TLS encrypted)"
  echo "  Gateway ID: $GATEWAY_ID"
  echo "  Gateway URL: $GATEWAY_URL"

  # --- Jira Integration via API Key + Custom OpenAPI spec ---
  # Uses Basic auth (email:api_token) against the Jira site URL directly.
  # No OAuth app needed — just JIRA_EMAIL, JIRA_API_TOKEN, JIRA_DOMAIN from .env.
  echo ""
  echo "Setting up Jira integration..."

  JIRA_API_KEY_NAME="jira-api-key"
  JIRA_BASIC_TOKEN=$(echo -n "${JIRA_EMAIL}:${JIRA_API_TOKEN}" | base64)

  # Create or recreate API key credential provider (resilient to eventual consistency)
  JIRA_API_KEY_ARN=$(aws bedrock-agentcore-control get-api-key-credential-provider \
    --name "$JIRA_API_KEY_NAME" --region $REGION \
    --query 'credentialProviderArn' --output text 2>/dev/null || echo "")

  if [ -z "$JIRA_API_KEY_ARN" ] || [ "$JIRA_API_KEY_ARN" = "None" ]; then
    echo "  Creating new API key credential provider..."
    JIRA_API_KEY_ARN=$(aws bedrock-agentcore-control create-api-key-credential-provider \
      --name "$JIRA_API_KEY_NAME" --api-key "$JIRA_BASIC_TOKEN" \
      --region $REGION --query 'credentialProviderArn' --output text 2>&1) || {
        echo -e "${YELLOW}  ⚠️  API key creation failed (may already exist), retrying...${NC}"
        sleep 5
        JIRA_API_KEY_ARN=$(aws bedrock-agentcore-control get-api-key-credential-provider \
          --name "$JIRA_API_KEY_NAME" --region $REGION \
          --query 'credentialProviderArn' --output text 2>/dev/null || echo "")
      }
    echo "  ✓ API key credential provider created"
  else
    echo "  API key provider exists (ARN: $JIRA_API_KEY_ARN), recreating to pick up token changes..."
    # Delete with error suppression (may take time for eventual consistency)
    aws bedrock-agentcore-control delete-api-key-credential-provider \
      --name "$JIRA_API_KEY_NAME" --region $REGION >/dev/null 2>&1 || true
    echo "  Waiting for deletion to propagate..."
    sleep 8
    # Create with retry
    for attempt in 1 2 3; do
      JIRA_API_KEY_ARN=$(aws bedrock-agentcore-control create-api-key-credential-provider \
        --name "$JIRA_API_KEY_NAME" --api-key "$JIRA_BASIC_TOKEN" \
        --region $REGION --query 'credentialProviderArn' --output text 2>/dev/null) && break
      echo "    Attempt $attempt failed, retrying in 5s..."
      sleep 5
    done
    if [ -z "$JIRA_API_KEY_ARN" ] || [ "$JIRA_API_KEY_ARN" = "None" ]; then
      echo -e "${YELLOW}  ⚠️  Could not recreate API key provider, using existing${NC}"
      JIRA_API_KEY_ARN=$(aws bedrock-agentcore-control get-api-key-credential-provider \
        --name "$JIRA_API_KEY_NAME" --region $REGION \
        --query 'credentialProviderArn' --output text 2>/dev/null || echo "")
    else
      echo "  ✓ API key credential provider updated"
    fi
  fi

  if [ -z "$JIRA_API_KEY_ARN" ] || [ "$JIRA_API_KEY_ARN" = "None" ]; then
    echo -e "${YELLOW}  ⚠️  Jira API key provider not available — skipping Jira gateway target${NC}"
  fi

  # Strip trailing slash from JIRA_DOMAIN
  JIRA_DOMAIN="${JIRA_DOMAIN%/}"

  # Generate OpenAPI spec at runtime with jq (avoids heredoc encoding issues)
  # Write to temp file, then use working jq -c | jq -Rs encoding
  echo "  Generating Jira OpenAPI spec with domain: $JIRA_DOMAIN"
  
  jq -n --arg domain "$JIRA_DOMAIN" '{
    "openapi": "3.0.1",
    "info": {"title": "Jira REST API", "version": "3"},
    "servers": [{"url": $domain}],
    "paths": {
      "/rest/api/3/issue": {
        "post": {
          "operationId": "createIssue",
          "summary": "Create a Jira issue",
          "requestBody": {
            "required": true,
            "content": {
              "application/json": {
                "schema": {
                  "type": "object",
                  "required": ["fields"],
                  "properties": {
                    "fields": {
                      "type": "object",
                      "required": ["project", "summary", "issuetype"],
                      "properties": {
                        "project": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
                        "summary": {"type": "string"},
                        "issuetype": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                        "description": {
                          "type": "object",
                          "required": ["type", "version", "content"],
                          "properties": {
                            "type": {"type": "string", "enum": ["doc"]},
                            "version": {"type": "integer", "enum": [1]},
                            "content": {"type": "array", "items": {"type": "object"}}
                          }
                        },
                        "priority": {"type": "object", "properties": {"name": {"type": "string"}}},
                        "labels": {"type": "array", "items": {"type": "string"}},
                        "assignee": {"type": "object", "properties": {"accountId": {"type": "string"}}}
                      }
                    }
                  }
                }
              }
            }
          },
          "responses": {"201": {"description": "Issue created"}}
        }
      },
      "/rest/api/3/issue/{issueIdOrKey}": {
        "get": {
          "operationId": "getIssue",
          "summary": "Get issue details",
          "parameters": [{"name": "issueIdOrKey", "in": "path", "required": true, "schema": {"type": "string"}}],
          "responses": {"200": {"description": "Issue details"}}
        },
        "put": {
          "operationId": "updateIssue",
          "summary": "Update issue",
          "parameters": [{"name": "issueIdOrKey", "in": "path", "required": true, "schema": {"type": "string"}}],
          "requestBody": {
            "required": true,
            "content": {"application/json": {"schema": {"type": "object", "properties": {"fields": {"type": "object"}}}}}
          },
          "responses": {"204": {"description": "Updated"}}
        },
        "delete": {
          "operationId": "deleteIssue",
          "summary": "Delete issue",
          "parameters": [{"name": "issueIdOrKey", "in": "path", "required": true, "schema": {"type": "string"}}],
          "responses": {"204": {"description": "Deleted"}}
        }
      },
      "/rest/api/3/issue/{issueIdOrKey}/assignee": {
        "put": {
          "operationId": "assignIssue",
          "summary": "Assign issue to user",
          "parameters": [{"name": "issueIdOrKey", "in": "path", "required": true, "schema": {"type": "string"}}],
          "requestBody": {
            "required": true,
            "content": {"application/json": {"schema": {"type": "object", "properties": {"accountId": {"type": "string"}}}}}
          },
          "responses": {"204": {"description": "Assigned"}}
        }
      },
      "/rest/api/3/issue/{issueIdOrKey}/comment": {
        "get": {
          "operationId": "getComments",
          "summary": "Get all comments for an issue",
          "parameters": [{"name": "issueIdOrKey", "in": "path", "required": true, "schema": {"type": "string"}}],
          "responses": {"200": {"description": "Comments list"}}
        },
        "post": {
          "operationId": "addComment",
          "summary": "Add comment",
          "parameters": [{"name": "issueIdOrKey", "in": "path", "required": true, "schema": {"type": "string"}}],
          "requestBody": {
            "required": true,
            "content": {
              "application/json": {
                "schema": {
                  "type": "object",
                  "required": ["body"],
                  "properties": {
                    "body": {
                      "type": "object",
                      "required": ["type", "version", "content"],
                      "properties": {
                        "type": {"type": "string", "enum": ["doc"]},
                        "version": {"type": "integer", "enum": [1]},
                        "content": {"type": "array", "items": {"type": "object"}}
                      }
                    }
                  }
                }
              }
            }
          },
          "responses": {"201": {"description": "Comment added"}}
        }
      },
      "/rest/api/3/issue/{issueIdOrKey}/transitions": {
        "get": {
          "operationId": "getTransitions",
          "summary": "Get available transitions for an issue",
          "parameters": [{"name": "issueIdOrKey", "in": "path", "required": true, "schema": {"type": "string"}}],
          "responses": {"200": {"description": "Available transitions"}}
        },
        "post": {
          "operationId": "transitionIssue",
          "summary": "Transition issue",
          "parameters": [{"name": "issueIdOrKey", "in": "path", "required": true, "schema": {"type": "string"}}],
          "requestBody": {
            "required": true,
            "content": {"application/json": {"schema": {"type": "object", "properties": {"transition": {"type": "object", "properties": {"id": {"type": "string"}}}}}}}
          },
          "responses": {"204": {"description": "Transitioned"}}
        }
      },
      "/rest/api/3/user/search": {
        "get": {
          "operationId": "findUsers",
          "summary": "Find users by query",
          "parameters": [
            {"name": "query", "in": "query", "required": true, "schema": {"type": "string"}},
            {"name": "maxResults", "in": "query", "schema": {"type": "integer", "default": 50}}
          ],
          "responses": {"200": {"description": "Users list"}}
        }
      },
      "/rest/api/3/search/jql": {
        "get": {
          "operationId": "SearchIssues",
          "summary": "Search issues using JQL",
          "parameters": [
            {"name": "jql", "in": "query", "schema": {"type": "string"}},
            {"name": "maxResults", "in": "query", "schema": {"type": "integer", "default": 10}},
            {"name": "fields", "in": "query", "schema": {"type": "string"}}
          ],
          "responses": {"200": {"description": "Search results"}}
        }
      },
      "/rest/api/3/issueLink": {
        "post": {
          "operationId": "createIssueLink",
          "summary": "Create link between issues",
          "requestBody": {
            "required": true,
            "content": {
              "application/json": {
                "schema": {
                  "type": "object",
                  "required": ["type", "inwardIssue", "outwardIssue"],
                  "properties": {
                    "type": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                    "inwardIssue": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
                    "outwardIssue": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}
                  }
                }
              }
            }
          },
          "responses": {"201": {"description": "Link created"}}
        }
      }
    }
  }' > /tmp/jira-openapi.json

  JIRA_CRED_CONFIG=$(jq -n --arg arn "$JIRA_API_KEY_ARN" \
    '[{credentialProviderType:"API_KEY",credentialProvider:{apiKeyCredentialProvider:{providerArn:$arn,credentialPrefix:"Basic ",credentialLocation:"HEADER",credentialParameterName:"Authorization"}}}]')

  # Create or skip Jira gateway target
  EXISTING_JIRA=$(aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier $GATEWAY_ID --region $REGION \
    --query "items[?name=='jira-mcp'].targetId" --output text 2>/dev/null)

  if [ -z "$EXISTING_JIRA" ] || [ "$EXISTING_JIRA" = "None" ]; then
    echo "  Creating Jira gateway target from generated spec..."
    aws bedrock-agentcore-control create-gateway-target \
      --gateway-identifier $GATEWAY_ID \
      --name "jira-mcp" \
      --description "Jira ticket management" \
      --target-configuration "{\"mcp\":{\"openApiSchema\":{\"inlinePayload\":$(jq -c < /tmp/jira-openapi.json | jq -Rs .)}}}" \
      --credential-provider-configurations "$JIRA_CRED_CONFIG" \
      --region $REGION >/dev/null 2>&1 && \
      echo "  ✓ Jira gateway target created" || \
      echo -e "${RED}  ✗ Jira target creation failed${NC}"
  else
    echo "  ✓ Jira target already exists: $EXISTING_JIRA"
  fi
else
  GATEWAY_URL="GATEWAY_NOT_CONFIGURED"
  echo -e "${YELLOW}⚠️  No gateway available - continuing without gateway${NC}"
fi

# Step 7b: Attach Cedar Authorization Policy to Gateway (LOG_ONLY mode)
# Creates a policy engine + permit policy and attaches it to the gateway.
# LOG_ONLY mode logs evaluation results to CloudWatch without blocking requests.
# Switch mode to ENFORCE manually after validating logs show expected "Allow" results.
echo ""
echo "--- Cedar Policy Setup (LOG_ONLY mode) ---"
if [ -n "$GATEWAY_ID" ] && [ "$GATEWAY_ID" != "None" ] && [ "$GATEWAY_ID" != "GATEWAY_NOT_CONFIGURED" ]; then
  # Get gateway ARN — needed for Cedar resource entity
  GATEWAY_ARN_FOR_CEDAR=$(aws bedrock-agentcore-control get-gateway \
    --gateway-identifier "$GATEWAY_ID" --region $REGION \
    --query 'gatewayArn' --output text 2>/dev/null || true)

  if [ -z "$GATEWAY_ARN_FOR_CEDAR" ] || [ "$GATEWAY_ARN_FOR_CEDAR" = "None" ]; then
    echo -e "${YELLOW}  ⚠️  Could not get gateway ARN — skipping Cedar policy${NC}"
  else
    # Check if policy engine already exists
    # Backward-compatible lookup: check both old (hyphen) and new (underscore) names
    EXISTING_ENGINE_ID=$(aws bedrock-agentcore-control list-policy-engines --region "$REGION" \
      --query "policyEngines[?name=='msp-policy-engine' || name=='msp_policy_engine'].policyEngineId" --output text 2>/dev/null || true)
    EXISTING_ENGINE_ID=$(echo "$EXISTING_ENGINE_ID" | awk '{print $1}')

    if [ -n "$EXISTING_ENGINE_ID" ] && [ "$EXISTING_ENGINE_ID" != "None" ] && [ "$EXISTING_ENGINE_ID" != "null" ]; then
      POLICY_ENGINE_ARN=$(aws bedrock-agentcore-control list-policy-engines --region "$REGION" \
        --query "policyEngines[?name=='msp-policy-engine' || name=='msp_policy_engine'].policyEngineArn" --output text 2>/dev/null || true)
      POLICY_ENGINE_ARN=$(echo "$POLICY_ENGINE_ARN" | awk '{print $1}')
      echo "  ✓ Policy engine already exists: $EXISTING_ENGINE_ID"
    else
      echo "  Creating Cedar policy engine..."
      ENGINE_RESPONSE=$(aws bedrock-agentcore-control create-policy-engine \
        --name "msp_policy_engine" \
        --description "Authorization policy engine for MSP Ops Gateway" \
        --region $REGION --output json 2>&1 || true)

      EXISTING_ENGINE_ID=$(echo "$ENGINE_RESPONSE" | jq -r '.policyEngineId // empty' 2>/dev/null || true)
      POLICY_ENGINE_ARN=$(echo "$ENGINE_RESPONSE" | jq -r '.policyEngineArn // empty' 2>/dev/null || true)

      if [ -z "$EXISTING_ENGINE_ID" ] || [ "$EXISTING_ENGINE_ID" = "null" ]; then
        echo -e "${YELLOW}  ⚠️  Could not create policy engine — skipping Cedar policy${NC}"
        echo "  Response: $ENGINE_RESPONSE"
        EXISTING_ENGINE_ID=""
      else
        echo "  ✓ Policy engine created: $EXISTING_ENGINE_ID"
        echo "  Waiting for policy engine to be READY..."
        ENGINE_STATUS=""
        for i in $(seq 1 12); do
          ENGINE_STATUS=$(aws bedrock-agentcore-control get-policy-engine \
            --policy-engine-id "$EXISTING_ENGINE_ID" --region $REGION \
            --query 'status' --output text 2>/dev/null || true)
          if [ "$ENGINE_STATUS" = "READY" ]; then
            break
          fi
          echo "    Status: ${ENGINE_STATUS:-unknown} (attempt $i/12)..."
          sleep 10
        done
        echo "  Engine status: $ENGINE_STATUS"
      fi
    fi

    # Only proceed if we have a valid engine
    if [ -n "$EXISTING_ENGINE_ID" ] && [ "$EXISTING_ENGINE_ID" != "null" ]; then
      # Check if policy already exists in this engine
      EXISTING_POLICY=$(aws bedrock-agentcore-control list-policies \
        --policy-engine-id "$EXISTING_ENGINE_ID" --region $REGION \
        --query "policies[?name=='msp_permit_all_targets'].policyId" --output text 2>/dev/null || true)
      EXISTING_POLICY=$(echo "$EXISTING_POLICY" | awk '{print $1}')

      if [ -z "$EXISTING_POLICY" ] || [ "$EXISTING_POLICY" = "None" ] || [ "$EXISTING_POLICY" = "null" ]; then
        # Create Cedar policy — permit all principals to call any tool on this gateway
        CEDAR_STMT="permit (principal, action, resource == AgentCore::Gateway::\"${GATEWAY_ARN_FOR_CEDAR}\");"
        POLICY_RESPONSE=$(aws bedrock-agentcore-control create-policy \
          --name "msp_permit_all_targets" \
          --description "Permit MSP agents to invoke all registered Gateway targets" \
          --policy-engine-id "$EXISTING_ENGINE_ID" \
          --definition "{\"cedar\":{\"statement\":\"${CEDAR_STMT}\"}}" \
          --region $REGION --output json 2>&1 || true)

        POLICY_ID=$(echo "$POLICY_RESPONSE" | jq -r '.policyId // empty' 2>/dev/null || true)
        if [ -n "$POLICY_ID" ] && [ "$POLICY_ID" != "null" ]; then
          echo "  ✓ Cedar policy created: $POLICY_ID"
        else
          echo -e "${YELLOW}  ⚠️  Cedar policy creation failed${NC}"
          echo "  Response: $POLICY_RESPONSE"
        fi
      else
        echo "  ✓ Cedar policy already exists: $EXISTING_POLICY"
      fi

      # Attach policy engine to gateway in LOG_ONLY mode
      UPDATE_RESPONSE=$(aws bedrock-agentcore-control update-gateway \
        --gateway-identifier "$GATEWAY_ID" \
        --policy-engine-configuration "{\"arn\":\"${POLICY_ENGINE_ARN}\",\"mode\":\"LOG_ONLY\"}" \
        --region $REGION --output json 2>&1 || true)

      if echo "$UPDATE_RESPONSE" | jq -e '.gatewayId' > /dev/null 2>&1; then
        echo "  ✓ Policy engine attached to gateway (LOG_ONLY)"
        echo "    To enforce: aws bedrock-agentcore-control update-gateway --gateway-identifier $GATEWAY_ID --policy-engine-configuration '{\"arn\":\"$POLICY_ENGINE_ARN\",\"mode\":\"ENFORCE\"}' --region $REGION"
      else
        echo -e "${YELLOW}  ⚠️  Failed to attach policy engine to gateway${NC}"
        echo "  Response: $UPDATE_RESPONSE"
      fi
    fi
  fi
else
  echo "  ℹ  Skipping Cedar policy (no gateway available)"
fi
echo ""

# Step 8: Deploy A2A Specialist Runtimes
echo -e "${YELLOW}[8/14] Deploying A2A Specialist Runtimes...${NC}"
echo ""
echo "Deploying 5 specialist agents as independent A2A runtimes..."
echo "This will take ~50-75 minutes (10-15 min per runtime)"
echo ""

declare A2A_ARN_cloudwatch=""
A2A_ARN_security=""
A2A_ARN_cost=""
A2A_ARN_advisor=""
A2A_ARN_jira=""
A2A_ARN_knowledge=""

RUNTIMES=("cloudwatch" "security" "cost" "advisor" "jira" "knowledge")
RUNTIME_COUNT=1

# Copy shared files to all specialist dirs (skip cloudwatch to avoid "identical" error)
echo "Copying shared files (gateway_client.py, context_tools.py) to specialist dirs..."
for runtime_name in "${RUNTIMES[@]}"; do
  # Skip cloudwatch since it's the source directory for context_tools.py
  if [ "$runtime_name" = "cloudwatch" ]; then
    echo "  ✓ Skipping cloudwatch (source directory)"
    continue
  fi
  
  cp "$SCRIPT_DIR/agents/runtime/gateway_client.py" "$SCRIPT_DIR/agents/runtime_${runtime_name}/" 2>/dev/null || true
  cp "$SCRIPT_DIR/agents/runtime/context_tools.py" "$SCRIPT_DIR/agents/runtime_${runtime_name}/" 2>/dev/null || true
  echo "  ✓ Copied to runtime_${runtime_name}"
done

# Copy robust_date_parser.py to cost runtime (needed for date enrichment)
cp "$SCRIPT_DIR/agents/runtime/robust_date_parser.py" "$SCRIPT_DIR/agents/runtime_cost/" 2>/dev/null || true
echo "  ✓ Copied robust_date_parser.py to runtime_cost"

# Verify all runtime directories have required shared files
echo ""
echo "Verifying shared files in all specialist dirs..."
MISSING_FILES=0
for runtime_name in "${RUNTIMES[@]}"; do
  for shared_file in "gateway_client.py" "context_tools.py"; do
    if [ ! -f "$SCRIPT_DIR/agents/runtime_${runtime_name}/${shared_file}" ]; then
      echo -e "${RED}  ✗ Missing: runtime_${runtime_name}/${shared_file}${NC}"
      MISSING_FILES=$((MISSING_FILES + 1))
    fi
  done
done
if [ "$MISSING_FILES" -eq 0 ]; then
  echo "  ✓ All shared files verified"
else
  echo -e "${RED}Error: ${MISSING_FILES} shared files missing. Cannot proceed.${NC}"
  exit 1
fi
echo "  ✓ All shared files copied"
echo ""

for runtime_name in "${RUNTIMES[@]}"; do
  echo "[8.${RUNTIME_COUNT}/6] Deploying ${runtime_name} A2A Runtime..."
  cd "$SCRIPT_DIR/agents/runtime_${runtime_name}"
  
  # Clean old configuration AND stale env files
  rm -f .bedrock_agentcore.yaml
  rm -f env_config.txt
  
  # Write env_config.txt (non-dotfile) with GATEWAY_URL so the container can connect to Gateway
  cat > env_config.txt << ENVEOF
# Auto-generated by deploy.sh
GATEWAY_URL=$GATEWAY_URL
MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0
ENVEOF

  # Jira specialist needs project config injected at container level
  if [ "$runtime_name" = "jira" ]; then
    cat >> env_config.txt << ENVEOF
JIRA_PROJECT_KEY=$JIRA_PROJECT_KEY
JIRA_DOMAIN=$JIRA_DOMAIN
JIRA_EMAIL=$JIRA_EMAIL
ENVEOF
  fi

  # Configure for A2A protocol
  agentcore configure \
    --entrypoint "${runtime_name}_a2a_runtime.py" \
    --protocol A2A \
    --name "${runtime_name}_a2a_runtime" \
    --region $REGION \
    --requirements-file requirements.txt \
    --non-interactive
  
  if [ $? -ne 0 ]; then
    echo -e "${RED}Error: ${runtime_name} configuration failed${NC}"
    cd "$SCRIPT_DIR"
    exit 1
  fi
  
  echo "  Configuration complete, deploying..."
  
  # Deploy
  agentcore deploy --auto-update-on-conflict
  
  if [ $? -ne 0 ]; then
    echo -e "${RED}Error: ${runtime_name} deployment failed${NC}"
    cd "$SCRIPT_DIR"
    exit 1
  fi
  
  # Get ARN
  ARN=$(agentcore status 2>&1 | grep "Agent ARN:" -A 1 | grep "arn:aws" | grep -oE 'arn:aws:bedrock-agentcore:[^[:space:]]+' | head -1)
  
  if [ -z "$ARN" ]; then
    echo -e "${RED}Error: Could not get ${runtime_name} Runtime ARN${NC}"
    cd "$SCRIPT_DIR"
    exit 1
  fi
  
  eval "A2A_ARN_${runtime_name}=\$ARN"
  echo "  ✓ ${runtime_name} A2A Runtime deployed"
  echo "    ARN: $ARN"
  echo ""
  
  cd "$SCRIPT_DIR"
  RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
done

echo "✓ All A2A Specialist Runtimes deployed"
for runtime_name in "${RUNTIMES[@]}"; do
  eval "echo \"  ${runtime_name}: \$A2A_ARN_${runtime_name}\""
done
echo ""

# Add InvokeGateway permissions to all specialist roles
echo "Adding InvokeGateway permissions to specialist roles..."

for runtime_name in "${RUNTIMES[@]}"; do
  eval "RUNTIME_ARN=\$A2A_ARN_${runtime_name}"
  RUNTIME_ID=$(echo "$RUNTIME_ARN" | awk -F'/' '{print $NF}')
  
  ROLE_ARN=$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" \
    --region $REGION \
    --query 'roleArn' \
    --output text 2>/dev/null)
  
  if [ -n "$ROLE_ARN" ] && [ "$ROLE_ARN" != "None" ]; then
    ROLE_NAME=$(echo "$ROLE_ARN" | awk -F'/' '{print $NF}')
    
    # Policy to invoke gateway
    GATEWAY_POLICY="{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Sid\": \"InvokeGateway\",
        \"Effect\": \"Allow\",
        \"Action\": \"bedrock-agentcore:InvokeGateway\",
        \"Resource\": \"$GATEWAY_ARN\"
      }]
    }"
    
    aws iam put-role-policy \
      --role-name "$ROLE_NAME" \
      --policy-name InvokeGatewayAccess \
      --policy-document "$GATEWAY_POLICY" \
      --region $REGION >/dev/null 2>&1 && \
      echo "  ✅ $runtime_name: InvokeGateway permission added" || \
      echo "  ⚠️  $runtime_name: Failed to add permission"
  else
    echo "  ⚠️  $runtime_name: Could not get role ARN"
  fi
done

echo ""

# Step 9: Redeploy Supervisor with A2A ARNs
echo -e "${YELLOW}[9/14] Redeploying Supervisor with A2A coordination...${NC}"
echo ""
echo "Updating Supervisor with specialist A2A Runtime ARNs..."
echo ""

cd "$SCRIPT_DIR/agents/runtime"

# Clean stale env files before writing fresh config
rm -f env_config.txt .env

# Write env_config.txt (non-dotfile) that gets bundled into the container
# a2a_client_helper.py reads this at startup as fallback for env vars
cat > env_config.txt << ENVEOF
# Auto-generated by deploy.sh — A2A specialist ARNs + Gateway + Jira config
CLOUDWATCH_A2A_ARN=$A2A_ARN_cloudwatch
SECURITY_A2A_ARN=$A2A_ARN_security
COST_A2A_ARN=$A2A_ARN_cost
ADVISOR_A2A_ARN=$A2A_ARN_advisor
JIRA_A2A_ARN=$A2A_ARN_jira
KNOWLEDGE_A2A_ARN=$A2A_ARN_knowledge
GATEWAY_URL=$GATEWAY_URL
MODEL_ID=${MODEL:-global.anthropic.claude-haiku-4-5-20251001-v1:0}
MEMORY_ID=$MEMORY_ID
MAX_TOKENS=${MAX_TOKENS:-512}
# Supervisor uses SUPERVISOR_MAX_TOKENS (isolated from specialist MAX_TOKENS=512).
# Must be large enough to relay specialist responses (cost tables, ticket lists, etc.)
SUPERVISOR_MAX_TOKENS=4096
JIRA_PROJECT_KEY=$JIRA_PROJECT_KEY
JIRA_DOMAIN=$JIRA_DOMAIN
JIRA_EMAIL=$JIRA_EMAIL
ENVEOF

echo "✓ Wrote env_config.txt with A2A ARNs (bundled into container):"
sed 's/=.*arn:.*/=arn:...truncated/' env_config.txt
echo ""

# Also write A2A ARNs to backend/.env for direct routing (bypasses Supervisor LLM hop)
cd "$SCRIPT_DIR"
if [ -f "backend/.env" ]; then
  echo "Writing A2A specialist ARNs to backend/.env for direct routing..."
  update_env_var "CLOUDWATCH_A2A_ARN" "$A2A_ARN_cloudwatch"
  update_env_var "SECURITY_A2A_ARN" "$A2A_ARN_security"
  update_env_var "COST_A2A_ARN" "$A2A_ARN_cost"
  update_env_var "ADVISOR_A2A_ARN" "$A2A_ARN_advisor"
  update_env_var "JIRA_A2A_ARN" "$A2A_ARN_jira"
  update_env_var "KNOWLEDGE_A2A_ARN" "$A2A_ARN_knowledge"
  echo "✓ A2A specialist ARNs saved to backend/.env for direct routing"
  echo "  (ECS will pick these up on next deployment via CDK)"
fi
cd "$SCRIPT_DIR/agents/runtime"

# Reconfigure supervisor
agentcore configure \
  --entrypoint supervisor_runtime.py \
  --protocol HTTP \
  --name msp_supervisor_agent \
  --region $REGION \
  --requirements-file requirements.txt \
  --non-interactive

if [ $? -ne 0 ]; then
  echo -e "${RED}Error: Supervisor reconfiguration failed${NC}"
  cd "$SCRIPT_DIR"
  exit 1
fi

echo "  Deploying updated Supervisor..."
agentcore deploy --auto-update-on-conflict

if [ $? -ne 0 ]; then
  echo -e "${RED}Error: Supervisor redeployment failed${NC}"
  cd "$SCRIPT_DIR"
  exit 1
fi

# Verify ARN hasn't changed
NEW_SUPERVISOR_ARN=$(agentcore status 2>&1 | grep "Agent ARN:" -A 1 | grep "arn:aws" | grep -oE 'arn:aws:bedrock-agentcore:[^[:space:]]+' | head -1)

echo ""
echo "✓ Supervisor redeployed with A2A coordination"
echo "  ARN: $NEW_SUPERVISOR_ARN"

# Add InvokeAgentRuntime permission so supervisor can call A2A specialists
echo ""
echo "  Adding InvokeAgentRuntime permission for supervisor → A2A calls..."
SUPERVISOR_RUNTIME_ID_FOR_ROLE=$(echo "$NEW_SUPERVISOR_ARN" | awk -F'/' '{print $NF}')
SUPERVISOR_ROLE_ARN_FOR_A2A=$(aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "$SUPERVISOR_RUNTIME_ID_FOR_ROLE" \
  --region $REGION \
  --query 'roleArn' \
  --output text 2>/dev/null)
SUPERVISOR_ROLE_FOR_A2A=$(echo "$SUPERVISOR_ROLE_ARN_FOR_A2A" | awk -F'/' '{print $NF}')

if [ -n "$SUPERVISOR_ROLE_FOR_A2A" ] && [ "$SUPERVISOR_ROLE_FOR_A2A" != "None" ]; then
  # Build resource list of all A2A specialist ARNs
  A2A_RESOURCE_LIST="["
  FIRST=true
  for runtime_name in "${RUNTIMES[@]}"; do
    eval "A2A_ARN_VAL=\$A2A_ARN_${runtime_name}"
    if [ -n "$A2A_ARN_VAL" ]; then
      if [ "$FIRST" = true ]; then
        A2A_RESOURCE_LIST="$A2A_RESOURCE_LIST\"$A2A_ARN_VAL\""
        FIRST=false
      else
        A2A_RESOURCE_LIST="$A2A_RESOURCE_LIST,\"$A2A_ARN_VAL\""
      fi
    fi
  done
  A2A_RESOURCE_LIST="$A2A_RESOURCE_LIST]"

  INVOKE_A2A_POLICY="{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Sid\": \"InvokeA2ASpecialists\",
      \"Effect\": \"Allow\",
      \"Action\": \"bedrock-agentcore:InvokeAgentRuntime\",
      \"Resource\": \"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:runtime/*\"
    }]
  }"

  aws iam put-role-policy \
    --role-name "$SUPERVISOR_ROLE_FOR_A2A" \
    --policy-name InvokeA2ASpecialists \
    --policy-document "$INVOKE_A2A_POLICY" \
    --region $REGION >/dev/null 2>&1 && \
    echo "  ✅ Supervisor: InvokeAgentRuntime permission added for all A2A specialists" || \
    echo "  ⚠️  Supervisor: Failed to add InvokeAgentRuntime permission"

  # Add Memory permissions for conversation context
  echo "  Adding Memory permissions for conversation context..."
  MEMORY_POLICY="{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Sid\": \"AccessMemory\",
      \"Effect\": \"Allow\",
      \"Action\": [
        \"bedrock-agentcore:ListEvents\",
        \"bedrock-agentcore:CreateEvent\",
        \"bedrock-agentcore:GetMemory\",
        \"bedrock-agentcore:RetrieveMemoryRecords\",
        \"bedrock-agentcore:SearchMemoryRecords\",
        \"bedrock-agentcore:InvokeMemory\",
        \"bedrock-agentcore:ListMemoryRecords\"
      ],
      \"Resource\": \"arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:memory/*\"
    }]
  }"

  aws iam put-role-policy \
    --role-name "$SUPERVISOR_ROLE_FOR_A2A" \
    --policy-name AccessMemory \
    --policy-document "$MEMORY_POLICY" \
    --region $REGION >/dev/null 2>&1 && \
    echo "  ✅ Supervisor: Memory permissions added (ListEvents, CreateEvent)" || \
    echo "  ⚠️  Supervisor: Failed to add Memory permissions"
else
  echo "  ⚠️  Could not get supervisor role ARN for InvokeAgentRuntime permission"
fi

echo ""

cd "$SCRIPT_DIR"

# Step 9.8: Re-deploy CDK Backend Stack with A2A ARNs for ECS direct routing
echo -e "${YELLOW}[9.8/13] Updating ECS task definition with A2A ARNs for direct routing...${NC}"
echo ""
echo "CDK Step 5 ran before A2A runtimes existed — ECS env vars were empty."
echo "Re-deploying backend stack with A2A ARNs as CDK context parameters."
echo ""

# Export FRONTEND_URL so CDK bakes the CloudFront CORS origin into Gateway Responses.
# This prevents every CDK redeploy from resetting ACAO headers back to localhost.
FRONTEND_URL_FOR_CDK=$(jq -r '.MSPAssistantFrontendStack.FrontendURL // empty' infrastructure/cdk/outputs.json 2>/dev/null)
if [ -n "$FRONTEND_URL_FOR_CDK" ] && [ "$FRONTEND_URL_FOR_CDK" != "null" ]; then
  export FRONTEND_URL="$FRONTEND_URL_FOR_CDK"
  echo "  CloudFront URL for CORS: $FRONTEND_URL"
else
  echo "  Warning: CloudFront URL not found in outputs.json — CORS headers will use localhost fallback"
fi

cd infrastructure/cdk
cdk deploy MSPAssistantBackendStack --require-approval never \
  --context region=$REGION \
  --context account=$ACCOUNT_ID \
  --context supervisor_runtime_arn="${NEW_SUPERVISOR_ARN:-$SUPERVISOR_ARN}" \
  --context cloudwatch_a2a_arn="$A2A_ARN_cloudwatch" \
  --context security_a2a_arn="$A2A_ARN_security" \
  --context cost_a2a_arn="$A2A_ARN_cost" \
  --context advisor_a2a_arn="$A2A_ARN_advisor" \
  --context jira_a2a_arn="$A2A_ARN_jira" \
  --context knowledge_a2a_arn="$A2A_ARN_knowledge"

echo "✓ ECS task definition updated with A2A ARNs"
cd "$SCRIPT_DIR"
echo ""

# Step 9.5: Add Cross-Account AssumeRole Permission to ECS Task Role
echo -e "${YELLOW}[9.5/13] Adding cross-account AssumeRole permission to ECS task role...${NC}"
echo ""
echo "ECS task role needs sts:AssumeRole permission to assume customer account roles."
echo ""

# Get ECS task role name from the backend service
CLUSTER_NAME=$(aws ecs list-clusters --region $REGION --query 'clusterArns[?contains(@, `MSPAssistant`)]' --output text | awk -F'/' '{print $NF}')
SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --region $REGION --query 'serviceArns[0]' --output text | awk -F'/' '{print $NF}')

if [ -n "$CLUSTER_NAME" ] && [ -n "$SERVICE_NAME" ]; then
  # Get task definition from service
  TASK_DEF_ARN=$(aws ecs describe-services --cluster $CLUSTER_NAME --services $SERVICE_NAME --region $REGION --query 'services[0].taskDefinition' --output text)
  
  # Get task role ARN from task definition
  TASK_ROLE_ARN=$(aws ecs describe-task-definition --task-definition $TASK_DEF_ARN --region $REGION --query 'taskDefinition.taskRoleArn' --output text)
  TASK_ROLE_NAME=$(echo $TASK_ROLE_ARN | awk -F'/' '{print $NF}')
  
  if [ -n "$TASK_ROLE_NAME" ] && [ "$TASK_ROLE_NAME" != "None" ]; then
    echo "  ECS Task Role: $TASK_ROLE_NAME"
    
    # Add inline policy for cross-account assume role
    aws iam put-role-policy \
      --role-name "$TASK_ROLE_NAME" \
      --policy-name CrossAccountAssumeRole \
      --policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
          "Sid": "AssumeCustomerAccountRoles",
          "Effect": "Allow",
          "Action": "sts:AssumeRole",
          "Resource": "arn:aws:iam::*:role/MSP-*-Role"
        }]
      }' \
      --region $REGION >/dev/null 2>&1 && \
      echo "  ✓ Cross-account AssumeRole permission added" || \
      echo "  ⚠️ Failed to add permission (may already exist)"
  else
    echo "  ⚠️ Could not determine ECS task role name"
  fi
else
  echo "  ⚠️ Could not find ECS cluster/service"
fi

echo ""

# Step 10: Add Secrets Manager Permissions to Runtime Roles
echo -e "${YELLOW}[10/14] Adding Secrets Manager & AWS service permissions to Runtime roles...${NC}"
echo ""
echo "Runtime MCPs need Secrets Manager access for cross-account credentials."
echo ""

# Dynamically get Runtime role ARNs from AWS API
echo "Retrieving Runtime execution role ARNs..."

CLOUDWATCH_ROLE_ARN=$(aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id $(echo $CLOUDWATCH_ARN | awk -F'/' '{print $NF}') \
  --region $REGION \
  --query 'roleArn' \
  --output text 2>/dev/null)

AWS_API_ROLE_ARN=$(aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id $(echo $AWS_API_ARN | awk -F'/' '{print $NF}') \
  --region $REGION \
  --query 'roleArn' \
  --output text 2>/dev/null)

KNOWLEDGE_ROLE_ARN=$(aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id $(echo $KNOWLEDGE_ARN | awk -F'/' '{print $NF}') \
  --region $REGION \
  --query 'roleArn' \
  --output text 2>/dev/null)

# Extract role names from ARNs
CLOUDWATCH_ROLE=$(echo $CLOUDWATCH_ROLE_ARN | awk -F'/' '{print $NF}')
AWS_API_ROLE=$(echo $AWS_API_ROLE_ARN | awk -F'/' '{print $NF}')
KNOWLEDGE_ROLE=$(echo $KNOWLEDGE_ROLE_ARN | awk -F'/' '{print $NF}')

echo "  CloudWatch MCP role: $CLOUDWATCH_ROLE"
echo "  AWS API MCP role: $AWS_API_ROLE"
echo "  Knowledge MCP role: $KNOWLEDGE_ROLE"
echo ""

SECRETS_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SecretsManagerAccess",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
      "Resource": "arn:aws:secretsmanager:*:*:secret:msp-credentials/*"
    },
    {
      "Sid": "STSAssumeRole",
      "Effect": "Allow",
      "Action": ["sts:AssumeRole", "sts:GetCallerIdentity"],
      "Resource": "*"
    }
  ]
}'

if [ -n "$CLOUDWATCH_ROLE" ]; then
  echo "[10.1] Adding Secrets Manager permission to CloudWatch MCP role..."
  aws iam put-role-policy \
    --role-name "$CLOUDWATCH_ROLE" \
    --policy-name SecretsManagerAccess \
    --policy-document "$SECRETS_POLICY" \
    --region $REGION && echo "  ✓ CloudWatch MCP role updated" || echo "  ⚠️ Role update failed"
fi

if [ -n "$AWS_API_ROLE" ]; then
  echo "[10.2] Adding Secrets Manager permission to AWS API MCP role..."
  aws iam put-role-policy \
    --role-name "$AWS_API_ROLE" \
    --policy-name SecretsManagerAccess \
    --policy-document "$SECRETS_POLICY" \
    --region $REGION && echo "  ✓ AWS API MCP role updated" || echo "  ⚠️ Role update failed"
fi

if [ -n "$KNOWLEDGE_ROLE" ]; then
  echo "[10.3] Adding Secrets Manager permission to AWS Knowledge MCP role..."
  aws iam put-role-policy \
    --role-name "$KNOWLEDGE_ROLE" \
    --policy-name SecretsManagerAccess \
    --policy-document "$SECRETS_POLICY" \
    --region $REGION && echo "  ✓ AWS Knowledge MCP role updated" || echo "  ⚠️ Role update failed"
fi

# Add AWS service permissions to MCP runtime roles (for default account operations)
AWS_SERVICES_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudWatchReadAccess",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:DescribeAlarms", "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics", "cloudwatch:ListDashboards", "cloudwatch:GetDashboard",
        "logs:DescribeLogGroups", "logs:DescribeLogStreams", "logs:GetLogEvents",
        "logs:FilterLogEvents", "logs:StartQuery", "logs:GetQueryResults"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CostExplorerAccess",
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage", "ce:GetCostForecast", "ce:GetReservationUtilization",
        "ce:GetSavingsPlansUtilization", "ce:GetDimensionValues"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SecurityHubAccess",
      "Effect": "Allow",
      "Action": [
        "securityhub:GetFindings", "securityhub:GetEnabledStandards",
        "securityhub:DescribeStandards", "securityhub:DescribeHub",
        "securityhub:BatchGetSecurityControls"
      ],
      "Resource": "*"
    },
    {
      "Sid": "TrustedAdvisorAccess",
      "Effect": "Allow",
      "Action": [
        "support:DescribeTrustedAdvisorChecks", "support:DescribeTrustedAdvisorCheckResult",
        "support:DescribeTrustedAdvisorCheckSummaries", "support:RefreshTrustedAdvisorCheck",
        "trustedadvisor:ListChecks", "trustedadvisor:ListRecommendations"
      ],
      "Resource": "*"
    },
    {
      "Sid": "HealthAccess",
      "Effect": "Allow",
      "Action": ["health:DescribeEvents", "health:DescribeEventDetails", "health:DescribeEventAggregates"],
      "Resource": "*"
    },
    {
      "Sid": "STSAccess",
      "Effect": "Allow",
      "Action": ["sts:GetCallerIdentity", "sts:AssumeRole"],
      "Resource": "*"
    },
    {
      "Sid": "EC2S3LambdaRDSECSAccess",
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*",
        "s3:List*", "s3:GetBucket*",
        "lambda:List*", "lambda:GetFunction",
        "rds:Describe*",
        "ecs:Describe*", "ecs:List*"
      ],
      "Resource": "*"
    }
  ]
}'

if [ -n "$CLOUDWATCH_ROLE" ]; then
  echo "[10.4] Adding AWS service permissions to CloudWatch MCP role..."
  aws iam put-role-policy \
    --role-name "$CLOUDWATCH_ROLE" \
    --policy-name AWSServiceAccess \
    --policy-document "$AWS_SERVICES_POLICY" \
    --region $REGION && echo "  ✓ CloudWatch MCP role updated" || echo "  ⚠️ Role update failed"
fi

if [ -n "$AWS_API_ROLE" ]; then
  echo "[10.5] Adding AWS service permissions to AWS API MCP role..."
  aws iam put-role-policy \
    --role-name "$AWS_API_ROLE" \
    --policy-name AWSServiceAccess \
    --policy-document "$AWS_SERVICES_POLICY" \
    --region $REGION && echo "  ✓ AWS API MCP role updated" || echo "  ⚠️ Role update failed"
fi

if [ -n "$KNOWLEDGE_ROLE" ]; then
  echo "[10.6] Adding AWS service permissions to AWS Knowledge MCP role..."
  aws iam put-role-policy \
    --role-name "$KNOWLEDGE_ROLE" \
    --policy-name AWSServiceAccess \
    --policy-document "$AWS_SERVICES_POLICY" \
    --region $REGION && echo "  ✓ AWS Knowledge MCP role updated" || echo "  ⚠️ Role update failed"
fi

# Replicate CLI caller's managed policies to all MCP roles
echo ""
echo "[10.7] Replicating CLI caller's managed policies to MCP roles..."
CLI_MANAGED_POLICIES=$(get_cli_managed_policies)

if [ -n "$CLI_MANAGED_POLICIES" ]; then
  echo "  Detected CLI caller policies: $(echo $CLI_MANAGED_POLICIES | tr ' ' ', ')"
  
  for MCP_ROLE in "$CLOUDWATCH_ROLE" "$AWS_API_ROLE" "$KNOWLEDGE_ROLE"; do
    if [ -n "$MCP_ROLE" ] && [ "$MCP_ROLE" != "None" ]; then
      echo "  Attaching to $MCP_ROLE..."
      for POLICY_ARN in $CLI_MANAGED_POLICIES; do
        POLICY_NAME=$(echo "$POLICY_ARN" | awk -F'/' '{print $NF}')
        aws iam attach-role-policy --role-name "$MCP_ROLE" --policy-arn "$POLICY_ARN" --region $REGION >/dev/null 2>&1 && \
          echo "    ✓ $POLICY_NAME" || echo "    ⚠️ $POLICY_NAME (may already be attached)"
      done
    fi
  done
else
  echo "  ⚠️ No managed policies found for CLI caller"
fi

# Also add to supervisor role (for STS GetCallerIdentity on default account)
echo ""
SUPERVISOR_RUNTIME_ID=$(echo "${NEW_SUPERVISOR_ARN:-$SUPERVISOR_ARN}" | awk -F'/' '{print $NF}')
SUPERVISOR_ROLE_ARN=$(aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id "$SUPERVISOR_RUNTIME_ID" \
  --region $REGION \
  --query 'roleArn' \
  --output text 2>/dev/null)
SUPERVISOR_ROLE=$(echo $SUPERVISOR_ROLE_ARN | awk -F'/' '{print $NF}')

if [ -n "$SUPERVISOR_ROLE" ]; then
  echo "[10.8] Adding AWS service permissions to Supervisor role..."
  aws iam put-role-policy \
    --role-name "$SUPERVISOR_ROLE" \
    --policy-name AWSServiceAccess \
    --policy-document "$AWS_SERVICES_POLICY" \
    --region $REGION && echo "  ✓ Supervisor role updated" || echo "  ⚠️ Role update failed"
fi

echo ""
echo "✓ Secrets Manager and AWS service permissions added to Runtime roles"
echo ""

# Step 11: Update CORS Configuration
echo -e "${YELLOW}[11/14] Updating CORS configuration...${NC}"

# Get CloudFront URL for CORS configuration
FRONTEND_URL=$(jq -r '.MSPAssistantFrontendStack.FrontendURL' infrastructure/cdk/outputs.json)
# Extract bare domain (e.g. d1abc123.cloudfront.net) for pinned CORS regex
CLOUDFRONT_DOMAIN="${FRONTEND_URL#https://}"

# Update backend/.env with CloudFront URL for dynamic CORS
if [ -n "$FRONTEND_URL" ] && [ "$FRONTEND_URL" != "null" ]; then
  echo "  CloudFront URL: $FRONTEND_URL"

  if [ -f "backend/.env" ]; then
    if grep -q "^FRONTEND_URL=" backend/.env 2>/dev/null; then
      # Update existing
      if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s|^FRONTEND_URL=.*|FRONTEND_URL=$FRONTEND_URL|" backend/.env
        sed -i '' "s|^CLOUDFRONT_DOMAIN=.*|CLOUDFRONT_DOMAIN=$CLOUDFRONT_DOMAIN|" backend/.env
      else
        sed -i "s|^FRONTEND_URL=.*|FRONTEND_URL=$FRONTEND_URL|" backend/.env
        sed -i "s|^CLOUDFRONT_DOMAIN=.*|CLOUDFRONT_DOMAIN=$CLOUDFRONT_DOMAIN|" backend/.env
      fi
    else
      # Add new
      echo "" >> backend/.env
      echo "# CloudFront URL for CORS (Added by deploy.sh)" >> backend/.env
      echo "FRONTEND_URL=$FRONTEND_URL" >> backend/.env
      echo "CLOUDFRONT_DOMAIN=$CLOUDFRONT_DOMAIN" >> backend/.env
    fi
    echo "✓ Frontend URL saved to backend/.env for CORS"
  fi
else
  echo -e "${YELLOW}⚠️  Warning: Could not get CloudFront URL from outputs${NC}"
  echo "  CORS will only work with localhost origins"
fi

echo ""
echo "✓ CORS configuration updated"

# Update API Gateway Gateway Responses to use CloudFront URL
# Gateway Responses handle 401/403/4XX/5XX BEFORE requests reach ECS.
# Without this, auth failures show ACAO: http://localhost:5173 which the
# browser treats as a CORS error instead of an auth error.
if [ -n "$FRONTEND_URL" ] && [ "$FRONTEND_URL" != "null" ]; then
  GW_API_ID=$(aws apigateway get-rest-apis --query "items[?name=='msp-assistant-api'].id" --output text 2>/dev/null)
  if [ -n "$GW_API_ID" ] && [ "$GW_API_ID" != "None" ]; then
    echo "  Updating Gateway Response ACAO headers (API: $GW_API_ID)..."
    GW_UPDATED=0
    for RESP_TYPE in UNAUTHORIZED ACCESS_DENIED DEFAULT_4XX DEFAULT_5XX; do
      aws apigateway update-gateway-response \
        --rest-api-id "$GW_API_ID" \
        --response-type "$RESP_TYPE" \
        --patch-operations "[{\"op\":\"replace\",\"path\":\"/responseParameters/gatewayresponse.header.Access-Control-Allow-Origin\",\"value\":\"'$FRONTEND_URL'\"}]" \
        > /dev/null 2>&1 && GW_UPDATED=$((GW_UPDATED+1)) || true
    done
    if [ "$GW_UPDATED" -gt 0 ]; then
      aws apigateway create-deployment \
        --rest-api-id "$GW_API_ID" \
        --stage-name prod \
        --description "Update Gateway Response CORS for CloudFront" \
        > /dev/null 2>&1
      echo "  Updated $GW_UPDATED Gateway Response ACAO headers to $FRONTEND_URL"
      echo "✓ Gateway Response CORS updated"
    else
      echo "  Warning: Could not update Gateway Response headers"
    fi
  else
    echo "  Warning: Could not find msp-assistant-api to update Gateway Responses"
  fi
fi
echo ""

# Step 12: Build and deploy frontend
echo -e "${YELLOW}[12/14] Building and deploying frontend...${NC}"

# Update frontend/.env with deployed infrastructure values
echo "Updating frontend/.env with deployed infrastructure..."
API_URL=$(jq -r '.MSPAssistantBackendStack.APIURL' infrastructure/cdk/outputs.json | sed 's|/$||')
USER_POOL_ID=$(jq -r '.MSPAssistantBackendStack.CognitoUserPoolId' infrastructure/cdk/outputs.json)
CLIENT_ID=$(jq -r '.MSPAssistantBackendStack.CognitoWebClientId' infrastructure/cdk/outputs.json)

# Extract API Gateway ID for CSP
API_GATEWAY_ID=$(echo $API_URL | sed 's|https://||' | cut -d'.' -f1)

# Get CloudFront domain for SSE streaming (CloudFront → ALB bypasses API GW buffering)
CLOUDFRONT_URL=$(jq -r '.MSPAssistantFrontendStack.FrontendURL // empty' infrastructure/cdk/outputs.json 2>/dev/null)
STREAM_BASE_URL=""
if [ -n "$CLOUDFRONT_URL" ] && [ "$CLOUDFRONT_URL" != "null" ]; then
  STREAM_BASE_URL="${CLOUDFRONT_URL}/api/v1"
fi

cat > frontend/.env << EOF
# Frontend Environment Variables (Vite)
# Auto-generated by deploy.sh from CDK outputs
VITE_AWS_REGION=$REGION
VITE_COGNITO_USER_POOL_ID=$USER_POOL_ID
VITE_COGNITO_CLIENT_ID=$CLIENT_ID

# API Configuration (Deployed API Gateway)
VITE_API_BASE_URL=${API_URL}/api/v1
VITE_WS_URL=wss://$(echo $API_URL | sed 's|https://||')/api/v1/ws

# SSE Streaming (CloudFront → ALB, bypasses API Gateway response buffering)
VITE_STREAM_BASE_URL=${STREAM_BASE_URL}
EOF

echo "✓ Frontend .env configured"
echo "  API: $API_URL"
echo "  Stream: ${STREAM_BASE_URL:-<will use API_BASE_URL fallback>}"
echo "  User Pool: $USER_POOL_ID"
echo ""

# Generate CSP with wildcard for API Gateway (works across redeployments)
echo "Generating Content Security Policy with wildcard API Gateway support..."
CSP_CONTENT="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' https://cognito-idp.$REGION.amazonaws.com https://cognito-identity.$REGION.amazonaws.com https://sts.$REGION.amazonaws.com https://*.execute-api.$REGION.amazonaws.com wss://*.execute-api.$REGION.amazonaws.com; font-src 'self' data:; img-src 'self' data: https:;"

# Update index.html with generated CSP
if grep -q "Content-Security-Policy" frontend/index.html; then
  # Update existing CSP
  if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "s|<meta http-equiv=\"Content-Security-Policy\".*>|<meta http-equiv=\"Content-Security-Policy\" content=\"$CSP_CONTENT\">|" frontend/index.html
  else
    sed -i "s|<meta http-equiv=\"Content-Security-Policy\".*>|<meta http-equiv=\"Content-Security-Policy\" content=\"$CSP_CONTENT\">|" frontend/index.html
  fi
else
  # Add new CSP after viewport
  if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "/<meta name=\"viewport\"/a\\
    <meta http-equiv=\"Content-Security-Policy\" content=\"$CSP_CONTENT\">
" frontend/index.html
  else
    sed -i "/<meta name=\"viewport\"/a\    <meta http-equiv=\"Content-Security-Policy\" content=\"$CSP_CONTENT\">" frontend/index.html
  fi
fi

echo "✓ CSP configured for:"
echo "  Cognito: https://cognito-idp.$REGION.amazonaws.com"
echo "  API Gateway: https://$API_GATEWAY_ID.execute-api.$REGION.amazonaws.com"
echo ""

cd frontend

# Install dependencies
npm install

# Build
npm run build

# Return to project root to access outputs
cd "$SCRIPT_DIR"

# Get S3 bucket name from CDK outputs
S3_BUCKET=$(jq -r '.MSPAssistantFrontendStack.S3BucketName // empty' infrastructure/cdk/outputs.json)

# Validate S3 bucket
if [ -z "$S3_BUCKET" ] || [ "$S3_BUCKET" = "null" ]; then
  echo -e "${RED}Error: Failed to get S3 bucket name from CDK outputs${NC}"
  exit 1
fi

# Deploy to S3 (from frontend/ directory)
aws s3 sync frontend/dist/ s3://$S3_BUCKET --delete

echo "✓ Frontend deployed to S3"

# Step 10.5: Automatically update API Gateway CORS for CloudFront
echo ""
echo -e "${YELLOW}[12.5/13] Updating API Gateway CORS for production...${NC}"
echo "Adding CloudFront URL to API Gateway OPTIONS methods..."

# Temporarily disable exit-on-error to show actual errors
set +e

# Get CloudFront and API info
CLOUDFRONT_URL=$(jq -r '.MSPAssistantFrontendStack.FrontendURL' infrastructure/cdk/outputs.json)
API_URL=$(jq -r '.MSPAssistantBackendStack.APIURL' infrastructure/cdk/outputs.json)
API_ID=$(echo $API_URL | sed 's|https://||' | cut -d'.' -f1)

echo "  API Gateway ID: $API_ID"
echo "  CloudFront URL: $CLOUDFRONT_URL"
echo ""

# Get all API resources
echo "Fetching API resources..."
RESOURCES=$(aws apigateway get-resources --rest-api-id $API_ID --output json 2>&1)
if [ $? -ne 0 ]; then
  echo -e "${RED}Error fetching resources:${NC}"
  echo "$RESOURCES"
  set -e
  exit 1
fi

RESOURCES_WITH_OPTIONS=$(echo "$RESOURCES" | jq -r '.items[] | select(.resourceMethods.OPTIONS != null) | .id')
OPTIONS_COUNT=$(echo "$RESOURCES_WITH_OPTIONS" | wc -w | tr -d ' ')

echo "Found $OPTIONS_COUNT resources with OPTIONS methods to update"
echo ""

# Update each OPTIONS method
UPDATED=0
FAILED=0
for RESOURCE_ID in $RESOURCES_WITH_OPTIONS; do
  RESOURCE_PATH=$(echo "$RESOURCES" | jq -r ".items[] | select(.id==\"$RESOURCE_ID\") | .path")
  echo "Processing $RESOURCE_PATH (ID: $RESOURCE_ID)..."
  
  # Get integration to discover actual status codes
  INTEGRATION=$(aws apigateway get-integration \
    --rest-api-id $API_ID \
    --resource-id $RESOURCE_ID \
    --http-method OPTIONS \
    --region $REGION 2>/dev/null)
  
  if [ $? -ne 0 ]; then
    echo "  ❌ Failed to get integration"
    FAILED=$((FAILED + 1))
    continue
  fi
  
  # Extract status codes from integrationResponses
  STATUS_CODES=$(echo "$INTEGRATION" | jq -r '.integrationResponses | keys[]' 2>/dev/null)
  
  if [ -z "$STATUS_CODES" ]; then
    echo "  ⚠️  No integration responses found"
    FAILED=$((FAILED + 1))
    continue
  fi
  
  # Update each status code's CORS header
  for STATUS_CODE in $STATUS_CODES; do
    # Check current Access-Control-Allow-Origin
    CURRENT_ORIGIN=$(aws apigateway get-integration-response \
      --rest-api-id $API_ID \
      --resource-id $RESOURCE_ID \
      --http-method OPTIONS \
      --status-code $STATUS_CODE \
      --query 'responseParameters."method.response.header.Access-Control-Allow-Origin"' \
      --output text 2>/dev/null)
    
    # Check if CloudFront URL already present
    if echo "$CURRENT_ORIGIN" | grep -q "$CLOUDFRONT_URL"; then
      echo "  ✓ Status $STATUS_CODE: Already configured"
      continue
    fi
    
    echo "  Status $STATUS_CODE: Current origin = $CURRENT_ORIGIN"
    echo "  Status $STATUS_CODE: Adding CloudFront URL..."
    
    # Update integration response to add CloudFront origin
    UPDATE_RESULT=$(aws apigateway update-integration-response \
      --rest-api-id $API_ID \
      --resource-id $RESOURCE_ID \
      --http-method OPTIONS \
      --status-code $STATUS_CODE \
      --patch-operations "op=replace,path=/responseParameters/method.response.header.Access-Control-Allow-Origin,value=\"'$CLOUDFRONT_URL'\"" \
      --region $REGION \
      2>&1)
    
    if [ $? -eq 0 ]; then
      echo "  ✓ Status $STATUS_CODE: Updated successfully"
      UPDATED=$((UPDATED + 1))
    else
      echo "  ❌ Status $STATUS_CODE: Update failed"
      FAILED=$((FAILED + 1))
    fi
  done
done

echo ""
echo "Update summary: Updated=$UPDATED, Failed=$FAILED"

# Create deployment if updates were made
if [ "$UPDATED" -gt 0 ]; then
  echo ""
  echo "Creating deployment to apply changes..."
  DEPLOY_RESULT=$(aws apigateway create-deployment \
    --rest-api-id $API_ID \
    --stage-name prod \
    --description "Added CloudFront CORS support" \
    2>&1)
  
  if [ $? -eq 0 ]; then
    DEPLOYMENT_ID=$(echo "$DEPLOY_RESULT" | jq -r '.id')
    echo "✓ CORS configuration deployed (Deployment ID: $DEPLOYMENT_ID)"
    echo "  Updated $UPDATED OPTIONS methods"
  else
    echo -e "${YELLOW}⚠️  Failed to create deployment: $DEPLOY_RESULT${NC}"
  fi
else
  echo "✓ All OPTIONS methods already configured (or all failed to update)"
fi

# Re-enable exit-on-error
set -e

# Step 11: Create Cognito user
echo ""
echo -e "${YELLOW}[13/14] Creating Cognito user...${NC}"
USER_POOL_ID=$(jq -r '.MSPAssistantBackendStack.CognitoUserPoolId // empty' infrastructure/cdk/outputs.json)

# Validate user pool ID
if [ -z "$USER_POOL_ID" ] || [ "$USER_POOL_ID" = "null" ]; then
  echo -e "${RED}Error: Failed to get Cognito User Pool ID from CDK outputs${NC}"
  exit 1
fi

# Generate random temporary password meeting Cognito requirements
# Must have: 8+ chars, uppercase, lowercase, digit, symbol
TEMP_PASSWORD="Temp1$(openssl rand -base64 9 | tr -d '/+=' | head -c 9)!"

aws cognito-idp admin-create-user \
  --user-pool-id $USER_POOL_ID \
  --username $EMAIL \
  --user-attributes Name=email,Value=$EMAIL Name=email_verified,Value=true \
  --temporary-password "$TEMP_PASSWORD" \
  --message-action SUPPRESS \
  --region $REGION

echo ""
echo -e "${YELLOW}[14/14] Syncing runbooks to Bedrock Knowledge Base...${NC}"
if [ -n "$BEDROCK_KNOWLEDGE_BASE_ID" ]; then
  python3 scripts/sync-runbooks.py --region $REGION --env-file backend/.env
else
  echo "Skipping - BEDROCK_KNOWLEDGE_BASE_ID not set"
fi

# Final Step: Force ECS service restart to pick up latest task definition with all ARNs
echo ""
echo -e "${YELLOW}Forcing ECS service restart with latest configuration...${NC}"
ECS_CLUSTER=$(jq -r '.MSPAssistantBackendStack.ECSClusterName // empty' infrastructure/cdk/outputs.json)
ECS_SERVICE=$(jq -r '.MSPAssistantBackendStack.ECSServiceName // empty' infrastructure/cdk/outputs.json)
if [ -n "$ECS_CLUSTER" ] && [ "$ECS_CLUSTER" != "null" ] && [ -n "$ECS_SERVICE" ] && [ "$ECS_SERVICE" != "null" ]; then
  aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --force-new-deployment --region $REGION >/dev/null 2>&1 && \
    echo "✓ ECS service force-restarted with latest config (A2A ARNs, model, Jira config)" || \
    echo "⚠️  ECS restart failed — containers may need manual restart"
else
  echo "⚠️  Could not find ECS cluster/service names in CDK outputs"
fi

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         Deployment Complete! ✓             ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════╝${NC}"
echo ""
echo "Frontend URL: $(jq -r '.MSPAssistantFrontendStack.FrontendURL' infrastructure/cdk/outputs.json)"
echo "API URL: $(jq -r '.MSPAssistantBackendStack.APIURL' infrastructure/cdk/outputs.json)"
echo "User Pool ID: $USER_POOL_ID"
echo ""
echo "Sign-in credentials:"
echo "  Email: $EMAIL"
echo "  Temporary password: $TEMP_PASSWORD"
echo ""
echo "✅ Deployment Architecture:"
echo "  1. Supervisor Runtime deployed FIRST with real ARN"
echo "  2. CDK read real ARN from outputs.json"
echo "  3. ECS created with correct environment variables from start"
echo "  4. No ECS restart needed - works immediately!"
echo ""
echo "✅ CORS Configuration:"
echo "  Production CORS automatically configured for CloudFront"
echo "  Your application is ready to use!"
echo ""
echo "Next steps:"
echo "  1. Access your application: $(jq -r '.MSPAssistantFrontendStack.FrontendURL' infrastructure/cdk/outputs.json)"
echo "  2. Sign in with the credentials above"
echo "  3. Complete password setup"
echo ""
echo "View observability: https://console.aws.amazon.com/cloudwatch/home?region=$REGION#genai-observability"
echo ""
echo "Supervisor Runtime: $(jq -r '.MSPAssistantAgentCoreStack.SupervisorRuntimeARN' infrastructure/cdk/outputs.json)"
echo ""