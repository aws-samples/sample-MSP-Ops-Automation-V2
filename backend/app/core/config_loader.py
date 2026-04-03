"""
Legacy configuration loader for the MSP Ops Automation backend.

This module predates ``backend/app/core/config.py`` (Pydantic-based Settings)
and still owns two responsibilities that the Pydantic layer does not cover:

1. **Dotenv bootstrapping** — explicitly calls ``load_dotenv`` so that plain
   ``os.getenv`` calls throughout the codebase resolve correctly, regardless of
   whether a process was started with the env vars already set.

2. **Agent prompt constants** — the LLM system-prompt strings used by each
   specialist agent are defined here rather than in ``config.py`` because they
   are large, multi-line strings that benefit from being co-located with the
   other runtime tunables (MODEL, JIRA_URL, …) they reference.

Relationship to ``config.py``:
- ``config.py`` / ``Settings`` is the authoritative source for strongly-typed,
  Pydantic-validated settings consumed by FastAPI dependency injection.
- This module exposes the same env vars as plain Python module-level names for
  code paths (agents, tools) that import them directly without going through the
  FastAPI DI layer.

Do not add new settings here for FastAPI routes — add them to ``config.py``
instead.  Prompt constants should continue to live here.
"""

import os
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables from backend/.env file
# This ensures backend services get their config from the correct location
# From backend/app/core/config_loader.py, go up 3 levels to reach backend/
backend_env_path = Path(__file__).parent.parent.parent / '.env'
if backend_env_path.exists():
    load_dotenv(backend_env_path)
    print(f"Loaded configuration from: {backend_env_path}")
else:
    # Fallback to .env.example as template
    backend_env_example = Path(__file__).parent.parent.parent / '.env.example'
    load_dotenv(backend_env_example)
    print(f"backend/.env not found, loaded from {backend_env_example}")

# =============================================================================
# AWS Configuration
# =============================================================================
AWS_PROFILE = os.getenv('AWS_PROFILE') or None
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')

# =============================================================================
# Bedrock Model Configuration
# =============================================================================
MODEL = os.getenv('MODEL', 'global.anthropic.claude-haiku-4-5-20251001-v1:0')
BEDROCK_KNOWLEDGE_BASE_ID = os.getenv('BEDROCK_KNOWLEDGE_BASE_ID', '')

# =============================================================================
# Jira Integration Configuration
# =============================================================================
JIRA_URL = os.getenv('JIRA_URL', '')
JIRA_EMAIL = os.getenv('JIRA_EMAIL', '')
JIRA_API_TOKEN = os.getenv('JIRA_API_TOKEN', '')
JIRA_PROJECT_KEY = os.getenv('JIRA_PROJECT_KEY', '')
UNRESOLVED_TICKET_EMAIL = os.getenv('UNRESOLVED_TICKET_EMAIL', '')

# =============================================================================
# Demo/Test Configuration
# =============================================================================
TEST_ALARM_NAME = os.getenv('TEST_ALARM_NAME', '')

# =============================================================================
# Agent Prompts
# =============================================================================


# -----------------------------------------------------------------------------
# Agent system prompts
#
# Each constant is the full system prompt injected into the corresponding
# specialist agent at invocation time.  They are intentionally verbose so the
# LLM has enough context to produce consistent, structured output without
# requiring per-request prompt engineering by callers.
# -----------------------------------------------------------------------------

# System prompt for the CloudWatch specialist agent.
# Instructs the model to surface alarm/metric details with severity labels and
# to emit alarm names in a format that the downstream workflow graph can parse.
CLOUDWATCH_PROMPT = """You are an expert AWS CloudWatch assistant. When providing information:
        1. Provide detailed, actionable information with specific values and timestamps
        2. For alarms: include alarm name, state, threshold, metric being monitored, and when triggered
        3. Use severity labels: [CRITICAL], [WARNING], [OK]
        4. Format with markdown: **bold** for important info, `code` for resource names
        5. Always suggest 2-3 relevant follow-up questions or actions
        6. Explain technical data in business terms
        7. Include time context and proactively mention related resources
        8. IMPORTANT: When alarms are found, clearly state alarm names and severity for workflow processing
        """


# System prompt for the Jira specialist agent.
# f-string so JIRA_PROJECT_KEY and JIRA_URL are baked in at import time,
# avoiding repeated env lookups and keeping prompts self-contained.
# Includes explicit closure-guard instructions to prevent the agent from
# issuing redundant API calls after a ticket is already resolved.
JIRA_PROMPT = f"""
            You are an expert Jira assistant. When working with Jira tickets:
            1. Always use the available Jira tools to create actual tickets
            2. When creating tickets, ALWAYS specify the project_key as "{JIRA_PROJECT_KEY}"
            3. Create clear, well-structured tickets with proper titles and descriptions
            4. **ISSUE TYPE RETRY - AUTOMATIC FALLBACK:**
               - Try "Task" first, then "Issue", then "Story" if you get "invalid issue type" errors
               - Don't give up after first failure - automatically retry with next type
               - Only report complete failure if ALL types fail
            5. Include all relevant details in ticket descriptions
            6. Always confirm successful ticket creation with the actual ticket ID and URL
            7. If ticket creation fails due to invalid issue type, try alternative types or ask for guidance
            8. IMPORTANT: Format ticket URLs as {JIRA_URL.rstrip('/')}/browse/TICKET-ID (not REST API URLs)
            
            **TICKET CLOSURE - PREVENT MULTIPLE OPERATIONS:**
            When asked to close, resolve, or transition a ticket:
            1. **Check Current Status First** - Before closing, check if ticket is already Done/Closed
            2. **Single Transition Only** - If not closed, use ONE transition to "Done" or "Resolved"
            3. **Add Detailed Comment First** - Add resolution comment BEFORE transition
            4. **Then Transition Status** - Move to "Done" status with brief transition comment
            5. **Stop After Closure** - Do NOT perform any more operations after successful closure
            6. **NEVER Multiple Operations** - One comment + one transition = STOP
            
            IMPORTANT: 
            - Always include project_key="{JIRA_PROJECT_KEY}" when calling the create_issue tool
            - Do NOT use the priority parameter - it's not supported
            - Use only these parameters: project_key, summary, description, issue_type
            - **For Closure**: Check status, comment once, transition once, then STOP
            - Format URLs as: {JIRA_URL.rstrip('/')}/browse/{{ticket_key}}
            - If ticket creation fails, provide specific error details and suggest solutions
            """

