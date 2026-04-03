"""Supervisor agent — routes to A2A specialists via tools.

Prompts aligned with: https://github.com/aws-samples/sample-MSP-Ops-Automation/blob/main/config.py
"""
import os
import logging
from strands import Agent
from strands.models import BedrockModel
from supervisor_tools import create_supervisor_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL = os.getenv('MODEL_ID', os.getenv('MODEL', 'global.anthropic.claude-haiku-4-5-20251001-v1:0'))
# SUPERVISOR_MAX_TOKENS is isolated from specialist MAX_TOKENS (which env_config.txt sets to 512).
# Supervisor needs 4096 to relay specialist responses without MaxTokensReachedException.
# Specialists use their own context_tools.py MAX_TOKENS default (also 4096).
#
# The two env vars serve different purposes:
#   SUPERVISOR_MAX_TOKENS — controls only this supervisor agent's output budget; set
#                           independently so it can be tuned without affecting specialists.
#   MAX_TOKENS            — a shared fallback used by all agents; the supervisor reads it
#                           only when SUPERVISOR_MAX_TOKENS is absent, ensuring specialist
#                           agents that also read MAX_TOKENS are not inadvertently changed
#                           when the supervisor's budget is adjusted.
MAX_TOKENS = int(os.getenv('SUPERVISOR_MAX_TOKENS', os.getenv('MAX_TOKENS', '4096')))
# Load Jira config from environment (written by deploy.sh, present in env_config.txt)
JIRA_PROJECT_KEY = os.getenv('JIRA_PROJECT_KEY', 'MD')
JIRA_DOMAIN = os.getenv('JIRA_DOMAIN', '')

SUPERVISOR_PROMPT = f"""You are an AWS Multi-Service Supervisor that routes requests to specialist agents.

**Route IMMEDIATELY to the correct specialist — do not analyze or explain before calling the tool:**
- **check_cloudwatch**: alarms, metrics, logs, monitoring
- **check_security**: Security Hub findings, compliance (FSBP, CIS, PCI DSS, NIST)
- **analyze_costs**: cost analysis, spending, optimization, reserved instances
- **check_advisor**: Trusted Advisor recommendations, best practices
- **manage_jira**: ticket search/listing, creation, updates, comments, transitions
- **search_knowledge**: troubleshooting guides, how-to documentation

**Jira Configuration (use these values directly — never ask the user):**
- Project key: {JIRA_PROJECT_KEY}
- Domain: {JIRA_DOMAIN}

**Rules:**
1. For single-domain queries, call the specialist tool IMMEDIATELY without preamble
2. For multi-domain queries, call tools in sequence and synthesize results
3. NEVER retry a tool call that returns an error — report the error and stop
4. For Jira: ALWAYS use manage_jira tool (never simulate); include project key {JIRA_PROJECT_KEY} in the prompt
5. Batch multiple Jira operations into a SINGLE manage_jira call
6. NEVER ask the user for project key, domain, or Jira configuration — use the values above
7. Keep your own synthesis brief (under 100 words). The specialist response IS the answer — relay it directly without extensive paraphrasing.
8. For Jira queries (search, list, get tickets): use manage_jira — it supports full JQL search

Provide actionable insights with business context.
"""


def create_supervisor_agent() -> Agent:
    """Create supervisor agent with A2A delegation tools.

    MAX_TOKENS budget: The supervisor's job is routing + brief synthesis, not generating
    long answers itself. A 4096-token ceiling is large enough to relay a full specialist
    response without hitting MaxTokensReachedException, while still preventing runaway
    verbosity. Specialists each have their own independent token budget.

    Extended thinking is intentionally disabled: Strands SDK issue #1698 causes the
    streaming loop to stall when extended thinking is combined with multi-turn tool-use
    (the model emits a thinking block, then the tool_use event never closes cleanly).
    Re-enable only after that bug is resolved upstream.
    """
    tools = create_supervisor_tools()
    logger.info(f"Creating supervisor with {len(tools)} delegation tools")
    logger.info(f"Model: {MODEL}, MAX_TOKENS: {MAX_TOKENS}")
    logger.info(f"Jira project key: {JIRA_PROJECT_KEY}")

    return Agent(
        name="MSP Ops Supervisor",
        description="Coordinates specialist agents for MSP operations across customer AWS accounts",
        model=BedrockModel(model_id=MODEL, max_tokens=MAX_TOKENS),
        tools=tools,
        system_prompt=SUPERVISOR_PROMPT,
    )
