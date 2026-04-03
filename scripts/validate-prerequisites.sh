#!/bin/bash
# Validate all prerequisites before deployment

set -e

echo "Checking prerequisites..."

# AWS CLI (must be v2.33.8 or newer for AgentCore)
if ! command -v aws &> /dev/null; then
    echo "❌ AWS CLI not found. Install: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html"
    exit 1
fi

# Check version
AWS_VERSION=$(aws --version 2>&1 | awk '{print $1}' | cut -d'/' -f2)
REQUIRED_VERSION="2.33.8"

# Simple version comparison (works for X.Y.Z format)
if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$AWS_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then
    echo "❌ AWS CLI version $AWS_VERSION is too old"
    echo "   AgentCore requires AWS CLI v2.33.8 or newer"
    echo "   Update: brew upgrade awscli (macOS)"
    echo "   Or download: https://awscli.amazonaws.com/AWSCLIV2.pkg"
    exit 1
fi

# Verify AgentCore commands available
if ! aws bedrock-agentcore-control help &> /dev/null; then
    echo "❌ AWS CLI doesn't support bedrock-agentcore-control commands"
    echo "   Update to AWS CLI v2.33.8 or newer"
    exit 1
fi

echo "✓ AWS CLI installed (version $AWS_VERSION)"

# AWS credentials
if ! aws sts get-caller-identity &> /dev/null; then
    echo "❌ AWS credentials not configured"
    exit 1
fi
echo "✓ AWS credentials configured"

# Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found"
    exit 1
fi
echo "✓ Docker installed"

# Node.js
if ! command -v node &> /dev/null; then
    echo "❌ Node.js not found"
    exit 1
fi
echo "✓ Node.js installed"

# Python 3.11
if ! python3 --version | grep -q "3.11"; then
    echo "⚠️  Python 3.11 recommended"
fi
echo "✓ Python installed"

# CDK
if ! command -v cdk &> /dev/null; then
    echo "❌ AWS CDK not found. Install: npm install -g aws-cdk"
    exit 1
fi
echo "✓ AWS CDK installed"

# Bedrock AgentCore toolkit
if ! command -v agentcore &> /dev/null; then
    echo "❌ AgentCore CLI not found. Install: pip install bedrock-agentcore-starter-toolkit"
    exit 1
fi
echo "✓ AgentCore CLI installed"

# Bedrock model access
REGION=${1:-us-east-1}
echo "Checking Bedrock model access..."
if ! aws bedrock list-foundation-models --region $REGION --query "modelSummaries[?modelId=='us.anthropic.claude-3-7-sonnet-20250219-v1:0'].modelId" --output text | grep -q "claude"; then
    echo "⚠️  Claude Sonnet model access required"
    echo "   Enable at: https://console.aws.amazon.com/bedrock/home?region=$REGION#/modelaccess"
fi

echo ""
echo "✓ All prerequisites validated"
