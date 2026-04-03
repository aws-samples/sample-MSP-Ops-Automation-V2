"""Supervisor tools — delegate to A2A specialist agents.

Account context is passed via contextvars for concurrency safety.
Each async task / request gets its own isolated context, preventing
cross-request credential bleed when multiple users hit the supervisor
simultaneously.
"""
import contextvars
import logging
import threading
from strands import tool
from a2a_client_helper import send_to_agent_sync
from robust_date_parser import RobustDateParser

logger = logging.getLogger(__name__)

# Initialize date parser (no LLM needed for regex/library parsing)
_date_parser = RobustDateParser(llm_agent=None)

# HYBRID APPROACH for concurrency safety:
# - _current_context uses ContextVar (set by runtime, read by tools during execution)
# - _current_request_id uses ContextVar (propagates to tools so they know their dict key)
# - tool tracking uses a request-ID keyed dict protected by a lock
#
# Why a dict instead of module globals? Module globals race when multiple requests are
# in-flight simultaneously: reset_tools_called() for Request2 clears Request1's tracking
# mid-execution. The dict gives each request an isolated slot.
# ContextVars propagate downward (runtime → tools) so tools can read _current_request_id,
# but tool→runtime writes are not visible, hence the shared dict for result handoff.

_current_context: contextvars.ContextVar[dict] = contextvars.ContextVar(
    '_current_context', default={"account_name": "", "region": "us-east-1"}
)
_current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    '_current_request_id', default=''
)

# Request-ID keyed tool tracking: {request_id: {"last": str, "all": list}}
_tool_state: dict = {}
_tool_state_lock = threading.Lock()


def _record_tool(tool_name: str) -> None:
    """Record a tool invocation in the current request's tracking slot."""
    rid = _current_request_id.get()
    with _tool_state_lock:
        state = _tool_state.setdefault(rid, {"last": "", "all": []})
        state["last"] = tool_name
        if tool_name not in state["all"]:
            state["all"].append(tool_name)


def set_context(account_name: str, region: str = "us-east-1", request_id: str = ""):
    """Set the current account context and request ID (called by runtime before invoke)."""
    _current_context.set({"account_name": account_name, "region": region})
    if request_id:
        _current_request_id.set(request_id)
    logger.info(f"Supervisor context set: account_name={account_name!r}, region={region}, request_id={request_id!r}")


def reset_tools_called(request_id: str = ""):
    """Initialize a per-request tool tracking slot."""
    with _tool_state_lock:
        _tool_state[request_id] = {"last": "", "all": []}


def get_last_tool_called(request_id: str = "") -> str:
    """Get the last tool called for the given request."""
    with _tool_state_lock:
        return _tool_state.get(request_id, {}).get("last", "")


def get_all_tools_called(request_id: str = "") -> list:
    """Get all tools called for the given request, in order."""
    with _tool_state_lock:
        return list(_tool_state.get(request_id, {}).get("all", []))


def clear_tools_state(request_id: str = "") -> None:
    """Remove tool tracking state after a request completes (prevents unbounded growth)."""
    with _tool_state_lock:
        _tool_state.pop(request_id, None)


@tool
def check_cloudwatch(prompt: str) -> str:
    """Check CloudWatch alarms, metrics, and logs.
    
    Use for: alarms, metrics, logs, monitoring data.
    
    Args:
        prompt: What to check (e.g., "List all alarms in ALARM state")
    """
    _record_tool("check_cloudwatch")
    ctx = _current_context.get()
    account_name, region = ctx["account_name"], ctx["region"]
    logger.info(f"CloudWatch [{account_name}]: {prompt[:50]}...")
    return send_to_agent_sync("cloudwatch", prompt, account_name, region)


@tool
def check_security(prompt: str) -> str:
    """Check Security Hub findings and compliance status.
    
    Use for: security findings, compliance (AWS FSBP, CIS, PCI DSS, NIST).
    
    Args:
        prompt: What to check (e.g., "Show critical findings")
    """
    _record_tool("check_security")
    ctx = _current_context.get()
    account_name, region = ctx["account_name"], ctx["region"]
    logger.info(f"Security [{account_name}]: {prompt[:50]}...")
    return send_to_agent_sync("security", prompt, account_name, region)