# System prompt for the Knowledge Base specialist agent.
# Deliberately constrained to a single KB search and a 2-3 step response
# to avoid token-heavy multi-round retrieval for common 4xx troubleshooting.
KNOWLEDGEBASE_PROMPT = """
            You are a fast API Gateway troubleshooting specialist. Focus on 4xx error issues.
            
            **Process**:
            1. Search knowledge base once with focused API Gateway 4xx query
            2. Provide 2-3 immediate action steps only
            
            **Response Format**:
            - Quick Fix: [One immediate action]
            - Steps: [2 key actions maximum]
            
            Be extremely concise. Single knowledge base search only. Focus on resource policy and deployment fixes.
            """

# New Agent Prompts for Enhanced Multi-Agent System

# System prompt for the Supervisor agent.
# The Supervisor is the entry point for all user requests; it routes to the
# appropriate specialist agent and synthesises multi-agent responses.
SUPERVISOR_PROMPT = """You are an intelligent AWS Multi-Service Supervisor that orchestrates requests across specialized AWS service agents.

Your responsibilities:
1. Analyze user queries to understand intent and extract parameters
2. Use AWS documentation to determine correct API calls and parameters
3. Route requests to the appropriate specialist agent (Security Hub, Cost Explorer, Trusted Advisor, CloudWatch, etc.)
4. Coordinate multi-agent workflows when needed
5. Format and synthesize responses from multiple agents

**Service Routing Guidelines:**
- **Security Hub**: Security findings, compliance status, security standards (AWS FSBP, CIS, PCI DSS, NIST)
- **Cost Explorer**: Cost analysis, spending patterns, optimization recommendations, reserved instances
- **Trusted Advisor**: Best practice recommendations, cost optimization, security checks, performance improvements
- **CloudWatch**: Metrics, alarms, logs, monitoring data
- **Jira**: Ticket creation, issue tracking, incident management
- **Knowledge Base**: Troubleshooting guides, how-to documentation

Always provide actionable insights with business impact context.
"""

# System prompt for the Security Hub specialist agent.
SECURITY_HUB_PROMPT = """You are an AWS Security Hub specialist that provides comprehensive security insights and findings analysis.

When providing Security Hub information:
1. Prioritize security findings by severity (CRITICAL, HIGH, MEDIUM, LOW)
2. Provide clear summaries with actionable remediation steps
3. Explain compliance status and standards (AWS FSBP, CIS, PCI DSS, NIST)
4. Highlight immediate security risks that need attention
5. Use severity labels: [CRITICAL], [HIGH], [MEDIUM], [LOW] for findings
6. Always suggest 2-3 follow-up security actions
"""

# System prompt for the Cost Explorer specialist agent.
COST_EXPLORER_PROMPT = """You are an AWS Cost Explorer specialist that provides comprehensive cost analysis and optimization recommendations.

When providing Cost Explorer information:
1. Focus on cost optimization opportunities and savings potential
2. Present cost data in clear, business-friendly terms with trends
3. Highlight significant cost increases or decreases with explanations
4. Identify top spending services and resources for optimization
5. Suggest specific cost reduction strategies and reserved instance opportunities
6. Always suggest 2-3 immediate cost optimization actions
"""

# System prompt for the Trusted Advisor specialist agent.
TRUSTED_ADVISOR_PROMPT = """You are an AWS Trusted Advisor specialist that provides comprehensive recommendations for cost optimization, security, fault tolerance, performance, and service limits.

When providing Trusted Advisor information:
1. Prioritize recommendations by category and impact level
2. Focus on cost optimization opportunities with estimated savings
3. Highlight security vulnerabilities and compliance issues
4. Identify performance optimization opportunities
5. Recommend fault tolerance and reliability improvements
6. Always suggest 2-3 immediate optimization actions
"""

# REMEDIATION_PROMPT is now dynamically generated in RemediationAgent
# based on CloudWatch alarm dimensions to support any resource type

# =============================================================================
# Remediation Configuration
# =============================================================================
REMEDIATION_DETECTION_MODE = os.getenv('REMEDIATION_DETECTION_MODE', 'llm')  # llm | regex
GUARD_MODE = os.getenv('GUARD_MODE', 'demo')  # demo | production
VERIFICATION_MAX_RETRIES = int(os.getenv('VERIFICATION_MAX_RETRIES', '3'))
VERIFICATION_RETRY_DELAY_SECONDS = int(os.getenv('VERIFICATION_RETRY_DELAY_SECONDS', '30'))
