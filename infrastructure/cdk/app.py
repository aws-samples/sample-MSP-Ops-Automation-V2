#!/usr/bin/env python3
"""
MSP Assistant AgentCore Migration - CDK App Entry Point
========================================================
Orchestrates the three-stack deployment for the MSP Ops Automation platform.

Stack deployment order (enforced by add_dependency):
  1. AgentCoreStack  — AgentCore Memory, Gateway, Observability.
                       Deployed first; exports ARNs consumed by BackendStack.
                       Resources are created imperatively via boto3 (CDK L2
                       constructs for AgentCore are not yet available).

  2. BackendStack    — ECS Fargate service + ALB + API Gateway + Cognito.
                       Receives agentcore_resources dict (ARNs/IDs) from
                       AgentCoreStack and bakes them into ECS environment vars.
                       Depends on: AgentCoreStack.

  3. FrontendStack   — React SPA on S3 + CloudFront CDN.
                       Receives the API Gateway URL and Cognito config from
                       BackendStack at synthesis time, and embeds them in a
                       runtime config.json served alongside the SPA.
                       Depends on: BackendStack.

Context variables (passed via --context or cdk.json):
  account                 — AWS account ID (defaults to CDK caller account)
  region                  — AWS region (defaults to us-east-1)
  supervisor_runtime_arn  — AgentCore Supervisor Runtime ARN (set by deploy.sh)
  cloudwatch_a2a_arn, security_a2a_arn, cost_a2a_arn,
  advisor_a2a_arn, jira_a2a_arn, knowledge_a2a_arn
                          — A2A Specialist Runtime ARNs for direct routing
                            (optional; populated by deploy.sh Step 9)
  alb_dns                 — ALB DNS name for CloudFront → ALB SSE streaming
                            origin (empty on first deploy; set on re-deploys)

Deploy: cdk deploy --all
"""
import aws_cdk as cdk
from stacks.backend_stack import BackendStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.frontend_stack import FrontendStack

app = cdk.App()

# Get config
env = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region") or "us-east-1"
)

# 1. AgentCore Infrastructure
agentcore_stack = AgentCoreStack(
    app, "MSPAssistantAgentCoreStack",
    env=env,
    description="AgentCore Runtime, Gateway, Memory, Identity, Policy, Observability (uksb-lfevfsxkwc)(tag:agentcore)"
)

# 2. Backend (ECS Fargate + ALB + API Gateway)
backend_stack = BackendStack(
    app, "MSPAssistantBackendStack",
    agentcore_resources=agentcore_stack.resources,
    env=env,
    description="FastAPI on ECS Fargate with ALB and API Gateway (uksb-lfevfsxkwc)(tag:backend)"
)
backend_stack.add_dependency(agentcore_stack)

# 3. Frontend (S3 + CloudFront)
frontend_stack = FrontendStack(
    app, "MSPAssistantFrontendStack",
    api_url=backend_stack.api_url,
    alb_dns=app.node.try_get_context("alb_dns") or "",
    cognito_config=backend_stack.cognito_config,
    env=env,
    description="React SPA on S3 with CloudFront CDN (uksb-lfevfsxkwc)(tag:frontend)"
)
frontend_stack.add_dependency(backend_stack)

# Tags
for stack in [agentcore_stack, backend_stack, frontend_stack]:
    cdk.Tags.of(stack).add("Project", "MSP-Assistant-AgentCore")
    cdk.Tags.of(stack).add("ManagedBy", "CDK")

app.synth()