@tool
def analyze_costs(prompt: str) -> str:
    """Analyze AWS costs and optimization opportunities.
    
    Use for: cost analysis, spending patterns, optimization, reserved instances.
    
    Args:
        prompt: What to analyze (e.g., "Show cost breakdown for last 30 days")
    """
    _record_tool("analyze_costs")
    ctx = _current_context.get()
    account_name, region = ctx["account_name"], ctx["region"]
    logger.info(f"Cost [{account_name}]: {prompt[:50]}...")

    # Date pre-processing: the cost specialist agent receives the prompt after the LLM
    # has already started generating, so it cannot interactively ask the user to clarify
    # "last month" vs a specific date range. RobustDateParser resolves ambiguous natural
    # language periods into concrete ISO dates before the prompt is dispatched, removing
    # any guesswork from the cost agent's Cost Explorer API calls.
    #
    # The keyword gate avoids the overhead of date parsing on prompts that clearly have
    # no temporal component (e.g. "what is my account ID?").
    time_keywords = ['cost', 'spending', 'bill', 'month', 'year', 'quarter', 'week', 'day',
                     'recent', 'last', 'past', 'today', 'yesterday']

    enhanced_prompt = prompt
    if any(keyword in prompt.lower() for keyword in time_keywords):
        try:
            date_range = _date_parser.parse_time_period(prompt)
            logger.info(f"Extracted time range: {date_range.period_days} days ({date_range.start_date} to {date_range.end_date})")
            logger.info(f"   Confidence: {date_range.confidence:.1%}, Method: {date_range.method_used}")

            # Append explicit date context to prompt
            time_context = f"""

TIME PERIOD CONTEXT - USE THESE EXACT DATES:
Start date: {date_range.start_date}
End date: {date_range.end_date}
Days: {date_range.period_days}
Confidence: {date_range.confidence:.1%}
"""
            enhanced_prompt = prompt + time_context
        except Exception as e:
            logger.warning(f"Date extraction failed: {e}, proceeding without time context")

    return send_to_agent_sync("cost", enhanced_prompt, account_name, region)


@tool
def check_advisor(prompt: str) -> str:
    """Check Trusted Advisor recommendations.
    
    Use for: best practices, cost optimization, security checks, performance.
    
    Args:
        prompt: What to check (e.g., "Show all recommendations")
    """
    _record_tool("check_advisor")
    ctx = _current_context.get()
    account_name, region = ctx["account_name"], ctx["region"]
    logger.info(f"Advisor [{account_name}]: {prompt[:50]}...")
    return send_to_agent_sync("advisor", prompt, account_name, region)


@tool
def manage_jira(prompt: str) -> str:
    """Search, create, update, or query Jira tickets.

    Use for: ticket search/listing, creation, updates, issue tracking, incident management.
    
    Args:
        prompt: What to do (e.g., "Create ticket for alarm XYZ")
    """
    _record_tool("manage_jira")
    ctx = _current_context.get()
    account_name, region = ctx["account_name"], ctx["region"]
    logger.info(f"Jira [{account_name}]: {prompt[:50]}...")

    # Inject Jira configuration directly into the prompt so the Jira specialist
    # never has to ask the user for credentials or project details.
    # Values are read from env at call time (not module load) so that late-binding
    # env files (env_config.txt loaded by a2a_client_helper) are always visible.
    import os as _os
    jira_project_key = _os.getenv('JIRA_PROJECT_KEY', 'MD')
    jira_domain = _os.getenv('JIRA_DOMAIN', '')
    jira_email = _os.getenv('JIRA_EMAIL', '')

    jira_context = (
        f"\n\n[Jira Config — use these values, never ask the user]\n"
        f"project_key: {jira_project_key}\n"
        f"domain: {jira_domain}\n"
        f"email: {jira_email}\n"
    )
    enriched_prompt = prompt + jira_context
    return send_to_agent_sync("jira", enriched_prompt, account_name, region)


@tool
def search_knowledge(prompt: str) -> str:
    """Search AWS knowledge base for troubleshooting and documentation.
    
    Use for: troubleshooting guides, how-to, AWS documentation.
    
    Args:
        prompt: What to search (e.g., "How to resolve high CPU on EC2")
    """
    _record_tool("search_knowledge")
    ctx = _current_context.get()
    account_name, region = ctx["account_name"], ctx["region"]
    logger.info(f"Knowledge: {prompt[:50]}...")
    return send_to_agent_sync("knowledge", prompt, account_name or "general", region)


def create_supervisor_tools():
    """Return list of supervisor tools for A2A delegation."""
    return [
        check_cloudwatch,
        check_security,
        analyze_costs,
        check_advisor,
        manage_jira,
        search_knowledge
    ]